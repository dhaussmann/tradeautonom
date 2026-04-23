# OMS-v2 PoC stability tests

Three standalone Python scripts (stdlib only, no dependencies) for verifying
that the CF PoC stays healthy over time and producing comparable numbers
against V1 (Photon OMS).

All scripts work with and without Cloudflare WARP / Zero Trust Gateway. If your
machine's DNS resolver blocks `*.defitool.de`, pass `--resolve-via 1.1.1.1`.

## 1. Watchdog — long-running stability probe

Polls `/health` and `/markets` on a regular cadence, writes a CSV row per
poll, and raises anomalies when reconnects happen, uptime resets (DO evicted),
messages stop flowing, or hot markets go stale.

```bash
cd deploy/cf-containers/proof-of-concept/test

# Quick smoke test — 2 minutes, 10 s interval
python3 watchdog.py --resolve-via 1.1.1.1 --interval 10 --duration 120

# Run overnight — every 30 s, no end time (Ctrl-C to stop)
python3 watchdog.py --resolve-via 1.1.1.1 --interval 30
```

Outputs (in CWD):

- `watchdog.csv` — one row per poll; columns include `ws_state`, `reconnect_attempts`,
  `markets_tracked`, `total_updates`, `uptime_ms`, `age_<market>`, `anomalies`.
- `watchdog.log` — human-readable per-poll line; anomalies prefixed with `!!`.

### What a healthy run looks like

```
2026-04-23T22:38:56.157133+00:00  ws=connected   markets=114 updates= 108569 reconn=1 uptime=740s last_msg=461ms hot=[BTC-USD=249ms ETH-USD=273ms SOL-USD=181ms BNB-USD=286ms]
2026-04-23T22:39:06.844193+00:00  ws=connected   markets=114 updates= 109852 reconn=1 uptime=751s last_msg=408ms hot=[BTC-USD=183ms ETH-USD=180ms SOL-USD=204ms BNB-USD=379ms]
```

Pass criteria over a 24-hour run:
- `reconnect_attempts` increments by ≤ 5 total (a handful of reconnects on CF host
  shuffles is fine; anything more indicates instability).
- `uptime_ms` never resets (no DO evictions despite inbound traffic from the
  watchdog every 30 s).
- `markets_tracked` stays at 110+ (Extended has ~114 active markets; transient
  drops during a disconnect are OK, but shouldn't persist).
- `total_updates` grows monotonically between successive polls.
- No `STREAM_SILENT` anomalies.

### Suggested schedule

- Run for 1 hour after any code change (short sanity).
- Run overnight (8–12 h) before declaring "stable".
- Re-run for 72 h before committing to Phase 3.

## 2. V1 vs V2 side-by-side comparison

Queries the same Extended markets on both V1 (Photon) and V2 (CF PoC) and
prints a row with top bid / top ask / mid-price diff in basis points, plus
both ages in ms.

```bash
cd deploy/cf-containers/proof-of-concept/test

# Single round against 8 default markets
python3 compare_v1_v2.py --resolve-via 1.1.1.1

# Specific market list
python3 compare_v1_v2.py --markets BTC-USD,ETH-USD,SOL-USD,ASTER-USD

# Repeat every 10 s for 5 minutes
python3 compare_v1_v2.py --resolve-via 1.1.1.1 --interval 10 --duration 300
```

Requires that V1 (Photon OMS at `192.168.133.100:8099`) is reachable from
the client. If you're not on the lab network, V1 columns will show
`ERR: v1 timeout`. That's still useful — V2 columns alone tell you if the
PoC is producing valid books.

### What a healthy comparison looks like

- `bid_diff` and `ask_diff` are small (≤ 1 tick, i.e. `minPriceChange` for the
  market). Perfect match is rare because the two streams land at slightly
  different instants.
- `mid_bp` (basis points of mid-price drift between V1 and V2) should be
  within ±2 bp for liquid markets.
- `v1_age` and `v2_age` should both be < 1000 ms for BTC/ETH/SOL-class
  markets. Large mismatches indicate one side is stale.
- Both `levels` columns should show `10/10` (or close) for depth.

## 3. Latency histogram

Samples `/book/{market}` N times and prints min/median/p90/p99 age plus an
ASCII histogram.

```bash
cd deploy/cf-containers/proof-of-concept/test

# 60 samples from V2, 1 second apart
python3 latency_histogram.py --resolve-via 1.1.1.1

# 200 samples, 0.5 s apart — tighter distribution
python3 latency_histogram.py --resolve-via 1.1.1.1 --samples 200 --interval 0.5

# Same against V1 for direct comparison
python3 latency_histogram.py --source v1 --samples 200 --interval 0.5
```

### Sample output

```
source=V2  host=oms-v2-poc.defitool.de  ip=104.18.8.150  market=BTC-USD  samples=30
completed in 25.9s  errors=0

n=30  min=123  max=223  median=190
p90=216  p99=223  mean=182.8

  <=  100ms:    0
  <=  200ms:   20  ########################################
  <=  500ms:   10  ####################
  <= 1000ms:    0
  ...
```

Pass criteria for BTC-USD-class markets:
- V2 p99 under 500 ms.
- V1 p99 under 300 ms (lower is expected — Photon is in the same AZ as
  Extended Tokyo).
- errors == 0.

Note: the measured `age_ms` is `now_client - ts_server` so it includes both
legs (server→DO + DO→client). V2 will always have higher age than V1 because
V1 runs in the same AWS Tokyo AZ as Extended; V2 runs on Cloudflare's global
network. The question is whether V2's absolute freshness is acceptable for
our execution loop (<500 ms is almost always fine).

## Running the tests in the background

```bash
cd deploy/cf-containers/proof-of-concept/test

# Kick off a 24-hour watchdog
nohup python3 watchdog.py --resolve-via 1.1.1.1 --interval 30 \
    --csv /tmp/wd-$(date +%Y%m%d).csv \
    --log /tmp/wd-$(date +%Y%m%d).log \
    > /tmp/wd-$(date +%Y%m%d).out 2>&1 &
echo "watchdog PID=$!"
```

Kill it later:

```bash
pgrep -f "watchdog.py" | xargs kill
```

## Interpreting CSV output

Load into Python / pandas for deeper analysis:

```python
import pandas as pd
df = pd.read_csv("watchdog.csv", parse_dates=["ts"])
# Reconnects over time
df[df["anomalies"].notna() & df["anomalies"].str.contains("RECONNECT", na=False)]
# Average total_updates per minute
df["updates_per_poll"] = df["total_updates"].diff()
print(df["updates_per_poll"].describe())
# Hot-market freshness distribution
df["age_BTC-USD"].describe()
```

## Troubleshooting

| Symptom | Likely cause | Remedy |
|---|---|---|
| `ConnectionError` or timeouts on V2 | Cloudflare WARP / Gateway blocks the zone | Use `--resolve-via 1.1.1.1`, or pause WARP |
| `ERR: v1 timeout` | Not on the lab network (`192.168.133.100`) | OK, ignore V1 columns, or run from a container on the VM |
| `STREAM_SILENT` anomalies after hours | DO went cold but outbound WS still reports "connected" | Investigate in `wrangler tail`; may indicate a CF-side socket leak |
| `UPTIME_RESET` | DO was evicted despite open WS — contradicts docs | Bug report to Cloudflare (rare, but worth documenting) |
| `MARKETS_DROPPED` | Extended removed markets (e.g. maintenance) OR WS dropped | Check `reconnect_attempts` and Extended status page |
