# WebSocket Architecture

This document describes every WebSocket connection in TradeAutonom, what it does, who owns it, and how it reconnects.

---

## Overview

There are four distinct layers of WebSocket connections:

| Layer | Owner | Purpose | Scope |
|-------|-------|---------|-------|
| **SharedAuthWS** | `BotRegistry` | Authenticated account stream (positions, fills, balances) | One per exchange, shared by all bots |
| **Fill WS** | Exchange clients | Real-time fill notifications for TWAP execution | One per exchange client, shared by all bots on that exchange |
| **Orderbook WS** | `DataLayer` (OMS) | Live bid/ask snapshots for spread calculation | One shared OMS connection or per-symbol HTTP poll fallback |
| **Legacy OB WS** | `ws_feeds.py` threads | Orderbook data for the legacy single-job mode | One thread per symbol, only used by `_fn_engine` |

---

## 1. Shared Authenticated WebSocket (`SharedAuthWebSocketManager`)

**File:** `app/shared_auth_ws_manager.py`  
**Managed by:** `BotRegistry` via `SharedWebSocketManagerRegistry`

### What it does

One persistent authenticated WS connection per exchange. Subscribes to the account-level stream that delivers real-time position updates, fill events, and balance changes. All bots share this single connection — there is no per-bot connection.

Incoming messages are parsed and written into `SharedDataCache` (an in-memory store keyed by `(exchange, symbol)`), which the `StateMachine` reads when it needs the current position without making a REST call.

### Lifecycle

```
BotRegistry.start_all()
  └── _init_shared_resources()
        └── SharedWebSocketManagerRegistry.create_manager(exchange, client)
              └── SharedAuthWebSocketManager.start()
                    └── asyncio.Task: _run_websocket()
                          └── _connect_and_run()   ← loops via async for ws in websockets.connect()
```

The task is cancelled and awaited in `BotRegistry.stop_all()` → `_stop_shared_resources()`.

### Endpoints

| Exchange | URL | Auth |
|----------|-----|------|
| Extended | `wss://api.starknet.extended.exchange/stream.extended.exchange/v1/account` | `X-Api-Key` header |
| GRVT | `wss://market-data.grvt.io/ws/full` (prod) | none (position handler is a stub — GRVT position data comes from REST or fill WS) |
| Nado | `wss://gateway.prod.nado.xyz/v1/subscribe` (OMS) | none |

> **Note:** The OMS now uses the **Nado Watchdog System** for orderbook connections. See [Nado Watchdog WebSocket](NADO_WATCHDOG_WEBSOCKET.md) for detailed documentation on dynamic symbol management, health monitoring, and WebSocket usage.

### Reconnect behaviour

Uses `async for ws in websockets.connect()` — the websockets library's legacy auto-reconnecting iterator. On disconnect or error the library automatically reconnects with Fibonacci backoff (0.5 s → 1 s → 2 s → … → 90 s max). The application adds its own delay layer on top via `_run_websocket()`:

```python
reconnect_delay = 1.0
while self._running:
    try:
        await self._connect_and_run()   # loops internally via async for
        break
    except asyncio.CancelledError:
        break
    except Exception as exc:
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 1.5, 30.0)
```

On reconnect, `_resubscribe_all()` re-sends subscribe messages for every symbol that has active bot subscribers.

### Symbol subscription tracking

Each bot calls `BotRegistry._subscribe_bot_symbols()` when it starts, which calls `manager.subscribe_symbol(bot_id, symbol)`. A `SymbolSubscriptionTracker` records which bot IDs are subscribed to each symbol. When the last bot unsubscribes (bot deleted), an unsubscribe message is sent to the exchange.

---

## 2. Fill WebSocket (per Exchange Client)

**Files:** `app/extended_client.py`, `app/grvt_client.py`, `app/nado_client.py`

### What it does

Each exchange client maintains ONE shared fill WS connection per client instance. Multiple bots on the same exchange register per-symbol callbacks via `async_subscribe_fills(symbol, callback)`. When a fill arrives the client dispatches it to the matching callback so the `StateMachine` can detect maker fills in real time.

