# OMS-v2 Proof of Concept

Minimal TypeScript implementation of a Cloudflare-native `ExtendedOms` Durable Object.
Goal: validate the **open technical questions** from `docs/v2-oms-cloudflare-native.md`
before committing to the full OMS-v2 rewrite.

**This is a research artifact, not a production path.** Do not deploy to a route that real
bot clients point at.

## What this PoC does

- Spins up a single `ExtendedOms` DO singleton.
- Opens an outbound WebSocket to the **public** Extended order book stream:
  `wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks/BTC-USD`.
  Per the [Extended docs](https://api.docs.extended.exchange/#order-book-stream), this
  stream requires no authentication — only a `User-Agent` header.
- Parses incoming `SNAPSHOT` and `DELTA` messages following the documented schema
  (envelope `{ts, type, data: {m, b, a}, seq}`, per-level `{p, q, c}` where `c` is the
  absolute size).
- Tracks sequence numbers. On a gap, closes and reconnects.
- Stores the top-20 bid/ask levels in memory.
- Exposes two HTTP endpoints:
  - `GET /book/BTC-USD` → current bids/asks + lag metrics
  - `GET /health` → `{ status, ws_state, last_message_ms, last_seq, reconnect_attempts, ... }`

## What this PoC answers

| Question | How this PoC answers it |
|---|---|
| Does outbound WebSocket from a DO (`fetch(url, { headers: { Upgrade: "websocket" } })`) work? | `ensureWs()` constructs exactly that request. If it doesn't, the DO logs `WS upgrade failed` with the status code. |
| Does the DO actually stay alive with an outbound WS and no inbound HTTP requests? | Observation: after 10 min of no HTTP calls, does `/book/BTC-USD` still return fresh data? |
| How much GB-s does a 24/7 outbound-WS DO accumulate? | Check Cloudflare dashboard → Workers & Pages → Durable Objects → billing for this script. |
| Does `state.storage.setAlarm()` reliably fire every 30s? | `/health` shows `last_alarm_ms`. |
| What's end-to-end latency Exchange → DO → `GET /book`? | `age_ms` in `/book` response. |

## How to deploy

Requires: Workers Paid plan, Wrangler 4+.

```bash
cd deploy/cf-containers/proof-of-concept
npm install
npx wrangler deploy
```

**No secrets required.** The Extended order book stream is a public endpoint.

The Worker will be deployed to `<project>.<account>.workers.dev`. Visit `/health` and
`/book/BTC-USD` to inspect state. Use `npx wrangler tail` for live logs.

## What this PoC does NOT do

- Reconnection is a 30s alarm retry; no exponential backoff.
- Only one market (BTC-USD). Real OMS-v2 uses the market-less shared stream
  that pushes all markets at once — or one stream per market with parallel DOs.
- No bot-client `/ws` subscriber API yet; that lives in `AggregatorDO` (Phase 3).
- No persistence — all state is in-memory and lost on DO eviction.

## Expected next steps after PoC verification

1. If WS with custom headers works → proceed to full `ExtendedOms` DO (multi-symbol, delta-seq tracking, proper reconnect backoff).
2. If DO GB-s cost is acceptable → commit to Pure-DO architecture for all four exchange DOs.
3. Scaffold `NadoOms`, `GrvtOms`, `VariationalOms` on the same pattern (Nado with EIP-712 via `ethers.js`).
4. Build `AggregatorDO` with hibernation WebSocket for bot subscribers.
5. Port bot-client protocol (`/book`, `/status`, `/tracked`, `/ws`) 1:1 from `deploy/monitor/monitor_service.py`.

## File layout

```
proof-of-concept/
├── README.md                              # this file
├── package.json
├── tsconfig.json
├── wrangler.jsonc
└── src/
    ├── index.ts                           # Worker entrypoint
    ├── exchanges/
    │   └── extended.ts                    # ExtendedOms DO
    └── lib/
        └── orderbook.ts                   # delta application helper
```
