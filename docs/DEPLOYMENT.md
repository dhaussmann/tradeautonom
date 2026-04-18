# TradeAutonom Deployment Guide

All containers run on a **Synology NAS** (`192.168.133.253`). SSH access via `~/.ssh/id_ed25519`.
NAS connection settings are in `.env` (`NAS_HOST`, `NAS_USER`, `NAS_DEPLOY_PATH`).

## Architecture Overview

| Component | Port / URL | Image Tag | Deploy Path | Script |
|-----------|-----------|-----------|-------------|--------|
| **Frontend** (Cloudflare Worker) | `bot.defitool.de` | — | Workers KV | `deploy/cloudflare/deploy.sh` |
| `tradeautonom` (prod) | 8002 | `latest` | `/volume1/docker/tradeautonom` | `deploy/prod/deploy.sh` |
| `tradeautonom-v2` | 8004 | `v2` | `/volume1/docker/tradeautonom-v2` | `deploy/v2/deploy.sh` |
| `tradeautonom-v3` (test) | 8005 | `v3` | `/volume1/docker/tradeautonom-v3` | `deploy/v3/deploy.sh` |
| `ta-user-*` (user containers) | 9001+ | `v3` | shared volume mount | `./deploy.sh` + restart |
| `tradeautonom-dashboard` | 8003 | `latest` | `/volume1/docker/tradeautonom` | `deploy/dashboard/deploy.sh` |
| `ta-orchestrator` | 8090 | `latest` | `/volume1/docker/tradeautonom-orchestrator` | `deploy/orchestrator/deploy.sh` |

**Important:** The frontend runs as a **Cloudflare Worker** on `bot.defitool.de`, NOT inside Docker.
Backend containers share `deploy/prod/Dockerfile` (except dashboard and orchestrator).

## Three Deployment Methods

### 1. Hot-Deploy (Python only, zero-downtime)

```bash
# From project root
./deploy.sh                          # Deploy ALL app/*.py files
./deploy.sh config.py engine.py      # Deploy specific files only
```

**How it works:**
- Copies `app/*.py` files via SSH to `/volume1/docker/tradeautonom/app/` on the NAS
- This directory is bind-mounted read-only into all `ta-user-*` containers at `/app/app`
- Uvicorn runs with `reload=True, reload_dirs=["/app/app"]` — **but** inotify does not reliably trigger on Synology bind-mounts, so a **manual container restart is required** after deploy
- Vault auto-inject from D1 happens on next frontend access after restart

**Safety:**
- Checks all containers (ports 8005, 9001–9006) for active bots before deploying
- Aborts if any bot is in ENTERING or EXITING state
- IDLE and HOLDING bots are considered safe (no active TWAP execution)

**Limitations:**
- Only deploys Python backend files (`app/*.py`)
- Does NOT deploy frontend changes, Dockerfile changes, or dependency changes
- Only affects containers with the `/volume1/docker/tradeautonom/app/` bind-mount (= `ta-user-*` containers)
- Does **NOT** affect `tradeautonom-v3` (port 8005) — that container bakes code into the Docker image at build time; use `deploy/v3/deploy.sh` for V3

> **Important:** After hot-deploy, restart all user containers:
> ```bash
> ssh dhaussmann@192.168.133.253 'for c in $(/usr/local/bin/docker ps --format "{{.Names}}" | grep "^ta-user-"); do /usr/local/bin/docker restart $c; done'
> ```

### 2. Full Deploy (Docker rebuild, brief downtime)

```bash
# V3 (primary multi-user instance)
./deploy/v3/deploy.sh                # Full: sync + build + restart
./deploy/v3/deploy.sh --restart      # Restart only (no rebuild)
./deploy/v3/deploy.sh --logs         # Tail logs
./deploy/v3/deploy.sh --status       # Check health

# Production (legacy)
./deploy/prod/deploy.sh

# V2 (legacy)
./deploy/v2/deploy.sh

# Dashboard
./deploy/dashboard/deploy.sh

# Orchestrator (requires ORCH_TOKEN env var)
ORCH_TOKEN=xxx ./deploy/orchestrator/deploy.sh
```

