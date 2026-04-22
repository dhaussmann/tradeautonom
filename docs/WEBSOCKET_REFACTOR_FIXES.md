# WebSocket Refactor — Bug Fixes & Architecture Cleanup

**Date:** 2026-04-21 (Bugs 1–8) / 2026-04-22 (Bugs 9–10)  
**Scope:** `tradeautonom-v3` (tested and deployed here first)

---

## Background

A refactor introduced `SharedAuthWebSocketManager` — one authenticated WebSocket connection per exchange shared by all bots — and moved bot lifecycle management into `BotRegistry`. During the rollout, several bugs were found that caused continuous `! connect failed; retrying in Xs` log spam and degraded reconnect behaviour.

This document records all changes made, their root causes, and how to verify them.

---

## Changes Summary

| File | Change | Why |
|------|--------|-----|
| `app/shared_auth_ws_manager.py` | Fixed Extended WS URL | Was pointing to wrong endpoint |
| `app/shared_auth_ws_manager.py` | Fixed Nado WS URL | Double `/v1/` prefix bug |
| `app/bot_registry.py` | Guard against double `_init_shared_resources()` | Orphaned WS manager tasks |
| `app/data_layer.py` | GRVT OB WS: add sleep on `ConnectionClosed` | Tight reconnect loop |
| `app/data_layer.py` | GRVT Position WS: add sleep on `ConnectionClosed` | Tight reconnect loop |
| `app/data_layer.py` | GRVT Position WS: `refresh_cookie()` → `asyncio.to_thread()` | Blocking event loop |
| `app/data_layer.py` | OMS WS: `async for ws` → `async with ws` | Infinite retry on HTTP 400 |
| `app/config.py` | Added `fn_enabled: bool = True` | Kill-switch for legacy engine |
| `app/server.py` | Guard `_fn_engine` startup with `fn_enabled` | Disable legacy single-job engine |
| `app/engine.py` | Guard `stop()` / `apply_config_and_restart_feeds()` against stopping shared DataLayer | Deleting any bot stopped OMS feeds for all bots |
| `app/state_machine.py` | WS fill settle window (300ms + REST confirm) in `_wait_for_maker_fill` | Extended WS delivers fills in rapid batches; first batch triggered premature taker sizing |

---

## Bug 1 — Wrong Extended WebSocket URL

**File:** `app/shared_auth_ws_manager.py`, `_get_websocket_url()`

**Symptom:** `SharedAuthWS[extended]` immediately got HTTP 400 on every connect attempt.

**Root cause:** The URL was set to `/v1/positions` instead of `/v1/account`.

**Fix:**
```python
"extended": "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/account"
```

The `/v1/account` endpoint is the authenticated account stream that receives POSITION, TRADE, ORDER, and BALANCE events. This matches what `extended_client.py` already uses for fill notifications.

---

## Bug 2 — Double `/v1/` in Nado WebSocket URL

**File:** `app/shared_auth_ws_manager.py`, `_get_nado_ws_url()`

**Symptom:** `SharedAuthWS[nado]` got HTTP 400 — URL resolved to `wss://gateway.prod.nado.xyz/v1/v1/subscribe`.

**Root cause:** `nado_client._gateway_rest` is already `"https://gateway.prod.nado.xyz/v1"`. The method appended `/v1/subscribe` instead of `/subscribe`, doubling the `/v1` path segment.

**Fix:**
```python
def _get_nado_ws_url(self) -> str:
    gateway = getattr(self.client, "_gateway_rest", "https://gateway.prod.nado.xyz")
    return gateway.replace("https://", "wss://") + "/subscribe"
```

---

## Bug 3 — Double Initialisation of Shared Resources

**File:** `app/bot_registry.py`, `start_all()`

**Symptom:** Three orphaned `SharedAuthWebSocketManager` tasks (one per exchange) started before the real managers. Each orphaned manager retried with the websockets library's exponential backoff (up to 90 s), causing three 90-second bursts of HTTP 400 errors on every startup.

**Root cause:** `start_all()` called `_init_shared_resources()` unconditionally. If `create_bot()` had already been called before `start_all()` (which is possible in some startup paths), shared resources were initialised twice, leaving the first set of WS tasks dangling.

