# TradeAutonom — Agent Guide

Multi-Exchange Arbitrage & Delta-Neutral Trading Bot mit WebUI.

## Quick Start (Local Dev)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edit API keys, ports
python main.py        # → http://localhost:8000/ui
```

## Project Structure

| Path | Purpose |
|------|---------|
| `main.py` | Entry point (uvicorn) |
| `app/server.py` | FastAPI + Auth + SSE endpoints |
| `app/engine.py` | FundingArbEngine, StateMachine orchestration |
| `app/arbitrage.py` | Spread monitoring, entry/exit signals |
| `app/state_machine.py` | TWAP: Maker→Taker, Repricing, Repair |
| `app/data_layer.py` | WebSocket feeds (orderbook, positions, fills) |
| `app/job_manager.py` | Multi-job management + persistency |
| `app/*_client.py` | Exchange clients: Extended, GRVT, Variational, Nado |
| `frontend/` | Vue.js + Vite WebUI (builds to `static/`) |
| `deploy/{prod,v2,v3,dashboard,local}/` | Per-instance deployment configs |

## Key Commands

```bash
# Dev server
python main.py

# Build frontend
cd frontend && npm install && npm run build

# Deploy to NAS (prod)
./deploy/prod/deploy.sh

# Deploy to NAS (v2 staging)
./deploy/v2/deploy.sh

# Local Docker (bind-mounted code)
cd deploy/local && docker-compose up -d

# Check status/logs remotely
./deploy/prod/deploy.sh --status
./deploy/prod/deploy.sh --logs
```

## Multi-Instance Deployments

All instances share the same `app/` code. Differences are purely infrastructure:

| Instance | Port | Container | Purpose |
|----------|------|-----------|---------|
| `prod` | 8002 | `tradeautonom` | Production trading |
| `v2` | 8004 | `tradeautonom-v2` | Test/Staging |
| `v3` | 8005 | `tradeautonom-v3` | Multi-user + Vault encryption |
| `dashboard` | 8003 | `tradeautonom-dashboard` | Read-only account view |
| `local` | 8000 | `tradeautonom` | Dev (bind-mounted) |

Each instance has its own `data/` directory for trade logs and encrypted vault.

## Critical File Locations

- **Config**: `app/config.py` (Pydantic Settings, loads from `.env`)
- **Secrets**: `.env` (never committed; stored separately on NAS per instance)
- **Trade logs**: `data/trade_log.jsonl` (instance-specific, survives rebuilds)
- **Vault (v3)**: `data/vault.enc` (AES-256-GCM encrypted API keys)

## Exchange Client Mapping

| Exchange | Client File | Notes |
|----------|-------------|-------|
| Extended | `app/extended_client.py` | StarkNet, x10 SDK, Post-Only + IOC |
| GRVT | `app/grvt_client.py` | REST + WS, Cookie-Auth |
| Variational | `app/variational_client.py` | RFQ-DEX, OLP Maker, curl_cffi for CF |
| Nado | `app/nado_client.py` | DEX, WS position + fill streams |

## Strategy Modes

- `arbitrage`: Entry at large spread, exit at small spread (mean-reversion)
- `delta_neutral`: Entry AND exit at small spread (funding rate harvesting)

## Environment Variables (Key)

See `.env.example` for full list. Critical ones:

```bash
# Instance
APP_PORT=8000                    # Must match deploy target

# Exchange credentials
GRVT_API_KEY=...
GRVT_PRIVATE_KEY=...
EXTENDED_API_KEY=...
NADO_PRIVATE_KEY=...
VARIATIONAL_JWT_TOKEN=...

# Trading safety (all have sensible defaults in config.py)
ARB_SPREAD_ENTRY_LOW=0.08
ARB_SPREAD_EXIT_HIGH=0.02
ARB_SIMULATION_MODE=false        # Paper trading when true
```

## Testing

No formal test suite. Manual verification:

```bash
# Health check
curl http://localhost:8000/health

# Job status
curl http://localhost:8000/jobs

# Spread check (manual)
curl -X POST http://localhost:8000/arb/check -H "Content-Type: application/json" \
  -d '{"instrument_a":"SOL-USD","instrument_b":"SOL_USDT_Perp"}'
```

## Code Style Conventions

- **Python**: No formal linter config; follow existing patterns in `app/`
- **Frontend**: Vue 3 + Composition API, Pinia for state, TypeScript strict
- **Async**: Heavy use of `asyncio` — always await exchange calls
- **Decimals**: Use `Decimal` for price/qty, convert to `float` only at API boundaries

## Common Pitfalls

1. **Port conflicts**: Each instance needs unique `APP_PORT` in `.env`
2. **Volume mounts**: Local dev mounts `app/` as read-only; changes need container restart
3. **WebSocket stale data**: `arb_ws_stale_ms` controls fallback to REST
4. **DNA Bot import**: Gracefully degrades if dependencies missing (see `server.py` lines 31-41)
5. **Deploy SSH key**: Requires `~/.ssh/id_ed25519` with NAS access; check `.env` for `NAS_HOST`, `NAS_USER`

## Documentation

- `docs/architecture.md` — Detailed module interactions
- `docs/safety-balance-verification.md` — Position verification logic
- `docs/taker-execution.md` — Taker-side execution details
- `deploy/*/README.md` — Per-instance deploy notes
