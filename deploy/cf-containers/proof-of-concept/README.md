# OMS-v2 Proof of Concept

Minimal TypeScript implementation of a Cloudflare-native `ExtendedOms` Durable Object.
Goal: validate the **open technical questions** from `docs/v2-oms-cloudflare-native.md`
before committing to the full OMS-v2 rewrite.

**This is a research artifact, not a production path.** Do not point real bot clients
at this endpoint yet.

## Current live deployment

`https://oms-v2-poc.defitool.de`
(fallback: `https://oms-v2-poc.cloudflareone-demo-account.workers.dev`)

Worker + DO are already live. Redeploy only needed after code changes.

## What this PoC does

- Spins up a single `ExtendedOms` Durable Object singleton.
- Opens **one** outbound WebSocket to the **shared all-markets** Extended stream:
  `wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks`
  (no `{market}` segment — mirrors Photon OMS production behavior, gets all active
  markets on one connection).
- No authentication. No X-Api-Key. The order book stream is public.
- Parses `SNAPSHOT` + `DELTA` messages, merges deltas per market.
- Keeps the **top 10 bid + 10 ask levels** per market.
- Handles 100+ markets concurrently.
- Self-heals via Extended's one-per-minute full SNAPSHOT (no manual seq-gap recovery
  needed on the shared stream; per-market seq continuity is impossible because seqs
  are interleaved globally).

## Persistence — the short answer

**No orderbook data is persisted.** Everything lives in-memory in the DO process.
If the DO evicts or is cold-started, the state is lost and repopulated from the
next SNAPSHOT (fast — <1s per market).

The `wrangler.jsonc` declares `new_sqlite_classes: ["ExtendedOms"]`, which enables
SQLite-backed storage for the DO class, but we intentionally don't use
`this.ctx.storage.sql.exec(...)` anywhere. Hot orderbook data would be pure
write-amplification in SQLite. Persistence will come in Phase 3 for:
- Bot subscription state (which bot wants which `(exchange, symbol)`)
- Tracking configuration (which markets to subscribe to)

Not for orderbook rows.

## Endpoints

```
GET /                  hint page
GET /health            DO health + WS state + total stats
GET /markets           all tracked markets with top-of-book + age + updates
GET /book/{market}     full top-10 orderbook for a specific market
```

## Quick test

```bash
curl -s https://oms-v2-poc.defitool.de/health | python3 -m json.tool
curl -s https://oms-v2-poc.defitool.de/markets | python3 -m json.tool
curl -s https://oms-v2-poc.defitool.de/book/BTC-USD | python3 -m json.tool
```

If your local DNS is filtered by a corporate Cloudflare WARP / Zero Trust Gateway,
bypass with explicit IP resolution:

```bash
IP=$(dig +short @1.1.1.1 oms-v2-poc.defitool.de | head -1)
curl -s --resolve oms-v2-poc.defitool.de:443:$IP \
  https://oms-v2-poc.defitool.de/health | python3 -m json.tool
```

## Sample output

`/health` after ~1 minute of uptime:

```json
{
  "status": "ok",
  "ws_state": "connected",
  "reconnect_attempts": 1,
  "last_message_ms": 1776983247471,
  "last_alarm_ms": 1776983232216,
  "markets_tracked": 114,
  "total_updates": 13454,
  "uptime_ms": 51580
}
```

`/markets` (abridged — returns all 114):

```
symbol                            bid            ask  bids  asks    upd    age
--------------------------------------------------------------------------------
1000BONK-USD                   0.0063         0.0063     8     9     60  205ms
1000PEPE-USD                   0.0038         0.0038    10     9    136  205ms
AAVE-USD                      94.3500        94.4000    10    10    654  204ms
ADA-USD                        0.2497         0.2498    10     9    147  205ms
BTC-USD                   78,151.0000    78,152.0000    10    10   1045  211ms
...
```

`/book/BTC-USD`:

```json
{
  "exchange": "extended",
  "symbol": "BTC-USD",
  "bids": [[78151, 25.8974], [78150, 0.5107], ...],
  "asks": [[78152, 0.7195], [78153, 0.3336], ...],
  "timestamp_ms": 1776983...,
  "age_ms": 186,
  "connected": true,
  "updates": 1049,
  "last_seq": 15938
}
```

## Evaluation test plan

Three ready-to-use Python scripts under `test/` automate all of this. See
`test/README.md` for the full usage guide. Short recap:

```bash
cd deploy/cf-containers/proof-of-concept/test

# Long-running stability probe (every 30 s, writes CSV + log, flags anomalies)
python3 watchdog.py --resolve-via 1.1.1.1

# Side-by-side V1 Photon vs V2 PoC for a set of markets
python3 compare_v1_v2.py --resolve-via 1.1.1.1

# Latency histogram (min/median/p90/p99/ASCII bars)
python3 latency_histogram.py --resolve-via 1.1.1.1 --samples 200 --interval 0.5
```

