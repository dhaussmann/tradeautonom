# V2 OMS — Cloudflare-native architecture

Status: **Phase E complete**. Live: `https://oms-v2.defitool.de`.

Source under `deploy/cf-containers/oms-v2/`.

Working:
- Extended (114 markets, all-markets shared stream)
- GRVT (90 markets, single subscribe frame, snapshot-style updates)
- Variational (97 markets, 1.2s REST polling, 3-tier synthetic book)
- Nado (52 markets via `NadoRelayContainer` — a tiny Node.js Cloudflare
  Container that holds the `permessage-deflate` WebSocket to Nado and
  forwards events to `NadoOms` DO via a plain WebSocket on port 8080).
- AggregatorDO (hibernation WS, V1 bot protocol, quote endpoints)
- ArbScannerDO (event-driven cross-exchange arb; V1-compatible `/ws/arb`)
- Phase E bot-entry enrichment: `/meta`, `/quote`, `/quote/cross`,
  enriched `{type:"book"}` with mid + cumsum, WS `quote`/`quote_cross`
  subscriptions.
- Auto-discovery cron (every 15 min) — 117 cross-exchange token pairs

Remaining phases: D (canary rollout of one V1 bot against oms-v2).

## Phase C — Cross-exchange arbitrage scanner

The `ArbScannerDO` is a separate Durable Object that receives book updates
from every ExchangeOms in parallel with `AggregatorDO` (both targets get
`Promise.allSettled([agg.onBookUpdate, scanner.onBookUpdate])`). It
serves two consumer surfaces:

### `/ws/arb` — DNA-bot WebSocket

Wire protocol matches V1 Photon OMS byte-for-byte
(`deploy/monitor/monitor_service.py` lines 824-927 and 958-1003) so that
`app/dna_bot.py` can switch from Photon OMS to V2 OMS simply by flipping
`oms_url` config.

Client → server:
- `{"action":"watch","token":"SOL","buy_exchange":"extended","sell_exchange":"grvt"}`
- `{"action":"unwatch", ...same fields...}`
- `{"action":"subscribe_opportunities","min_profit_bps":10,"exchanges":["extended","grvt"]}`
- `{"action":"unsubscribe_opportunities"}`

Server → client:
- `{"type":"arb_opportunity", ...21 fields matching V1 _arb_opp_to_dict...}`
- `{"type":"arb_status", token, buy_exchange, sell_exchange, buy_ask, sell_bid, spread_bps, fee_threshold_bps, profitable, timestamp_ms}`
- `{"type":"arb_close", ...same fields..., reason:"spread_below_fees"}`
- `{"type":"watching", token, buy_exchange, sell_exchange, has_data:false}` when no book data yet

### `/arb/*` — REST

- `GET /arb/opportunities` — current opportunities, sorted by `net_profit_bps` desc
- `GET /arb/opportunities?token=BTC` — filter by base token
- `GET /arb/opportunities?min_profit_bps=5` — live re-scan with custom threshold
  (used by DNA-bot half-neutral/custom spread modes)
- `GET /arb/config` — scanner config (exchanges, fees, thresholds)
- `GET /arb/health` — tokens_tracked, books_cached, last_scan_ms, totals

### Key V1 parity constants

Ported verbatim from `monitor_service.py` to `src/lib/arb.ts`:
- `TAKER_FEE_PCT = { extended: 0.0225, nado: 0.035, grvt: 0.039, variational: 0.04 }`
- `ARB_FEE_BUFFER_BPS = 1.0`
- `ARB_MAX_NOTIONAL_USD = 50_000`
- `ARB_EXCHANGES = { extended, grvt, nado }` (Variational excluded per V1 default)
- `ARB_EXCLUDED_TOKENS = { WTI, MEGA, AMZN, AAPL, TSLA, HOOD, META, USDJPY }`
- Binary-search quantity finder: 12 iterations, `min_qty=0.001`
- `_min_profit_bps(a,b) = (fee_a + fee_b) * 2 * 100 + buffer_bps`

### Event-driven recomputation (instead of V1's 200ms poll loop)

V1 Photon runs `_scan_arbitrage` on a fixed `OMS_ARB_SCAN_INTERVAL_S=0.2`
timer. V2 recomputes opportunities for a single token the moment any
exchange pushes a fresh book for that token — latency is bounded by the
slowest leg's book freshness plus one RPC hop (~5 ms). Each update also
triggers `notifyWatchers(snap)` for real-time per-position spread pushes.

### Throughput observations (initial deploy)