**Fix:** Guard the call with a None-check:
```python
# Initialize shared resources once (guard against double-init if create_bot ran first)
if self._shared_data_cache is None:
    await self._init_shared_resources()
```

---

## Bug 4 — No Backoff on GRVT OB WebSocket Disconnect

**File:** `app/data_layer.py`, `_run_ob_ws_grvt()`, line ~911

**Symptom:** When GRVT closes the orderbook WS (rate-limit, rolling restart, transient outage), the loop immediately retried without sleeping, producing a tight reconnect spin.

**Root cause:** The `ConnectionClosed` handler logged and fell through to the next loop iteration without any delay. The previous `async for ws in websockets.connect()` pattern had built-in exponential backoff; the replacement `async with websockets.connect()` manual loop did not.

**Fix:**
```python
except websockets.ConnectionClosed:
    logger.warning("DataLayer: GRVT OB WS disconnected: %s — reconnecting in %.0fs",
                   symbol, reconnect_delay)
    await asyncio.sleep(reconnect_delay)
    reconnect_delay = min(reconnect_delay * 2, 30.0)
```

`reconnect_delay` resets to `1.0` inside the `async with` block on a successful connection, so backoff resets after stability is restored.

---

## Bug 5 — No Backoff on GRVT Position WebSocket Disconnect

**File:** `app/data_layer.py`, `_run_pos_ws_grvt()`, line ~1346

**Symptom:** Same tight reconnect spin as Bug 4, but for the authenticated GRVT position stream.

**Fix:** Identical pattern — add sleep and increment `reconnect_delay` in the `ConnectionClosed` branch.

```python
except websockets.ConnectionClosed:
    logger.warning("DataLayer: GRVT position WS disconnected — reconnecting in %.0fs",
                   reconnect_delay)
    await asyncio.sleep(reconnect_delay)
    reconnect_delay = min(reconnect_delay * 2, 30.0)
```

---

## Bug 6 — Blocking `refresh_cookie()` in Async Context

**File:** `app/data_layer.py`, `_run_pos_ws_grvt()`, line ~1278

**Symptom:** The asyncio event loop stalled briefly before every GRVT position WS reconnect attempt. Under high reconnect frequency (e.g. combined with Bug 5), this could delay all other async tasks for the duration of an HTTP round-trip.

**Root cause:** `GrvtCcxt.refresh_cookie()` is a synchronous method that makes a blocking `requests.get()` HTTP call. It was called directly in an async coroutine.

**Fix:**
```python
if api_obj and hasattr(api_obj, "refresh_cookie"):
    try:
        await asyncio.to_thread(api_obj.refresh_cookie)
    except Exception as _ce:
        logger.warning("DataLayer: GRVT position WS cookie refresh failed: %s", _ce)
```

`asyncio.to_thread()` runs the blocking call in a thread-pool worker, leaving the event loop free.

---

## Bug 7 — OMS WebSocket Infinite Retry on HTTP 400

**File:** `app/data_layer.py`, `_run_oms_ws()`, line ~629

**Symptom:** Continuous `! connect failed again; retrying in Xs` log spam with HTTP 400, escalating to 90-second intervals but never stopping. The application-level fallback to HTTP polling was never triggered.

**Root cause:** The function used `async for ws in websockets.connect(ws_url)` — the websockets library's legacy auto-reconnecting iterator. This iterator catches ALL connection failures internally (including `InvalidStatusCode` / HTTP 400), logs them with a traceback, and retries with its own exponential backoff. **It never raises to the application code.** As a result, the application-level `consecutive_failures` counter and the `break`-to-HTTP-poll fallback at `max_failures_before_fallback = 5` were unreachable.

Note: HTTP polling was still working because `add_symbols()` independently starts HTTP poll tasks when `_oms_ws_active` is False at subscription time.

**Fix:** Replace `async for ws in websockets.connect()` with `async with websockets.connect()`. This makes HTTP 400 raise `InvalidStatusCode` directly to the application's `except Exception` handler, so `consecutive_failures` increments correctly and the fallback triggers after 5 attempts.

