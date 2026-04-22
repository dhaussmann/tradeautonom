:# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

**TradeAutonom** is a multi-exchange arbitrage and delta-neutral trading bot. It runs a FastAPI server (Python 3.11) with a Vue.js WebUI, deployed as Docker containers on a Synology NAS at `192.168.133.100`. All instances share the same `app/` codebase; differences between instances are purely infrastructure (port, container name, data volume).

## Commands

```bash
# Local dev server (hot-reload via uvicorn)
python main.py  # → http://localhost:8000/ui

# Frontend (builds into static/ which the server serves)
cd frontend && npm install && npm run build
npm run dev  # Vite dev server only

# Local Docker dev (bind-mounted code, no rebuild needed for app changes)
cd deploy/local && docker-compose up -d

# Deploy to NAS instances
./deploy/prod/deploy.sh        # Production (port 8002)
./deploy/v2/deploy.sh          # Staging (port 8004)
./deploy/v3/deploy.sh          # Multi-user (port 8005)

# Health / manual verification (no formal test suite)
curl http://localhost:8000/health
curl http://localhost:8000/jobs
curl -X POST http://localhost:8000/arb/check -H "Content-Type: application/json" \
  -d '{"instrument_a":"SOL-USD","instrument_b":"SOL_USDT_Perp"}'
```
## Username
use root to connect to the NAS
## Architecture

### Request path
Browser → FastAPI (`server.py`) → `JobManager` → `FundingArbEngine` (`engine.py`) → `StateMachine` (`state_machine.py`) → exchange clients

### Core modules

| Module | Role |
|--------|------|
| `server.py` | FastAPI, SSE streams, Vault auth, REST endpoints for jobs/arb/funding |
| `engine.py` | `FundingArbEngine` — creates and drives `StateMachine` instances per job |
| `state_machine.py` | TWAP execution: Maker→Taker chunks, repricing loop, position repair |
| `data_layer.py` | Async WebSocket feeds (orderbook, positions, fills) per exchange+symbol; REST fallback on stale |
| `arbitrage.py` | Spread signals, entry/exit thresholds, mean-reversion logic |
| `safety.py` | Pre-trade checks: `walk_book()` estimates fill price, validates depth/slippage |
| `risk_manager.py` | Delta limits, circuit-breaker (cumulative loss), spread guards |
| `job_manager.py` | Multi-job lifecycle, persists to `data/jobs.json` |
| `config.py` | Pydantic Settings loaded from `.env`; all trading params have defaults here |
| `crypto.py` | AES-256-GCM vault for API keys; unlocked once per session via password |
| `dna_bot.py` | Delta-Neutral Arb bot (optional; import gracefully degrades if deps missing) |

### Exchange clients

All implement the `ExchangeClient` protocol (`exchange.py`):

| Exchange | File | Notes |
|----------|------|-------|
| Extended | `extended_client.py` | StarkNet, x10 SDK, Post-Only + IOC |
| GRVT | `grvt_client.py` | REST + WS, Cookie-Auth |
| Variational | `variational_client.py` | RFQ-DEX, OLP Maker, `curl_cffi` for Cloudflare bypass |
| Nado | `nado_client.py` | DEX, WS position + fill streams |

### TWAP execution flow (per chunk)
1. Maker phase: Post-Only order at best bid/ask ± offset
2. Chase loop: on timeout → cancel → reprice ± N ticks → retry (max N rounds)
3. Taker phase: IOC hedge immediately after Maker fill (uses `walk_book` for slippage estimate)
4. Position verify: REST queries both exchanges, validates delta against baseline
5. Repair: if delta > `min_repair_qty`, places IOC correction

### Strategy modes
- `arbitrage` — enter at large spread, exit at small spread (mean-reversion)
- `delta_neutral` — enter AND exit at small spread (funding-rate harvesting)

## Key conventions

- **Decimals**: Use `Decimal` for all price/qty; convert to `float` only at API call boundaries
- **Async**: All exchange calls are async; always `await` them — never call synchronously
- **Frontend**: Vue 3 + Composition API, Pinia for state, TypeScript strict mode
- **Simulation mode**: Set `ARB_SIMULATION_MODE=true` or `FN_SIMULATION_MODE=true` in `.env` for paper trading

## Deployment model

- The NAS host is `192.168.133.100`; SSH key at `~/.ssh/id_ed25519` with NAS access required
- App code is **not baked into Docker images** — it is bind-mounted at runtime; `deploy.sh` hot-copies `app/*.py` over SSH and uvicorn auto-reloads within ~3 seconds
- Each instance has its own `data/` volume: `data/jobs.json`, `data/vault.enc`, `data/trade_log.jsonl`, `data/execution_log.jsonl`
- `deploy.sh` checks `/fn/bots` for active bots and **aborts if any are running** — do not deploy during live trades

## Pitfalls

- **Port conflicts**: Each instance requires a unique `APP_PORT` in its `.env`
- **Stale WS data**: `arb_ws_stale_ms` controls when DataLayer falls back to REST; tune per exchange
- **DNA Bot import**: Server wraps the import in try/except — missing deps log a warning but don't crash startup
- **Vault sessions**: After a hot-deploy the vault stays unlocked (session lives in-memory in the running process); a container restart requires password re-entry via `/auth/unlock`