- 4,975 scans in ~4 minutes after deploy (event-driven, all 4 exchanges
  fanning out)
- 349 books cached across 117 tokens
- 3-9 actionable opportunities at any time (varies with market)
- Probe with `subscribe_opportunities` + 2 watches: 14 opportunity pushes,
  2 `arb_status`, 450 `arb_close` in 20 s (`arb_close` fires on every
  relevant book update of a non-profitable watched position; matches V1)

## The Nado deflate problem and why we use a container

Nado's subscription gateway (`wss://gateway.prod.nado.xyz/v1/subscribe`)
REQUIRES `Sec-WebSocket-Extensions: permessage-deflate` in the upgrade
request. This is documented explicitly
(<https://docs.nado.xyz/developer-resources/api/subscriptions>) and
enforced server-side — a connection attempt without it returns HTTP 403:

```json
{
  "reason": "Invalid compression headers: 'Sec-WebSocket-Extensions' must include 'permessage-deflate'",
  "block": true
}
```

Cloudflare Workers' outbound WebSocket client (via
`fetch(url, { headers: { Upgrade: "websocket" } })`) does NOT advertise
or negotiate any `Sec-WebSocket-Extensions`, so the upgrade is rejected
and the Worker never sees any Nado frame.

Options considered:
  1. **Raw TCP + TLS + WebSocket framing + DEFLATE implemented in the
     Worker** using `cloudflare:sockets`. Rejected — 500+ LOC including
     RFC 7692 sliding-window semantics, high bug risk, ongoing maintenance.
  2. **Python relay on Photon VM (`192.168.133.100`)**. Rejected —
     reintroduces a Photon host in the V2 data path; contradicts the
     CF-native goal.
  3. **Cloudflare Container holding the upstream WebSocket with deflate**.
     Chosen. Small Node.js service using the `ws` library (native
     `permessage-deflate` support). Stays inside Cloudflare. Same
     deploy pipeline as the Worker (`wrangler deploy`).
  4. Snapshot-only (no WebSocket). Rejected — 5-second polling staleness
     breaks cross-exchange arbitrage detection.

## Nado data flow

```
                  ┌──────────────────────────────────────┐
                  │  Cloudflare Worker (oms-v2)          │
                  │                                      │
                  │  NadoOms DO                          │
                  │  • REST /query snapshot per product  │
                  │  • book state + delta merge          │
                  │  • seq-gap detection                 │
                  │  • fan-out to AggregatorDO           │
                  │                                      │
                  │         WebSocket ▼                  │
                  │                                      │
                  │  NadoRelayContainer (CF Container)   │
                  │  • Node.js 20 + `ws`                 │
                  │  • 1 upstream WS with deflate        │
                  │  • 30s ping (required by Nado)       │
                  │  • auto-reconnect + re-subscribe     │
                  │  • forwards raw JSON events          │
                  └──────────────┬───────────────────────┘
                                 │ wss:// + permessage-deflate + 30s ping
                                 ▼
                    wss://gateway.prod.nado.xyz/v1/subscribe
```

Container is stateless w.r.t. books — all book-state, delta merging, and
fan-out remain in `NadoOms` DO, matching the pattern used for the other
three exchanges. The container is a "dumb transport" whose sole job is
to overcome the deflate-negotiation limitation.

### Relay protocol (DO ↔ Container, internal WebSocket)

DO → Container (control):
  - `{op:"subscribe", product_id:N}` — add one product to upstream
  - `{op:"unsubscribe", product_id:N}` — remove one product
  - `{op:"resubscribe_all", product_ids:[N1,N2,...]}` — replace the full
    tracked set (sent on fresh DO-to-container connect)

Container → DO (events):
  - `{type:"hello", relay_version, started_at_ms}` — sent on DO connect
  - `{type:"upstream_connected", at_ms}` — upstream to Nado opened
  - `{type:"upstream_disconnected", at_ms, reason}` — upstream lost;
    container is auto-reconnecting in the background
  - `{type:"event", at_ms, event:<raw Nado JSON>}` — forwarded book_depth
    envelope from Nado with x18 price/size strings preserved

### Container operational notes

- `max_instances: 1` — one relay, one upstream WS. Prevents fan-out race.
- `sleepAfter: "14d"` — the relay is a long-lived streamer, not on-demand.
- `instance_type: "basic"` — near-idle (inflate + forward); 256 MB is ample.
- Source: `deploy/cf-containers/oms-v2/container/nado-relay/`
- Worker binding class: `NadoRelayContainer`
  (`deploy/cf-containers/oms-v2/src/nado-relay-container.ts`)