```python
while self._running:
    try:
        async with websockets.connect(ws_url, close_timeout=5) as ws:
            self._oms_ws = ws
            self._oms_ws_active = True
            reconnect_delay = 1.0
            consecutive_failures = 0
            # ... subscribe, receive/ping tasks ...
    except asyncio.CancelledError:
        self._oms_ws_active = False
        return
    except Exception as exc:
        self._oms_ws_active = False
        consecutive_failures += 1
        logger.warning("DataLayer: OMS WS connection error (%d/%d): %s — retry in %.0fs",
                       consecutive_failures, max_failures_before_fallback, exc, reconnect_delay)
        if consecutive_failures >= max_failures_before_fallback:
            logger.warning("DataLayer: OMS WS failed %d times — falling back to HTTP poll",
                           consecutive_failures)
            break
        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)
```

**Key insight:** `async for ws in websockets.connect()` is convenient for simple reconnect loops but it hides all exceptions from the caller. Use `async with websockets.connect()` whenever the application needs to count failures, apply custom backoff, or trigger a fallback path.

---

## Bug 8 — Legacy `_fn_engine` Competing for Extended WebSocket Slot

**File:** `app/server.py`, `app/config.py`

**Symptom:** After fixing Bugs 1–7, one persistent `! connect failed; retrying in Xs` stream remained — using the `async for ws in websockets.connect()` (legacy iterator) pattern, so the backoff reached 90 s and looped indefinitely.

**Root cause:** `server.py` unconditionally started a legacy single-job `FundingArbEngine` (`job_id=default`, Extended:SOL-USD ↔ GRVT:SOL_USDT_Perp) using hardcoded defaults from `config.py`. This engine created its own `DataLayer` which independently connected to `wss://api.starknet.extended.exchange/stream.extended.exchange/v1/account` — the same endpoint already held by `SharedAuthWS[extended]`. Extended allows only one active session per API key; it kicked the DataLayer's connection, which then retried forever via the `async for ws` iterator.

The engine itself was IDLE (state=IDLE, no open position, trade_count=0) and served no purpose alongside the 10 active BotRegistry bots.

**Fix — `app/config.py`:**
```python
fn_enabled: bool = True   # set False to skip legacy single-job engine startup
```

**Fix — `app/server.py`:**
```python
if _settings.fn_enabled:
    try:
        fn_config = EngineConfig.from_settings(_settings, job_id="default")
        _fn_engine = FundingArbEngine(...)
        await _fn_engine.start()
        ...
    except Exception as exc:
        ...
```

**Deployment — `tradeautonom-v3`:**

Since the container's OS-level environment is set at `docker run` time and cannot be changed via hot-reload, the flag is applied by writing to `/app/.env` (which Pydantic BaseSettings reads on every reload):

```bash
echo 'FN_ENABLED=false' | docker exec -i tradeautonom-v3 tee /app/.env
# Also persisted to /opt/tradeautonom-v3/env.txt for future container restarts
```

---

## Bug 9 — `engine.stop()` Stopping the Shared OMS DataLayer

**Date:** 2026-04-22  
**File:** `app/engine.py`, `stop()` and `apply_config_and_restart_feeds()`

**Symptom:** After deleting a bot (e.g. LIT bots at 20:29:52), all remaining bots (XRP, ADA, etc.) stopped receiving orderbook price updates. The `DataLayer.get_book()` method returned stale data with `age_ms` growing to ~18 minutes. Bots could not enter new chunks because spread calculations failed.

**Root cause:** `FundingArbEngine.stop()` called `self._data_layer.stop()` unconditionally. In BotRegistry mode, every engine's `self._data_layer` is set to the shared OMS DataLayer instance (the same object stored in `BotRegistry._oms_data_layer`). Deleting any one bot triggered `engine.stop()` → `DataLayer.stop()` → all HTTP poll tasks for all symbols were cancelled → no more orderbook data for any bot.

**Fix:** Guard the stop call in both affected methods:
```python
# engine.stop()
if self._data_layer and self._data_layer is not self._oms_data_layer:
    await self._data_layer.stop()

# engine.apply_config_and_restart_feeds() — same guard
if self._data_layer and self._data_layer is not self._oms_data_layer:
    await self._data_layer.stop()
```

