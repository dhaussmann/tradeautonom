# OMS-v2 Proof of Concept

Minimal TypeScript implementation of a Cloudflare-native `ExtendedOms` Durable Object.
Goal: validate the **five open technical questions** from `docs/v2-oms-cloudflare-native.md`
before committing to the full OMS-v2 rewrite.

**This is a research artifact, not a production path.** Do not deploy to a route that real
bot clients point at.

## What this PoC does

- Spins up a single `ExtendedOms` DO singleton.
- Opens an outbound WebSocket to `wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks`
  with an `X-Api-Key` header.
- Parses incoming SNAPSHOT/DELTA messages for a single market (`BTC-USD`).
- Stores the top-20 bid/ask levels in memory.
- Exposes one HTTP endpoint:
  - `GET /book/BTC-USD` → JSON with current bids/asks + lag metrics
  - `GET /health` → `{ status, reconnect_attempts, last_message_ms, ws_state }`

## What this PoC answers

| Question | How this PoC answers it |
|---|---|
| Does `fetch(url, { headers: { Upgrade: "websocket", "X-Api-Key": ... }})` work? | `ensureWs()` constructs exactly that request. If it doesn't, the DO logs `upgrade_failed` with the status code. |
| Does the DO actually stay alive with an outbound WS and no inbound requests? | Observation: after 10 min of no HTTP calls, does `/book/BTC-USD` still return fresh data? |
| How much GB-s does a 24/7 outbound-WS DO accumulate? | Check Cloudflare dashboard → Workers & Pages → Durable Objects → billing for this script. |
| Does `state.storage.setAlarm()` reliably fire every 30s? | `/health` shows `last_alarm_ms`. |
| What's end-to-end latency Exchange → DO → `GET /book`? | `age_ms` in `/book` response. |

## How to deploy

Requires: Workers Paid plan, Wrangler 4+, an Extended API key.

```bash
cd deploy/cf-containers/proof-of-concept
npm install
npx wrangler secret put EXTENDED_API_KEY
# paste your Extended API key
npx wrangler deploy
```

The Worker will be deployed to `<project>.<account>.workers.dev`. Visit `/health` and
`/book/BTC-USD` to inspect state. Use `npx wrangler tail` for live logs.

## What this PoC does NOT do

- Does not handle reconnection beyond a 30s alarm retry.
- Does not parse delta sequence numbers or emit gap warnings.
- Does not implement the bot-client `/ws` subscriber API.
- Does not persist anything — all state is in-memory and lost on DO eviction.
- Does not support more than one market.

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