- `wrangler.jsonc` — see `containers:[...]` + migration tag `v3`
- Health: container `GET /health` returns `{upstream:{connected,...}}`.
  DO `/nado/health` exposes `relay_state`, `upstream_connected`,
  `last_event_ms` for full-path visibility.

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

## Phase E — Bot-entry enrichment

Phase E moves every orderbook-derived calculation that V1 bots do at entry
into OMS, so each bot's entry logic reduces to: "receive an opportunity →
call /quote/cross → place orders at the returned limit prices." Live on
`oms-v2.defitool.de` at deploy `127c7992`.

### Static per-symbol meta: `/meta`

```
GET /meta                        → all 323 symbols' static meta
GET /meta/:exchange              → per-exchange subset
GET /meta/:exchange/:symbol      → one symbol
```

Per-symbol fields:
- `tick_size` — sourced from each exchange's metadata API at discovery time:
  Extended `tradingConfig.minPriceChange`, GRVT `tick_size`,
  Nado `price_increment_x18 / 1e18`. Variational publishes no tick.
- `min_order_size`, `qty_step`, `max_leverage` — also from discovery
- `min_notional_usd` — USD-notional floor, **non-null only for Nado**
  (Nado publishes `min_size` as USD notional; Extended/GRVT/Variational
  publish base-qty mins and report `null` here). See the Nado notional
  section below.
- `taker_fee_pct` — `TAKER_FEE_PCT` constants (same table as `/arb/config`)
- `maker_fee_pct` — reserved (null for now)
- `funding_interval_s` — 3600 for Extended/GRVT/Nado; inferred from
  symbol format for Variational (`P-{TICKER}-USDC-{interval_s}`)

Coverage after discovery run (2026-04-24):
- extended: 84/84 with tick_size ✓
- grvt: 90/90 with tick_size ✓
- nado: 52/52 with tick_size ✓ + 52/52 `min_notional_usd` = 100 USD
- variational: 0/97 (API does not publish tick)

### Nado `min_size` is USD notional (Phase F)

Nado's `/symbols` API publishes `min_size` as a **USD-notional floor**, not a
base-qty threshold — unlike Extended/GRVT/Variational which publish real
base-qty mins. The raw value for every Nado perp is `100` (i.e. $100
minimum notional). V1's `app/nado_client.py::get_min_order_size` handles
this via `ceil(notional / mid / step) * step`; V2 OMS now does the same.

Discovery now stores two separate fields for Nado:
- `min_order_size = size_increment` (the true base-qty tick, e.g. 0.00005 BTC)
- `min_notional_usd = 100` (the USD floor)

`computeQuote` (and `findArbForToken` for `/arb/opportunities`) compute
an **effective base-qty min** at evaluation time using the live book's
mid price:

```
effMinQty = max(
  minOrderSize,
  minNotionalUsd > 0 && midPrice > 0
    ? ceil(minNotionalUsd / midPrice / qtyStep) * qtyStep
    : 0
)
```

Cold-start fallback (no book mid yet): skip the notional conversion,
use `minOrderSize` only → never false-reject.

All `Quote.min_order_size` and `ArbOpportunity.buy/sell_min_order_size`
fields now carry the **effective** value (not the raw Nado `100`). Wire-
compatible with V1 DNA-bot `_harmonize_qty`.

### Single-leg quote: `/quote/:exchange/:symbol`

```
GET /quote/grvt/BTC_USDT_Perp?side=buy&qty=0.05
GET /quote/extended/BTC-USD?side=sell&notional_usd=5000&buffer_ticks=2
```

Replaces every call to `app/safety.py::walk_book`,
`estimate_fill_price`, `check_order_book_depth`, `check_book_quantity`,
and `app/arbitrage.py::_compute_vwap_limit`.

Response includes:
- `fillable_qty`, `unfilled_qty` — feasibility
- `vwap`, `best_price`, `worst_price` — fill stats
- `slippage_bps_vs_best`, `slippage_bps_vs_mid` — pre-trade slippage
- `notional_usd` — total USD cost of the sweep
- `limit_price_with_buffer` — `worst_price ± buffer_ticks*tick_size`
  (BUY adds buffer above worst; SELL uses worst exactly — matches V1
  `_compute_vwap_limit` semantics)
- `harmonized_qty` — requested qty rounded down to `qty_step`
- `feasible: bool` + `feasibility_reason` (`no_book`, `book_stale`,
  `book_disconnected`, `empty_side`, `missing_size_input`,
  `qty_below_step`, `qty_below_min_order_size`, `insufficient_depth`)