The guard relies on the fact that in BotRegistry mode `engine._data_layer` and `engine._oms_data_layer` are the same object (set by `BotRegistry._assign_oms_data_layer()`). In standalone mode `_oms_data_layer` is `None`, so the condition is always True and behaviour is unchanged.

---

## Bug 10 — WS Fill Batch Race: Taker Sized on First Burst Only

**Date:** 2026-04-22  
**File:** `app/state_machine.py`, `_wait_for_maker_fill()`

**Symptom:** ADA bot chunk 2 placed a Variational taker IOC for 398 instead of 7500, leaving a position gap of 7102. The mandatory verify detected the imbalance and a repair order was triggered. The same pattern was observed across multiple chunks and bots (BNB, LTC, etc.) — `chunk_gap=0` but `pos_gap>0` — indicating the state machine consistently credited equal maker and taker fills (both too small), while the exchange showed a larger maker position.

**Root cause:** Extended exchange's matching engine fills orders atomically, but delivers WS `TRADE` events in multiple rapid bursts for the same order. Example for ADA chunk 2:

```
T+0ms:   WS events: qty=300 + qty=98  → accumulated ws_qty = 398
T+2ms:   _wait_for_maker_fill(): ws_qty=398 > 0 → return {"filled": True, "traded_qty": 398}
T+2ms:   _execute_single_chunk(): if filled.get("filled"): → maker_filled_qty=398 → taker IOC placed for 398
T+254ms: WS event: qty=7102 arrives — too late, taker already in flight
T+1500ms: REST verify: Extended=7500, Variational=398, gap=7102 → REPAIR TRIGGERED
```

`_wait_for_maker_fill` returned `{"filled": True}` on the first WS fill event, routing the caller into the `if filled.get("filled"):` branch which breaks immediately to the taker hedge — bypassing the partial-fill path that had the existing 0.3s recheck. The partial-fill path (with recheck) was only reached when `filled=False` (REST-detected partial fills).

**Fix:** Add a 300ms settle window + REST confirmation inside the WS detection path of `_wait_for_maker_fill`:

```python
ws_qty = self._get_ws_filled_qty(oid)
if ws_qty > 0:
    # Settle window: Extended delivers fills in rapid batches
    await asyncio.sleep(0.3)
    ws_qty = self._get_ws_filled_qty(oid)  # collect stragglers
    try:
        rest_result = await client.async_check_order_fill(oid)
        rest_qty = float(rest_result.get("traded_qty", 0))
        if rest_qty > ws_qty:
            ws_qty = rest_qty  # REST is authoritative
    except Exception:
        pass
    return {"filled": True, "traded_qty": ws_qty}
```

Applied to both the in-loop WS check and the post-deadline final check.

**Why 300ms is safe:** The TWAP interval between chunks is 10 seconds. A 300ms settle adds <3% to per-chunk latency and eliminates repair orders caused by fill batch races. Repair orders are more expensive (additional IOC fee + latency) than the settle delay.

**Key design rule:** `_wait_for_maker_fill` must never return a WS-detected fill amount without confirming it is complete. Exchanges that batch-deliver fills (Extended, potentially others) require a settle window before the accumulated WS total can be trusted as the final filled quantity.

---

## Deployment Notes

All instances use a bind-mount from `/opt/tradeautonom-v3/app/` — `scp` the changed files to the NAS path and uvicorn's `--reload` picks them up within ~3 seconds without a container restart.

```bash
scp app/engine.py app/state_machine.py root@192.168.133.100:/opt/tradeautonom-v3/app/
# uvicorn reloads in ~3s automatically
```

For Bugs 1–8 (earlier refactor), files were deployed via `docker cp` into the running container before the bind-mount migration.

---

## Verification

After all fixes, a clean startup shows:

```
SharedAuthWS[grvt]: Connected
SharedAuthWS[extended]: Connected
SharedAuthWS[nado]: Connected
DataLayer: OMS WS connected to ws://192.168.133.100:8099/ws
BotRegistry started: 10 bots (10 restored)
Application startup complete.
```

No `! connect failed` entries appear in steady-state logs. OMS orderbook data flows via WebSocket (or HTTP poll fallback if the `/ws` endpoint is unavailable). GRVT position/orderbook WS reconnects with exponential backoff (1 s → 2 s → … → 30 s max) rather than spinning tight.
