# OMS-v2 Proof of Concept

Minimal TypeScript implementation of a Cloudflare-native `ExtendedOms` Durable Object.
Goal: validate the **open technical questions** from `docs/v2-oms-cloudflare-native.md`
before committing to the full OMS-v2 rewrite.

**This is a research artifact, not a production path.** Do not deploy to a route that real
bot clients point at.

## Current live deployment

Deployed and running on:
`https://oms-v2-poc.defitool.de`
(also available on `https://oms-v2-poc.cloudflareone-demo-account.workers.dev` if you have
access to that hostname)

Worker + DO are already live. No further deploy step needed unless you change code.

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

## Quick test (copy/paste)

```bash
# Health snapshot
curl -s https://oms-v2-poc.defitool.de/health | python3 -m json.tool

# Top-20 orderbook for BTC-USD
curl -s https://oms-v2-poc.defitool.de/book/BTC-USD | python3 -m json.tool
```

If your local DNS is blocked (e.g. by a corporate Cloudflare WARP / Zero Trust Gateway
that filters `*.defitool.de`), bypass with explicit IP resolution:

```bash
IP=$(dig +short @1.1.1.1 oms-v2-poc.defitool.de | head -1)
curl -s --resolve oms-v2-poc.defitool.de:443:$IP \
  https://oms-v2-poc.defitool.de/health | python3 -m json.tool
```

## Test plan — how to evaluate the PoC

This PoC exists to answer specific technical questions. Run the following observations
over the course of a few hours and record results.

### 1. Does outbound WebSocket from a DO deliver messages?

Expected within the first ~5 seconds after a cold start: `ws_state: "connected"`,
`updates > 0`, `last_seq > 0`.

```bash
curl -s https://oms-v2-poc.defitool.de/health
```

Pass = `updates` grows over successive calls.

### 2. Does the DO stay alive with an outbound WS and no inbound HTTP?

Let the DO sit idle for ~20 minutes without hitting any endpoint, then query `/book/BTC-USD`:

- If `age_ms` is small (<5s) → DO stayed hot the whole time. Pass.
- If `age_ms` is huge or `updates` reset → DO evicted; the outbound WS would not have
  kept it alive (contradicts the docs).

```bash
# T = 0
curl -s https://oms-v2-poc.defitool.de/health
# wait 20 minutes — do not hit the endpoint
# T = 20min
curl -s https://oms-v2-poc.defitool.de/book/BTC-USD | python3 -c 'import sys,json; d=json.load(sys.stdin); print("age_ms=", d["age_ms"], "updates=", d["updates"])'
```

### 3. How fresh is the orderbook?

Latency from the exchange to the caller is directly readable. `age_ms` in `/book/BTC-USD`
is `now - server-provided timestamp`. Includes one network hop from Extended to Cloudflare
and one from Cloudflare to your machine.

```bash
for i in 1 2 3 4 5; do
  curl -s https://oms-v2-poc.defitool.de/book/BTC-USD | \
    python3 -c 'import sys,json,time; d=json.load(sys.stdin); print(f"age_ms={d[\"age_ms\"]:>4}  updates={d[\"updates\"]}  seq={d[\"last_seq\"]}")'
  sleep 2
done
```

Typical V1 Python OMS (Photon VM) sees `age_ms` under ~200 ms for Extended. V2 Pure DO
should be in a similar range if Cloudflare places the DO near the AWS Tokyo endpoint.
Compare side-by-side with `/book/extended/BTC-USD` on the Photon OMS if you want a
direct benchmark (`curl http://192.168.133.100:8099/book/extended/BTC-USD`).

### 4. Do sequence numbers stay monotonic?

`/health` reports `last_seq`. It should increase by 1 per DELTA and reset to 1 on every
scheduled SNAPSHOT (every minute per the spec).

```bash
# Sample every 5s and look for gaps
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  curl -s https://oms-v2-poc.defitool.de/health | \
    python3 -c 'import sys,json,time; d=json.load(sys.stdin); print(f"t={time.strftime(\"%H:%M:%S\")}  seq={d[\"last_seq\"]}  updates={d[\"updates\"]}  reconnects={d[\"reconnect_attempts\"]}")'
  sleep 5
done
```

- `updates` should grow by ~50-100 per 5s (~10-20 updates/s).
- `reconnect_attempts` should stay at 1 under normal conditions.
- If `reconnect_attempts` increases without apparent network problems, check the dashboard
  logs for "seq gap detected" — that's the PoC responding to missed deltas.

### 5. Cost / resource observation

Inspect Durable Objects metrics for 24-48 hours:

1. Cloudflare dashboard → Workers & Pages → `oms-v2-poc` → Metrics
2. Note the **"duration" (GB-s)** billed per day.
3. Compare against the Photon VM's per-day OMS runtime cost
   (rough calc: VM cost × fraction of CPU/RAM used by `oms` container / total VM resources).

One DO with an open outbound WS typically bills as continuously active (no hibernation).
A rough back-of-envelope estimate: ~0.02 GB sustained × 86_400 s ≈ 1_700 GB-s/day.

### 6. Live logs (optional, requires dashboard or wrangler CLI)

```bash
cd deploy/cf-containers/proof-of-concept
npx wrangler tail
```

Interesting log patterns to look for:

- `alarm fired` every ~30s (the heartbeat).
- `opening WS` and `WS connected` — should only fire at startup.
- `WS closed` / `seq gap detected` — rare; if they happen frequently, flag it.

## How to redeploy after code changes

```bash
cd deploy/cf-containers/proof-of-concept
npm install          # only needed once
npx wrangler deploy
```

No secrets required — the Extended order book stream is public.

The deployment keeps DO instance IDs stable, so a redeploy resets in-memory state but the
DO class migration stays intact.

## What this PoC does NOT do

- Reconnection is a 30s alarm retry; no exponential backoff.
- Only one market (BTC-USD). Real OMS-v2 could use the market-less shared stream
  that pushes all markets at once — or one stream per market with parallel DOs.
- No bot-client `/ws` subscriber API yet; that lives in `AggregatorDO` (planned Phase 3).
- No persistence — all state is in-memory and lost on DO eviction.

## Expected next steps once PoC is validated

1. If the answers to questions 1-5 are all positive → proceed to full `ExtendedOms` DO
   (multi-symbol, proper backoff, expose AggregatorDO-facing RPC).
2. Scaffold `NadoOms`, `GrvtOms`, `VariationalOms` on the same pattern.
3. Build `AggregatorDO` with Hibernation WebSocket API for bot subscribers.
4. Port the bot-client wire format (`/book`, `/status`, `/tracked`, `/ws`) 1:1 from
   `deploy/monitor/monitor_service.py` so existing `app/data_layer.py::_run_oms_ws`
   clients can point at V2 via `FN_OPT_SHARED_MONITOR_URL`.
5. Canary: one V2 user on the new OMS subdomain; compare `/book/*` output against
   the V1 Photon OMS.

## File layout

```
proof-of-concept/
├── README.md                              # this file
├── package.json                           # wrangler 4 + TypeScript
├── tsconfig.json
├── wrangler.jsonc                         # DO binding + routes
└── src/
    ├── index.ts                           # Worker entrypoint
    ├── exchanges/
    │   └── extended.ts                    # ExtendedOms DO
    └── lib/
        └── orderbook.ts                   # SNAPSHOT/DELTA application
```