- Static meta repeated in response (tick_size, fees, step) so a bot
  can skip the `/meta` call

Input: exactly one of `qty` (base units) or `notional_usd`. If
`notional_usd`, OMS derives qty = `notional_usd / mid_price`.

### Cross-venue arb quote: `/quote/cross`

```
GET /quote/cross?token=BTC&buy_exchange=grvt&sell_exchange=extended&notional_usd=5000
```

Replaces `app/spread_analyzer.py::analyze_cross_venue_spread` and
`app/dna_bot.py::_harmonize_qty`. Fetches both books, harmonises qty to
the coarser `qty_step`, runs `computeQuote` on each leg, computes:
- `bbo_spread_bps` — top-of-book spread
- `exec_spread_bps` — VWAP execution spread at harmonized qty
- `slippage_bps_over_bbo` — `exec - bbo` (extra cost vs best)
- `fee_threshold_bps` — same per-pair fee sum as `/arb/config`
- `net_profit_bps_after_fees` — `exec_spread_bps - fee_threshold_bps`
- `profitable: bool`
- `min_order_size_binding` — which exchange's step was the coarser one
- Both legs returned as full `Quote` objects (per-leg `limit_price_with_buffer`
  is what the bot passes to `create_limit_order`)

### Enriched `{type:"book"}` WebSocket pushes

Every book push now also carries:
- `mid_price`
- `bid_qty_cumsum: number[]`, `ask_qty_cumsum: number[]`
- `bid_notional_cumsum: number[]`, `ask_notional_cumsum: number[]`

Index `i` in each cumsum array is the cumulative sum across levels `0..i`.
Cheap (O(depth)=10 adds per push); bots never need to cumsum themselves.

The `{type:"subscribed"}` ACK also now carries `meta: SymbolMeta | null`
so that a subscriber sees tick/step/fee once at subscribe time and never
needs a separate `/meta` round-trip.

### WS quote subscriptions

On the same `/ws` connection a bot can also subscribe to live quote
pushes that refresh on every relevant book update (coalesced to at most
10/sec per subscription):

```
Client → Server: {action:"quote", exchange, symbol, side,
                  qty?, notional_usd?, buffer_ticks?}
Client → Server: {action:"quote_cross", token, buy_exchange, sell_exchange,
                  qty?, notional_usd?, buffer_ticks?}
Client → Server: {action:"unquote", ...}
Client → Server: {action:"unquote_cross", ...}

Server → Client: {type:"quote", ...full Quote fields...}
Server → Client: {type:"quote_cross", ...full CrossQuote fields...}
```

Per-connection cap: 50 quote subscriptions. Throttle:
`QUOTE_PUSH_MIN_INTERVAL_MS = 100` between pushes per subscription.

### What V1 bots can delete after adopting Phase E

- `app/safety.py::walk_book`, `estimate_fill_price`,
  `check_order_book_depth`, `check_book_quantity`, `check_dual_liquidity`
  → replaced by `/quote` fields
- `app/spread_analyzer.py::analyze_cross_venue_spread`
  → replaced by `/quote/cross` fields
- `app/arbitrage.py::_compute_vwap_limit`
  → replaced by `Quote.limit_price_with_buffer`
- `app/dna_bot.py::_harmonize_qty`
  → replaced by `Quote.harmonized_qty`
- `client.get_tick_size()` hot-path calls
  → replaced by `subscribed` ACK meta or `/meta/:exch/:sym`

Bot entry path after full adoption:
1. Receive `arb_opportunity` on `/ws/arb`
2. `GET /quote/cross?token=T&buy_exchange=A&sell_exchange=B&notional_usd={config.position_size_usd}`
3. If `feasible && profitable`, `create_limit_order` on each leg in
   parallel using `buy.limit_price_with_buffer` / `sell.limit_price_with_buffer`
   with `harmonized_qty`

## Cross-references

- `docs/v2-cf-containers-architecture.md` — overall V2 plan, rollout phases, routing
- `docs/V5_OMS_AND_FEATURES.md` — V1 OMS documentation (current behavior)
- `app/data_layer.py::_run_oms_ws` — client code that consumes OMS (compatibility target)
- `deploy/monitor/monitor_service.py` — V1 OMS reference implementation
- Cloudflare docs:
  - https://developers.cloudflare.com/durable-objects/best-practices/websockets/
  - https://developers.cloudflare.com/workers/runtime-apis/tcp-sockets/
  - https://developers.cloudflare.com/containers/ (for UserContainer-v2, not for OMS-v2)