The `StateMachine` calls `client.async_subscribe_fills(symbol, on_fill)` at the start of each execution session and cancels it on stop.

### Lifecycle

```
StateMachine._subscribe_fill_ws()
  └── client.async_subscribe_fills(symbol, callback)
        └── registers callback in client._fill_callbacks dict
        └── if no task running: asyncio.create_task(_run_shared_fill_ws())
```

Only one `_run_shared_fill_ws` task runs per client regardless of how many bots register callbacks. If three bots on Extended all call `subscribe_fills`, they each get an entry in `_fill_callbacks` but share the same underlying WS connection.

### Endpoints

| Exchange | URL | Auth |
|----------|-----|------|
| Extended | `wss://api.starknet.extended.exchange/stream.extended.exchange/v1/account` | `X-Api-Key` header |
| GRVT | `wss://trades.grvt.io/ws/full` (prod) | Cookie + `X-Grvt-Account-Id` header |
| Nado | `wss://gateway.prod.nado.xyz/v1/subscribe` (derived from `_gateway_rest`) | none |

### Reconnect behaviour

- **Extended / GRVT:** `async with websockets.connect()` in a manual `while self._running` loop with exponential backoff (1 s → 30 s max), and a cookie refresh before each GRVT reconnect.  
- **Nado:** `async for ws in websockets.connect()` (library auto-reconnect).

---

## 3. OMS WebSocket / Orderbook Feeds (`DataLayer`)

**File:** `app/data_layer.py`  
**Managed by:** `BotRegistry` via one shared `DataLayer` instance  
(`BotRegistry._start_oms_websocket()` creates it with `symbols_map={}`)

### What it does

Provides real-time bid/ask orderbook snapshots to the `StateMachine` for spread calculation and entry/exit decisions. Each bot's symbols are added dynamically via `DataLayer.add_symbols()` after the OMS DataLayer starts.

> **Nado Symbols:** The OMS uses the **Nado Watchdog System** for dynamic symbol management. Symbols are automatically discovered, health-monitored, and subscribed. See [Nado Watchdog WebSocket](NADO_WATCHDOG_WEBSOCKET.md) for the WebSocket API and symbol management details.

### Connection hierarchy

```
DataLayer._run_oms_ws()           ← ONE shared WS to the OMS aggregator
    ws://192.168.133.100:8099/ws

  On connect: subscribes each (exchange, symbol) pair
  On disconnect / HTTP 400: falls back to per-symbol HTTP polling
        └── DataLayer._run_ob_oms_poll(exchange, symbol)
                GET http://192.168.133.100:8099/book/{exchange}/{symbol}
                every 500 ms
```

### Fallback logic

`_run_oms_ws()` uses `async with websockets.connect()`. If the `/ws` endpoint returns HTTP 400 or is unavailable, `InvalidStatusCode` is raised and caught by the application-level handler. After `max_failures_before_fallback = 5` attempts it breaks to HTTP polling.

`add_symbols()` also starts HTTP polling immediately if `_oms_ws_active` is False at subscription time, so orderbook data is always available even during WS startup.

### Per-exchange direct WS (unused in shared mode)

`DataLayer` also contains direct per-exchange orderbook WS methods (`_run_ob_ws_grvt`, `_run_ob_ws_extended`, `_run_ob_ws_nado`) and position WS methods (`_run_pos_ws_grvt`, `_run_pos_ws_extended`, `_run_pos_ws_nado`). These are only started when a `DataLayer` is created with an explicit `symbols_map` (i.e. the legacy single-job `_fn_engine` path). They are **not used** in the shared BotRegistry architecture.

#### GRVT OB WS reconnect (direct mode)
`async with websockets.connect()` in a `while self._running` loop. `ConnectionClosed` sleeps `reconnect_delay` (1 s → 30 s); other exceptions do the same. `reconnect_delay` resets to 1 s on a successful connect.

#### GRVT Position WS reconnect (direct mode)
Same pattern. Additionally, `GrvtCcxt.refresh_cookie()` is called via `asyncio.to_thread()` before each connect so the cookie is always fresh without blocking the event loop.

---