**How it works:**
1. `cmd_sync` — tars the project (excluding `.venv`, `.git`, `data`, `.env`, etc.) and extracts on NAS
2. `cmd_build` — runs `docker build` on the NAS using `deploy/prod/Dockerfile`
3. `cmd_up` — stops old container, starts new one with volume mounts and env

**All deploy scripts support these flags:**

| Flag | Short | Action |
|------|-------|--------|
| *(none)* | | Full deploy: sync + build + start |
| `--restart` | `-r` | Restart container only |
| `--logs` | `-l` | Tail container logs |
| `--stop` | `-s` | Stop + remove container |
| `--status` | `-t` | Show container status + health check |
| `--sync` | | Sync code only (no build/restart) |
| `--build` | | Sync + build (no restart) |

### 3. Frontend Deploy (Cloudflare Worker)

```bash
./deploy/cloudflare/deploy.sh              # Full: build Vue app + deploy Worker
./deploy/cloudflare/deploy.sh --worker     # Deploy Worker only (skip build)
./deploy/cloudflare/deploy.sh --build      # Build frontend only (no deploy)
```

**How it works:**
1. Builds the Vue app from `frontend/` → `frontend/dist/`
2. Uploads static assets to Workers KV (site bucket configured in `wrangler.jsonc`)
3. Deploys the Worker to Cloudflare route `bot.defitool.de/*`

**Config:** `deploy/cloudflare/wrangler.jsonc`
- VPC binding to NAS backend (`192.168.133.253:8090` via orchestrator)
- D1 database `tradeautonom-history` for trade history
- Secrets: `INGEST_TOKEN`, `BETTER_AUTH_SECRET`, `ORCH_TOKEN` (set via `wrangler secret put`)

## Typical Deployment Scenarios

### Backend-only change (Python files) → User containers
```bash
./deploy.sh server.py dna_bot.py nado_client.py     # 1. Copy files to NAS shared volume
ssh dhaussmann@192.168.133.253 \                     # 2. Restart all user containers
  'for c in $(/usr/local/bin/docker ps --format "{{.Names}}" | grep "^ta-user-"); do /usr/local/bin/docker restart $c; done'
```

### Backend-only change → V3 test container (port 8005)
```bash
./deploy/v3/deploy.sh                               # Full rebuild (code baked into image)
```

### Backend change → ALL containers (V3 + user containers)
```bash
./deploy/v3/deploy.sh                               # 1. Rebuild V3 image + restart
./deploy.sh server.py dna_bot.py nado_client.py     # 2. Copy to shared volume
ssh dhaussmann@192.168.133.253 \                     # 3. Restart user containers
  'for c in $(/usr/local/bin/docker ps --format "{{.Names}}" | grep "^ta-user-"); do /usr/local/bin/docker restart $c; done'
```

### Frontend + Backend change
```bash
./deploy.sh config.py engine.py state_machine.py   # 1. Hot-deploy Python
ssh dhaussmann@192.168.133.253 \                     # 2. Restart user containers
  'for c in $(/usr/local/bin/docker ps --format "{{.Names}}" | grep "^ta-user-"); do /usr/local/bin/docker restart $c; done'
./deploy/cloudflare/deploy.sh                       # 3. Build + deploy frontend to CF Worker
```

### Frontend-only change
```bash
./deploy/cloudflare/deploy.sh
```

### Dependency change (requirements.txt)
```bash
./deploy/v3/deploy.sh    # Full Docker rebuild required (V3 + user containers share same image)
```

### Quick restart (no code change)
```bash
./deploy/v3/deploy.sh --restart
```

## Monitoring

```bash
# Health check
curl http://192.168.133.253:8005/health

# Bot status
curl http://192.168.133.253:8005/fn/bots

# Container logs
./deploy/v3/deploy.sh --logs
```