The five questions below are what the scripts above answer.

### 1. Does outbound WebSocket from a DO deliver messages?

Poll `/health` shortly after a cold start. Within ~5 seconds:
- `ws_state` = `"connected"`
- `total_updates` > 0
- `markets_tracked` > 0

```bash
curl -s https://oms-v2-poc.defitool.de/health | python3 -m json.tool
```

### 2. Multi-market capacity on one shared stream

Extended's shared stream pushes ~114 active markets; the DO should track all of them
on a single WebSocket. Check:

```bash
curl -s https://oms-v2-poc.defitool.de/markets | python3 -c '
import sys, json
d = json.load(sys.stdin)
print(f"total markets: {d[\"total\"]}")
print(f"with data    : {sum(1 for m in d[\"markets\"] if m[\"updates\"] > 0)}")
print(f"with 10+10 levels: {sum(1 for m in d[\"markets\"] if m[\"bid_levels\"] >= 10 and m[\"ask_levels\"] >= 10)}")
'
```

### 3. Freshness (Exchange → DO → caller latency)

`age_ms` in any `/book/*` response is `now - Extended's server-provided timestamp`.
Active markets should be well under 500 ms.

```bash
for sym in BTC-USD ETH-USD SOL-USD BNB-USD; do
  echo -n "$sym: "
  curl -s https://oms-v2-poc.defitool.de/book/$sym | \
    python3 -c 'import sys,json; d=json.load(sys.stdin); print(f"age_ms={d[\"age_ms\"]}  updates={d[\"updates\"]}")'
done
```

Compare against the Photon OMS (V1) for the same symbol:
```bash
curl -s http://192.168.133.100:8099/book/extended/BTC-USD | python3 -m json.tool
```

### 4. Does the DO stay alive with no inbound HTTP traffic?

Let the DO sit untouched for 20+ minutes. Outbound WS should keep it warm.

```bash
curl -s https://oms-v2-poc.defitool.de/health    # note uptime_ms
sleep 1200                                        # 20 minutes
curl -s https://oms-v2-poc.defitool.de/health    # uptime_ms should be +1200000 with no reset
```

If `uptime_ms` resets (goes back to a small value) or `reconnect_attempts` grows,
the DO was evicted despite the open outbound WS.

### 5. Cost (GB-seconds per day)

Cloudflare dashboard → Workers & Pages → `oms-v2-poc` → Durable Objects Metrics.

A single DO with an always-open outbound WS doesn't hibernate, so it bills as
continuously active. Rough estimate: ~20–40 MB in-memory × 86,400 s ≈ 1,700–3,500
GB-s/day. Confirm against real metrics.

## Live logs

```bash
cd deploy/cf-containers/proof-of-concept
npx wrangler tail
```

Interesting log lines:
- `alarm fired` — every ~30 s (the heartbeat)
- `WS connected (all-markets shared stream)` — once per reconnect
- `WS closed` / `WS stale, closing` — rare; investigate if frequent
- `alarm fired ... age_ms=<high>` — data pipeline broken somewhere

## How to redeploy after code changes

```bash
cd deploy/cf-containers/proof-of-concept
npm install          # only first time
npx wrangler deploy
```

No secrets required. The deployment keeps the DO class migration (`v1`) stable,
so in-memory state is reset but the SQLite table structure (unused but present)
is preserved.

## What this PoC does NOT do

- No persistence of orderbook data (by design — it's hot state).
- No bot-client `/ws` subscriber API (that's Phase 3: `AggregatorDO`).
- Reconnects are 30 s alarm-driven, no exponential backoff.
- Only Extended. Nado / GRVT / Variational come later.
- No tick-size/qty-step validation — we trust Extended's own formatting.

## Expected next steps once PoC is validated

1. Add `NadoOms` on the same pattern (but EIP-712 signed subscribe frame via `ethers`).
2. Add `GrvtOms` + `VariationalOms` (latter polls `proxy.defitool.de`).
3. Build `AggregatorDO` — hibernation-enabled WebSocket server for bot subscribers.
4. Port the V1 OMS wire protocol (`/book`, `/status`, `/tracked`, `/ws`) 1:1 so the
   existing `app/data_layer.py::_run_oms_ws` client works unchanged against V2.
5. Canary: route one V2 user's `FN_OPT_SHARED_MONITOR_URL` to the V2 OMS and
   compare `/book/*` outputs against V1 Photon OMS.

## File layout

```
proof-of-concept/
├── README.md                              # this file
├── package.json                           # wrangler 4 + TypeScript
├── tsconfig.json
├── wrangler.jsonc                         # DO binding + routes + custom_domain
└── src/
    ├── index.ts                           # Worker entrypoint
    ├── exchanges/
    │   └── extended.ts                    # ExtendedOms DO (all-markets shared stream)
    └── lib/
        └── orderbook.ts                   # SNAPSHOT/DELTA merging, top-N cap
```