## 4. Legacy Orderbook Feed Threads (`ws_feeds.py`)

**File:** `app/ws_feeds.py`  
**Used by:** `OrderbookFeedManager` (instantiated in `server.py` for the legacy `JobManager`)

### What it does

Three `threading.Thread` subclasses, one per exchange, that each maintain a **synchronous** WebSocket connection (`websockets.sync.client.connect`) for orderbook data. They write into shared `OrderbookSnapshot` objects that the legacy `_arb_engine` and `JobManager` jobs read.

| Thread | Exchange | URL |
|--------|----------|-----|
| `_ExtendedFeedThread` | Extended | `wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks/{symbol}` |
| `_GrvtFeedThread` | GRVT | `wss://market-data.grvt.io/ws/full` (subscribes `v1.book.s`) |
| `_NadoFeedThread` | Nado | `wss://gateway.prod.nado.xyz/v1/subscribe` |

### Reconnect behaviour

Synchronous — each thread's `run()` loop catches exceptions, logs, waits `reconnect_delay` (1 s → 30 s), and re-enters `_connect_and_listen()`. No asyncio involved; entirely separate from the async WS stack.

### Usage scope

`OrderbookFeedManager` only starts feed threads when `add_feed(exchange, symbol)` is called, which happens when a `JobManager` job is created. If no `JobManager` jobs exist (the common case when using `BotRegistry`), no threads are started.

---

## 5. Connection Map (Startup, BotRegistry Mode)

```
process startup
│
├── SharedWebSocketManagerRegistry (BotRegistry)
│   ├── SharedAuthWS[extended]  wss://...extended.exchange/.../v1/account  (async for ws)
│   ├── SharedAuthWS[grvt]      wss://market-data.grvt.io/ws/full          (async for ws)
│   └── SharedAuthWS[nado]      wss://gateway.prod.nado.xyz/v1/subscribe    (async for ws)
│
├── OMS DataLayer (shared, empty symbols at init)
│   └── _run_oms_ws             ws://192.168.133.100:8099/ws                (async with ws)
│       └── fallback: HTTP poll GET .../book/{exchange}/{symbol}
│
└── Per-bot (10 × FundingArbEngine, all sharing OMS DataLayer)
    └── StateMachine fill WS subscriptions (lazy — started on first execution)
        ├── extended_client._run_shared_fill_ws  wss://...v1/account   (async with ws, 1 task)
        ├── grvt_client._run_shared_fill_ws      wss://trades.grvt.io  (async with ws, 1 task)
        └── nado_client._run_shared_fill_ws      wss://gateway.nado... (async for ws, 1 task)
```

---

## 6. Important Design Rules

**One fill WS per exchange client, not per bot.**  
`extended_client`, `grvt_client`, and `nado_client` each keep a single `_fill_ws_task`. Calling `async_subscribe_fills()` from multiple bots on the same exchange just adds callbacks to a dict — it does NOT open a new connection. Opening a second authenticated connection to the same endpoint with the same credentials causes the exchange to reject one of them.

**One SharedAuthWS per exchange, not per bot.**  
`SharedWebSocketManagerRegistry` is keyed by exchange name. `BotRegistry.create_manager()` is a no-op if the exchange already has a manager.

**`async for ws in websockets.connect()` hides errors from callers.**  
The websockets legacy iterator catches ALL exceptions (including `InvalidStatusCode` / HTTP 400) internally and retries with its own backoff. The application-level code never sees the exception. Use `async with websockets.connect()` when you need to count failures, trigger fallbacks, or apply custom retry logic.

**`async with websockets.connect()` propagates exceptions.**  
Each call attempt either succeeds (yields the `ws` object) or raises immediately. The application `except` block is reached on every failure, enabling proper failure counting and fallback decisions.

**Never start a second DataLayer with explicit symbols if SharedAuthWS is active.**  
If a `DataLayer` is started with `symbols_map` containing Extended/GRVT/Nado symbols, it will also start per-exchange position WS tasks that compete with `SharedAuthWS` for the same authenticated endpoint. Only the shared OMS `DataLayer` (started with `symbols_map={}`) should be used in BotRegistry mode.
