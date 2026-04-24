# V2 OMS — Cloudflare-native architecture

Status: **Phase A deployed** (Extended + AggregatorDO + /ws bot protocol).
Live: `https://oms-v2.defitool.de` — source under `deploy/cf-containers/oms-v2/`.

Remaining phases: B (GRVT + Nado + Variational + auto-discovery),
C (ArbScannerDO + /ws/arb DNA-bot protocol), D (canary rollout).

## What OMS does today (V1, Python, Photon VM)

See `deploy/monitor/monitor_service.py` and `docs/V5_OMS_AND_FEATURES.md`.

Three concerns in one process:

1. **Ingestion** — persistent outbound WebSocket to each exchange:
   - Extended: single shared `wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks` (no market param, server pushes all markets)
   - Nado: `wss://gateway.prod.nado.xyz/v1/subscribe` (EIP-712 auth)
   - GRVT: `wss://market-data.grvt.io/ws/full`
   - Variational: REST polling via `proxy.defitool.de` (Cloudflare TLS-bypass)
2. **Aggregation** — in-memory orderbook per `(exchange, symbol)` with delta merging, top-N levels, auto-discovery, symbol normalization (Nado x18, k-prefix/1000x, bid/ask inversion for ZRO/ZEC/XMR).
3. **Distribution** — subscriber WebSocket on `/ws` + REST `/book/<exchange>/<symbol>`; bot clients subscribe to `(exchange, symbol)` pairs and receive delta-compressed pushes.

Scale: ~317 feeds across 114 base tokens.

## Why a Pure Durable Objects rewrite (not a CF Container)

Original V2 plan had OMS running as a CF Container (Python copy). That was rejected in favor of a full TypeScript rewrite on Durable Objects. Reasons:

- **Fewer hops**: Worker → DO directly, vs. Worker → DO → Container.
- **Native SQLite** per DO (128 MB): no ephemeral-disk problem.
- **No container runtime overhead** (no tini, no Docker registry, no image pulls).
- **Modern deploy pipeline**: `wrangler deploy` vs. `docker build + push`.
- **Better observability**: Worker/DO logs in Cloudflare dashboard natively.

Trade-offs accepted:

- **Full rewrite** of ~2000+ lines of Python (Nado x18, Extended delta-merge, symbol normalization, auto-discovery) into TypeScript.
- **`curl_cffi`** (Variational's Cloudflare-TLS-bypass) is unavailable in Workers; `VariationalOms` DO delegates to the existing `proxy.defitool.de` Worker.
- **Outgoing WebSocket prevents DO hibernation.** Exchange DOs will run 24/7, billed as GB-s. No hibernation savings on the ingestion side. The only component that benefits from hibernation is the `AggregatorDO` (bot-subscriber fan-out), which has no outbound connections.

## Architecture

```
Bot clients (V1 user container, V2 user container, tradeautonom-v3)
        │  ws://oms-v2.defitool.de/ws
        │  https://oms-v2.defitool.de/book/<exch>/<sym>
        ▼
┌────────────────────────────────────────────────────────────┐
│  OMS Worker (stateless entrypoint)                         │
│  - Upgrade check for WebSocket                             │
│  - Forward /ws to AggregatorDO                             │
│  - Forward /book, /status, /tracked to AggregatorDO        │
└──────────────────────┬─────────────────────────────────────┘
                       │ fetch() / RPC
                       ▼
┌────────────────────────────────────────────────────────────┐
│  AggregatorDO (singleton: idFromName("aggregator"))        │
│  - Holds bot-subscriber WebSockets (hibernation-enabled)   │
│  - Maps (exchange, symbol) → set of subscribers            │
│  - On subscribe: asks the right ExchangeOms DO for book    │
│  - On book update from ExchangeOms: fans out to subs       │
└──────┬──────────┬──────────┬──────────┬────────────────────┘
       │RPC       │RPC       │RPC       │RPC
       ▼          ▼          ▼          ▼
┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────────────┐
│Extended  │ │  Nado    │ │  Grvt    │ │ Variational    │
│Oms DO    │ │  Oms DO  │ │  Oms DO  │ │ Oms DO         │
│          │ │          │ │          │ │                │
│out WS    │ │out WS    │ │out WS    │ │REST poll via   │
│+SQLite   │ │+SQLite+  │ │+SQLite   │ │proxy.defitool  │
│          │ │watchdog  │ │          │ │+SQLite         │
└────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬───────────┘
     │            │            │            │
     ▼            ▼            ▼            ▼
Extended WS   Nado WS      GRVT WS      proxy.defitool.de
api.starknet  gateway.prod market-data  /api/metadata/stats
```

Singleton DOs:

- `AggregatorDO` — always one instance (`idFromName("aggregator")`)
- `ExtendedOms`, `NadoOms`, `GrvtOms`, `VariationalOms` — always one each (`idFromName("singleton")`)

This is a deliberate choice: the exchanges emit global state, so sharding by symbol adds complexity without obvious benefit at current scale (~317 feeds). Can be reconsidered later.

## Exchange DO pattern

Each `<Exchange>Oms` DO follows the same pattern:

```ts
import { DurableObject } from "cloudflare:workers";

export class ExtendedOms extends DurableObject<Env> {
  private ws: WebSocket | null = null;
  private books: Map<string, Orderbook> = new Map();
  private reconnectAttempt = 0;

  constructor(state: DurableObjectState, env: Env) {
    super(state, env);
    // Alarm pattern: ensure WS is open whenever DO is alive.
    state.blockConcurrencyWhile(async () => {
      await this.ensureWs();
      await this.state.storage.setAlarm(Date.now() + 30_000);
    });
  }

  // RPC called by AggregatorDO
  async getBook(symbol: string): Promise<Orderbook | null> {
    return this.books.get(symbol) ?? null;
  }

  // RPC called by AggregatorDO when a new bot subscribes
  async trackSymbol(symbol: string): Promise<void> {
    // Extended sends all markets on one WS; we just ensure the map has an entry
    if (!this.books.has(symbol)) {
      this.books.set(symbol, { bids: [], asks: [], ts: 0, connected: false });
    }
  }

  async alarm() {
    await this.ensureWs();
    await this.state.storage.setAlarm(Date.now() + 30_000);
  }

  private async ensureWs() {
    if (this.ws?.readyState === WebSocket.OPEN) return;

    // Public stream — no auth. `User-Agent` is the only required header.
    // https://api.docs.extended.exchange/#order-book-stream
    const resp = await fetch(
      "https://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks",
      {
        headers: {
          Upgrade: "websocket",
          "User-Agent": "tradeautonom-oms-v2/0.1",
        },
      },
    );
    if (resp.status !== 101) {
      console.error("Extended WS upgrade failed", resp.status);
      return;
    }
    this.ws = resp.webSocket!;
    this.ws.accept();
    this.ws.addEventListener("message", (e) => this.onMessage(e.data));
    this.ws.addEventListener("close", (e) => this.onClose(e));
    this.reconnectAttempt = 0;
  }

  private onMessage(raw: string | ArrayBuffer) {
    const msg = JSON.parse(typeof raw === "string" ? raw : new TextDecoder().decode(raw));
    // Extended sends either "SNAPSHOT" or "DELTA". Apply to in-memory book.
    const symbol: string = msg.m;
    const book = this.books.get(symbol) ?? { bids: [], asks: [], ts: 0, connected: true };
    applyDelta(book, msg);  // orderbook.ts helper
    book.ts = Date.now();
    book.connected = true;
    this.books.set(symbol, book);

    // Notify AggregatorDO for fanout
    const agg = this.env.AGGREGATOR_DO.get(
      this.env.AGGREGATOR_DO.idFromName("aggregator"),
    );
    agg.onExchangeUpdate("extended", symbol, book).catch(() => {});
  }

  private onClose(e: CloseEvent) {
    this.ws = null;
    // Exponential backoff in ensureWs() triggered by the next alarm
  }
}
```

Key decisions:

- **In-memory books** (Map). Not SQLite. SQLite is used only for the durable state (last-known-good snapshot, reconnect epoch) via `blockConcurrencyWhile`-restored fields. Hot orderbook data does not round-trip to SQLite.
- **Alarm every 30s** to reconnect if WS is down. Outgoing WS keeps the DO warm, so this is mostly redundant, but guarantees recovery from eviction (e.g. rare host restart).
- **RPC to AggregatorDO** for every book update. This is the fan-out point. To limit RPC volume, we can batch within a short window (5-50 ms) if needed; PoC will measure.

## AggregatorDO pattern (with Hibernation)

```ts
export class AggregatorDO extends DurableObject<Env> {
  async fetch(req: Request): Promise<Response> {
    const url = new URL(req.url);
    if (url.pathname === "/ws") {
      return this.handleWebSocketUpgrade(req);
    }
    if (url.pathname.startsWith("/book/")) {
      return this.handleBookRequest(url);
    }
    // ...
    return new Response("Not found", { status: 404 });
  }

  private async handleWebSocketUpgrade(req: Request): Promise<Response> {
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    this.state.acceptWebSocket(server);   // HIBERNATION-compatible
    return new Response(null, { status: 101, webSocket: client });
  }

  // Hibernation-compatible message handler
  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer) {
    const msg = JSON.parse(typeof message === "string" ? message : "");
    if (msg.action === "subscribe") {
      await this.addSubscription(ws, msg.exchange, msg.symbol);
      ws.send(JSON.stringify({ type: "subscribed", exchange: msg.exchange, symbol: msg.symbol }));
    }
    // ...
  }

  // Called by ExchangeOms DO via RPC
  async onExchangeUpdate(exchange: string, symbol: string, book: Orderbook): Promise<void> {
    const key = `${exchange}:${symbol}`;
    const payload = JSON.stringify({
      type: "book",
      exchange, symbol,
      bids: book.bids.slice(0, 20),
      asks: book.asks.slice(0, 20),
      timestamp_ms: book.ts,
    });
    // Fan out to all subscribers of this (exchange, symbol)
    const subs = await this.getSubsFor(key);
    for (const ws of subs) {
      ws.send(payload);
    }
  }
}
```

Hibernation works here because AggregatorDO has no outbound sockets. When no exchange updates and no new bot messages arrive, the DO evicts from memory while keeping all bot WebSockets connected.

## Variational DO — REST polling

Variational is the only exchange that does not provide a useful WebSocket market-data stream. The existing Python OMS polls `https://omni.variational.io/api/metadata/stats` every 5s and generates synthetic books. Variational is also behind Cloudflare with TLS-fingerprint detection, which is why `curl_cffi` is mandatory — and why `fetch()` from a Worker/DO will not work directly.

V2 solution: `VariationalOms` DO polls the existing `https://proxy.defitool.de/api/metadata/stats` endpoint. That proxy is a Worker that forwards to Variational with the right TLS fingerprint via a runtime path we already maintain.

```ts
export class VariationalOms extends DurableObject<Env> {
  constructor(state: DurableObjectState, env: Env) {
    super(state, env);
    state.blockConcurrencyWhile(async () => {
      await state.storage.setAlarm(Date.now() + 5_000);
    });
  }

  async alarm() {
    await this.pollStats();
    await this.state.storage.setAlarm(Date.now() + 5_000);
  }

  private async pollStats() {
    const resp = await fetch("https://proxy.defitool.de/api/metadata/stats", {
      headers: { "X-Internal-Auth": this.env.PROXY_AUTH_TOKEN },
    });
    const stats: Record<string, any> = await resp.json();
    // Parse stats into synthetic Orderbook per symbol
    for (const [symbol, s] of Object.entries(stats)) {
      this.books.set(symbol, toOrderbook(s));
    }
    // Notify aggregator
    // ...
  }
}
```

## Bot-client protocol — unchanged

We keep exact wire compatibility with the V1 Python OMS so that `app/data_layer.py::_run_oms_ws` can subscribe to either V1 or V2 OMS via env var `FN_OPT_SHARED_MONITOR_URL`:

- `GET /health` → `{ status: "ok" }`
- `GET /status` → `{ "<exch>:<sym>": { connected, has_data, age_ms, updates, bid_levels, ask_levels } }`
- `GET /tracked` → `{ <base>: { <exch>: <sym> } }`
- `GET /book/<exch>/<sym>` → `{ exchange, symbol, bids, asks, timestamp_ms, connected, updates }`
- WebSocket `/ws`:
  - Client → `{ action: "subscribe", exchange, symbol }`
  - Server → `{ type: "subscribed", exchange, symbol }`
  - Server → `{ type: "book", exchange, symbol, bids, asks, timestamp_ms }` (ongoing)
  - Client → `{ action: "unsubscribe", exchange, symbol }`

## Secrets

OMS-v2 needs credentials where the exchange stream actually requires auth.
Extended's **order book stream is public** — no secret required.

- Extended: no secret (public order book stream). If we later add the private
  account/fill WS, an `X-Api-Key` would be needed for that stream only.
- Nado: private key / linked signer key for EIP-712 signing of the subscribe frame.
- GRVT: credentials for the authenticated trades WS if/when used.
- `PROXY_AUTH_TOKEN` for `proxy.defitool.de` (Variational).

Stored as Worker secrets via `wrangler secret put`, accessed in DO constructors.

## Deployment

- Subdomain: `oms-v2.defitool.de`
- Wrangler project: `deploy/cf-containers/oms-v2/`
- `wrangler deploy` — no Docker build required
- Rollout: new V2 users get `FN_OPT_SHARED_MONITOR_URL=https://oms-v2.defitool.de`. V1 users keep `http://192.168.133.100:8099`.
- Canary: enable OMS-v2 for one V2 test user first, run alongside V1-OMS with identical subscribes, compare `/book/*` outputs.

## Open technical questions

Resolved by PoC (Phase 0):

1. **Outbound WS from a DO.** Does `fetch(url, { headers: { Upgrade: "websocket" } })` actually deliver a usable `WebSocket` back? (Extended's order book stream is public, so no auth-header question for Extended. For Nado, subscribe frames are EIP-712-signed after connect, not via headers.)
2. **Outbound WS lifetime.** How long does a DO live with an open outbound WS but no inbound requests? Cloudflare docs say "outbound WebSockets do not hibernate" — verify this is honored in practice.
3. **RPC cost.** GB-s billing per RPC call vs. internal fetch()? Determines whether to batch updates.
4. **Hibernation with partial state.** Does `acceptWebSocket` still work if we have per-connection attachments > 2 KB? (Subscription lists could grow.)
5. **Nado EIP-712 in `ethers.js`.** Verify that `_signTypedData` produces signatures Nado accepts.

## Non-goals (for now)

- **Multi-region OMS.** One DO singleton per exchange globally. Cloudflare picks placement near the exchange endpoint. If latency to a particular exchange becomes a bottleneck, revisit with regional DOs.
- **Historical orderbook storage.** Out of scope. Journaling stays in `journal_collector.py` on the user container side.
- **Symbol sharding.** Each ExchangeOms DO handles all symbols. Current scale (~100 base tokens) does not justify sharding.
- **Broadcast DO split.** Considered but punted: introducing a separate fan-out DO in front of AggregatorDO adds latency and complexity for a marginal hibernation gain. Revisit if AggregatorDO becomes a bottleneck.

## Cross-references

- `docs/v2-cf-containers-architecture.md` — overall V2 plan, rollout phases, routing
- `docs/V5_OMS_AND_FEATURES.md` — V1 OMS documentation (current behavior)
- `app/data_layer.py::_run_oms_ws` — client code that consumes OMS (compatibility target)
- `deploy/monitor/monitor_service.py` — V1 OMS reference implementation
- Cloudflare docs:
  - https://developers.cloudflare.com/durable-objects/best-practices/websockets/
  - https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/
  - https://developers.cloudflare.com/containers/ (for UserContainer-v2, not for OMS-v2)
