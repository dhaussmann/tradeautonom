# TradeAutonom — Developer Guide

Comprehensive guide for developers working on the TradeAutonom codebase: a multi-exchange arbitrage and delta-neutral trading system with a Vue.js frontend, Python/FastAPI backend, and Docker-based deployment to a Synology NAS.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Tech Stack](#tech-stack)
3. [Project Structure](#project-structure)
4. [Local Development Setup](#local-development-setup)
5. [Backend (`app/`)](#backend-app)
6. [Frontend (`frontend/`)](#frontend-frontend)
7. [Exchange Clients](#exchange-clients)
8. [Trading Engine & State Machine](#trading-engine--state-machine)
9. [Vault & Secret Management](#vault--secret-management)
10. [Deployment](#deployment)
11. [Infrastructure](#infrastructure)
12. [API Reference (Key Endpoints)](#api-reference-key-endpoints)
13. [Known Issues & Gotchas](#known-issues--gotchas)
14. [Testing](#testing)
15. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

```
                        ┌──────────────────────────┐
                        │   Cloudflare Worker       │
                        │   (bot.defitool.de)       │
                        │   Vue SPA + API Router    │
                        └────────┬─────────────────┘
                                 │ VPC binding
                        ┌────────▼─────────────────┐
                        │   Orchestrator (port 8090)│
                        │   Routes to user containers│
                        └──┬──────┬──────┬─────────┘
                           │      │      │
                   ┌───────▼┐ ┌───▼────┐ ┌▼────────┐
                   │ ta-9001│ │ ta-9002│ │ ta-9003 │  ← Per-user containers
                   └───┬────┘ └───┬────┘ └───┬─────┘
                       │          │          │
                ┌──────▼──────────▼──────────▼─────────────┐
                │            FastAPI Server (server.py)      │
                │  ┌──────────┐ ┌──────────┐ ┌───────────┐ │
                │  │ Vault    │ │ Bot/Job  │ │ Engine(s) │ │
                │  │ (crypto) │ │ Manager  │ │ per bot   │ │
                │  └──────────┘ └──────────┘ └─────┬─────┘ │
                │                                   │       │
                │  ┌────────┬────────┬────────┬─────▼──┐   │
                │  │Extended│  GRVT  │Variat. │  Nado  │   │
                │  │ Client │ Client │ Client │ Client │   │
                │  └────────┴────────┴────────┴────────┘   │
                └──────────────────────────────────────────┘
```

**Request flow:**
1. User opens `bot.defitool.de` → Cloudflare Worker serves Vue SPA
2. Frontend API calls → Worker proxies to Orchestrator (port 8090) via VPC
3. Orchestrator routes `/api/*` to the correct per-user container (`ta-<user_id>`)
4. Container runs FastAPI with trading engines, exchange clients, and vault

---

## Tech Stack

### Backend
- **Python 3.11** — FastAPI + Uvicorn (auto-reload)
- **Pydantic v2** — Settings (`config.py`), request/response schemas (`schemas.py`)
- **Exchange SDKs** — `grvt-pysdk`, `x10-python-trading-starknet`, `curl_cffi`, `eth_account`
- **WebSockets** — `websockets` lib for exchange feeds
- **httpx** — Async HTTP client for exchange APIs
- **Crypto** — AES-256-GCM via PyCryptodome, PBKDF2-SHA256 key derivation

### Frontend
- **Vue 3** (Composition API + `<script setup>`)
- **TypeScript**
- **Vite** — Build tool
- **Pinia** — State management (`stores/`)
- **Vue Router** — Routing (`router/`)
- No Tailwind — custom CSS variables (`assets/styles/variables.css`)

### Infrastructure
- **Synology NAS** — Docker host (192.168.133.253)
- **Cloudflare Worker** — Frontend hosting + API proxy (`bot.defitool.de`)
- **Cloudflare D1** — Trade history database
- **Workers KV** — Static asset storage
- **Workers VPC** — Private network binding to NAS

---

## Project Structure

```
tradeautonom/
├── main.py                           # Entry point: uvicorn with auto-reload
├── requirements.txt                  # Python dependencies
├── .env.example                      # Template for local .env
│
├── app/                              # ═══ BACKEND ═══
│   ├── server.py                     #   FastAPI app: REST API, SSE, Auth, WebSocket
│   ├── engine.py                     #   FundingArbEngine: orchestrates per-bot trading
│   ├── arbitrage.py                  #   ArbitrageEngine: spread monitoring, signals
│   ├── state_machine.py              #   TradeStateMachine: TWAP maker→taker execution
│   ├── data_layer.py                 #   DataLayer: WS orderbook/position/fill feeds
│   ├── job_manager.py                #   JobManager: multi-job CRUD, persistence, tick loop
│   ├── bot_registry.py               #   BotRegistry: in-memory bot instance tracking
│   ├── config.py                     #   Pydantic Settings from .env
│   ├── schemas.py                    #   Pydantic request/response models
│   ├── risk_manager.py               #   RiskManager: delta limits, circuit breaker
│   ├── funding_monitor.py            #   FundingMonitor: polling funding rates
│   ├── safety.py                     #   Orderbook depth + slippage safety checks
│   ├── executor.py                   #   Legacy TradeExecutor (single-order)
│   ├── exchange.py                   #   ExchangeClient + AsyncExchangeClient protocols
│   ├── crypto.py                     #   AES-256-GCM vault encryption
│   ├── journal_collector.py          #   Trade journal/history collection
│   ├── ws_feeds.py                   #   WebSocket feed helpers
│   ├── extended_client.py            #   Extended Exchange (StarkNet / x10 SDK)
│   ├── grvt_client.py                #   GRVT Exchange
│   ├── variational_client.py         #   Variational DEX (RFQ, curl_cffi)
│   └── nado_client.py                #   Nado Exchange (EIP-712 signing)
│
├── frontend/                         # ═══ FRONTEND (Vue 3 SPA) ═══
│   ├── src/
│   │   ├── views/                    #   Page components (Dashboard, Settings, etc.)
│   │   ├── components/               #   Reusable UI components
│   │   ├── stores/                   #   Pinia stores (auth, bots, app, account)
│   │   ├── composables/              #   Vue composables (SSE streams)
│   │   ├── lib/                      #   API client, auth helpers
│   │   ├── types/                    #   TypeScript type definitions
│   │   └── router/                   #   Vue Router config
│   ├── vite.config.ts
│   └── package.json
│
├── deploy/                           # ═══ DEPLOYMENT ═══
│   ├── prod/                         #   Production (port 8002) — Dockerfile here
│   ├── v2/                           #   V2 staging (port 8004)
│   ├── v3/                           #   V3 multi-user (port 8005)
│   │   ├── deploy.sh                 #     Full Docker deploy for tradeautonom-v3
│   │   └── manage.sh                 #     Per-user container management (create/destroy/update)
│   ├── orchestrator/                 #   Orchestrator (port 8090)
│   ├── cloudflare/                   #   Cloudflare Worker (frontend + API proxy)
│   │   ├── src/                      #     Worker source (TypeScript)
│   │   └── wrangler.jsonc            #     Wrangler config
│   ├── dashboard/                    #   Read-only dashboard (port 8003)
│   └── local/                        #   Docker Compose for local dev
│
├── deploy.sh                         # ═══ HOT-DEPLOY ═══ (zero-downtime Python push)
├── docs/                             # Documentation
├── scripts/                          # Utility scripts
├── static/                           # Legacy static HTML
└── tests/                            # Tests
```

---

## Local Development Setup

### Prerequisites
- Python 3.11+
- Node.js 18+ (for frontend)
- Docker (optional, for containerized dev)

### Backend

```bash
# 1. Clone
git clone https://github.com/dhaussmann/tradeautonom.git
cd tradeautonom

# 2. Virtual environment
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. Environment
cp .env.example .env
# Edit .env — set at least APP_HOST, APP_PORT
# Exchange API keys are set via the UI vault, not .env

# 4. Run
python main.py
# → http://localhost:8000/ui (legacy static UI)
# → API at http://localhost:8000/
```

Uvicorn runs with `--reload` watching `/app/app/` — any Python file change auto-restarts.

### Frontend

```bash
cd frontend
npm install
npm run dev
# → http://localhost:5173 (Vite dev server)
```

The frontend dev server proxies API calls to the backend. For production, the frontend is built and deployed to Cloudflare Workers.

### Docker (local)

```bash
cd deploy/local
docker-compose up -d
# → http://localhost:8000/
```

---

## Backend (`app/`)

### Entry Point

`main.py` → loads `.env` → starts Uvicorn → mounts `app.server:app` (FastAPI).

### Key Modules

#### `server.py` — FastAPI Application
The main application file (~2600 lines). Responsibilities:
- **Lifespan**: Initializes settings, exchange clients, bot registry on startup
- **Auth/Vault**: Password setup, unlock/lock, encrypted secrets management
- **Bot CRUD**: Create, start, stop, configure bots
- **SSE Stream**: Real-time updates to the frontend (`/fn/stream/{bot_id}`)
- **Settings API**: Key management, exchange configuration
- **NADO Authorization**: Linked signer key generation and persistence

#### `engine.py` — FundingArbEngine
One engine instance per bot. Orchestrates:
- DataLayer initialization (orderbook + position feeds)
- FundingMonitor startup
- StateMachine creation for TWAP execution
- Pre-trade checks (min order size, auto-chunk-reduce)
- Entry/exit position logic

#### `state_machine.py` — TradeStateMachine
Implements the TWAP maker→taker execution flow:
1. Split total quantity into N chunks
2. Per chunk: place maker post-only order → chase/reprice on timeout → taker IOC hedge on fill
3. Position verification via REST after each chunk
4. Repair logic: IOC on taker side if delta gap detected

#### `arbitrage.py` — ArbitrageEngine
Monitors cross-exchange spreads, generates entry/exit signals for mean-reversion and delta-neutral strategies.

#### `data_layer.py` — DataLayer
Manages WebSocket connections to exchanges for real-time orderbook, position, and fill data. Falls back to REST polling when WS is unavailable.

#### `job_manager.py` — JobManager
Multi-job management: CRUD, persistence to disk (`data/bots/`), tick loop for auto-trading.

#### `config.py` — Settings
Pydantic Settings loaded from `.env`. All configuration has defaults. Exchange API keys are **not** stored here in production — they go through the encrypted vault.

#### `exchange.py` — Protocol Definitions
Defines `ExchangeClient` (legacy sync) and `AsyncExchangeClient` (new async) protocols that all exchange adapters must implement.

#### `crypto.py` — Vault Encryption
AES-256-GCM encryption with PBKDF2-SHA256 key derivation. File format: `salt(16) | nonce(12) | tag(16) | ciphertext`. Uses PyCryptodome with OpenSSL subprocess fallback.

---

## Frontend (`frontend/`)

### Architecture

Standard Vue 3 Composition API app:

- **Views** (`views/`): `DashboardView`, `SettingsView`, `BotDetailView`, `HistoryView`, `PositionsView`, `AccountView`, `MarketsView`, `StrategiesView`, `LoginView`, `AdminView`
- **Stores** (`stores/`): `auth` (vault state), `bots` (bot list/state), `app` (global), `account` (portfolio)
- **Composables** (`composables/`): `useBotStream` (SSE), `usePortfolioStream`
- **API Client** (`lib/api.ts`): Wraps fetch calls to backend

### Key Components
- `VaultScreen.vue` — Password setup/unlock screen
- `BotCard.vue` / `BotMiniCard.vue` — Bot status display
- `BotCreateModal.vue` — New bot creation wizard
- `AppHeader.vue` — Navigation header

### Build & Deploy

```bash
cd frontend
npm run build          # → frontend/dist/
# Deployed to Cloudflare Workers KV via deploy/cloudflare/deploy.sh
```

---

## Exchange Clients

All clients implement `AsyncExchangeClient` protocol from `exchange.py`.

| Exchange | Client File | Type | Auth | Special Notes |
|----------|------------|------|------|--------------|
| **Extended** | `extended_client.py` | CEX (StarkNet) | x10 SDK + API Key | WS orderbook + account stream |
| **GRVT** | `grvt_client.py` | CEX | REST + Cookie auth | WS fills, positions, orders |
| **Variational** | `variational_client.py` | DEX (RFQ) | JWT token (vr-token cookie) | `curl_cffi` for Cloudflare bypass, OLP maker quotes |
| **Nado** | `nado_client.py` | DEX | EIP-712 signing | Linked signer key, signed order placement + cancellation |

### Adding a New Exchange Client

1. Create `app/new_exchange_client.py`
2. Implement `AsyncExchangeClient` protocol (see `exchange.py`)
3. Required methods:
   - `async_fetch_order_book()`, `async_fetch_markets()`
   - `async_get_min_order_size()`, `async_get_tick_size()`
   - `async_create_post_only_order()`, `async_create_ioc_order()`
   - `async_cancel_order()`, `async_check_order_fill()`
   - `async_fetch_positions()`, `async_fetch_funding_rate()`
   - `async_subscribe_fills()`, `async_subscribe_funding_rate()`
4. Register in `server.py` → `_init_exchange_clients()`
5. Add settings to `config.py`

### Nado-Specific: EIP-712 Signing

Nado requires all order placement and cancellation to be signed with EIP-712 typed data:

- **Orders**: `Order` struct with `sender`, `priceX18`, `amount`, `expiration`, `nonce`, `appendix`
- **Cancellations**: `Cancellation` struct with `sender`, `productIds`, `digests`, `nonce`
- **Link Signer**: `LinkSigner` struct for authorization

The `sender` field is a `bytes32` derived from `wallet_address + subaccount_name`. The signing key is either the linked signer key (preferred) or the wallet private key.

### Variational-Specific: curl_cffi

Variational uses Cloudflare protection, so `requests` doesn't work. The client uses `curl_cffi` which impersonates a browser TLS fingerprint. Auth is via `vr-connected-address` header + `vr-token` cookie.

---

## Trading Engine & State Machine

### Bot Lifecycle

```
IDLE → ENTERING → HOLDING → EXITING → IDLE
         ↓                      ↓
       (TWAP)                 (TWAP)
```

### TWAP Execution Flow (per chunk)

```
1. Calculate chunk_qty = total_qty / num_chunks
2. Auto-reduce num_chunks if chunk_qty < min_order_size
3. For each chunk:
   a. Place MAKER post-only order at best bid/ask ± offset
   b. Wait for fill (maker_timeout_ms)
   c. If timeout → cancel → reprice (chase up to max_chase_rounds)
   d. On fill → place TAKER IOC hedge on opposite exchange
   e. Verify positions on both exchanges via REST
   f. If delta gap > min_repair_qty → IOC repair on taker side
4. Wait twap_interval_s between chunks
```

### Key Config Parameters (per bot)

| Parameter | Default | Description |
|-----------|---------|-------------|
| `fn_quantity` | 0.1 | Total position size |
| `fn_twap_num_chunks` | 10 | Number of TWAP chunks |
| `fn_twap_interval_s` | 10.0 | Seconds between chunks |
| `fn_maker_timeout_ms` | 10000 | Max wait for maker fill |
| `fn_maker_max_chase_rounds` | 5 | Max reprice attempts |
| `fn_delta_max_usd` | 50.0 | Max delta imbalance |
| `fn_max_spread_pct` | 0.05 | Max spread % to allow entry |

---

## Vault & Secret Management

### How It Works

1. User sets a **vault password** via UI → creates `data/auth.json` (PBKDF2 hash)
2. User enters API keys → encrypted with AES-256-GCM → saved to `data/secrets.enc`
3. On container restart, vault auto-unlocks using a session cookie (`data/.vault_session`)
4. Keys are held in RAM (`_current_password`) while vault is unlocked
5. On lock, keys are wiped from RAM

### File Layout (per container)

```
/app/data/
├── auth.json          # Password verification hash
├── secrets.enc        # Encrypted API keys (AES-256-GCM)
├── .vault_session     # Encrypted session for auto-unlock after restart
└── bots/              # Per-bot state (JSON configs, trade logs)
    ├── bot-abc123/
    │   ├── config.json
    │   └── trades.json
    └── ...
```

### Important: Container Data Persistence

Each container mounts a **persistent volume** for `/app/data/`:
- V3 (8005): `-v /volume1/docker/tradeautonom-v3/data:/app/data`
- User containers: `-v /volume1/docker/tradeautonom-v3/data-<user_id>/app-data:/app/data`

**If this volume is lost (e.g., container rebuild without proper mount), all vault data including API keys is gone.** The user must re-enter everything.

---

## Deployment

### Overview

| Method | When | Downtime | Scope |
|--------|------|----------|-------|
| **Hot-Deploy** (`deploy.sh`) | Python-only changes | Zero | Containers sharing `/app/` mount |
| **Full Deploy** (`deploy/*/deploy.sh`) | Dockerfile/deps/infra changes | Brief (~30s) | Single container type |
| **Frontend Deploy** (`deploy/cloudflare/deploy.sh`) | Vue/Worker changes | Zero | Cloudflare Worker |
| **User Container Update** (`manage.sh update`) | Image rebuild for user containers | Brief | All user containers |

### Hot-Deploy (Most Common)

```bash
./deploy.sh                            # All app/*.py files
./deploy.sh nado_client.py engine.py   # Specific files
```

- Copies files to `/volume1/docker/tradeautonom/app/` on NAS
- Uvicorn auto-reload detects changes → restarts in ~3 seconds
- **Safety check**: Aborts if any bot is in ENTERING or EXITING state
- **Scope**: Only affects containers with the shared code volume mount

**⚠ IMPORTANT**: Hot-deploy does NOT reach user containers (9001-9003) by default! They run code baked into the Docker image. See [Known Issues](#known-issues--gotchas).

### Full Docker Deploy

```bash
# V3 main instance (port 8005)
./deploy/v3/deploy.sh                  # sync + build + restart
./deploy/v3/deploy.sh --restart        # restart only
./deploy/v3/deploy.sh --logs           # tail logs
./deploy/v3/deploy.sh --status         # health check

# Other instances
./deploy/prod/deploy.sh                # Production (port 8002)
./deploy/v2/deploy.sh                  # V2 (port 8004)
./deploy/dashboard/deploy.sh           # Dashboard (port 8003)
ORCH_TOKEN=xxx ./deploy/orchestrator/deploy.sh  # Orchestrator (port 8090)
```

**Steps performed by a full deploy:**
1. `cmd_sync` — tar project → extract on NAS (excludes `.venv`, `.git`, `data`, `.env`)
2. `cmd_build` — `docker build` on NAS using `deploy/prod/Dockerfile`
3. `cmd_up` — stop old container → start new one with volume mounts + env

### User Container Management

```bash
# Create new user
./deploy/v3/manage.sh create <user_id> [--port 9004] [--env-file user.env]

# List all users
./deploy/v3/manage.sh list

# Start/stop/destroy individual users
./deploy/v3/manage.sh start <user_id>
./deploy/v3/manage.sh stop <user_id>
./deploy/v3/manage.sh destroy <user_id>    # ⚠ Deletes all data!

# Rebuild image + restart ALL user containers
./deploy/v3/manage.sh update
```

### Frontend Deploy (Cloudflare Worker)

```bash
./deploy/cloudflare/deploy.sh              # Build Vue + deploy Worker
./deploy/cloudflare/deploy.sh --worker     # Worker only
./deploy/cloudflare/deploy.sh --build      # Build only
```

### Typical Deployment Scenarios

| Scenario | Commands |
|----------|----------|
| Python-only change | `./deploy.sh engine.py state_machine.py` |
| Python + frontend | `./deploy.sh engine.py` then `./deploy/cloudflare/deploy.sh` |
| Dependency change | `./deploy/v3/deploy.sh` (full rebuild) |
| User container update | `./deploy/v3/manage.sh update` |
| Quick restart | `./deploy/v3/deploy.sh --restart` |

---

## Infrastructure

### Container Map

| Container | Port | Image Tag | Volume Mounts | Deploy Script |
|-----------|------|-----------|---------------|---------------|
| `tradeautonom-v3` | 8005 | `v3` | `data:/app/data` | `deploy/v3/deploy.sh` |
| `ta-<user_id>` | 9001+ | `v3` | `app-data:/app/data` | `deploy/v3/manage.sh` |
| `tradeautonom` | 8002 | `latest` | `data:/app/data` | `deploy/prod/deploy.sh` |
| `tradeautonom-v2` | 8004 | `v2` | `data:/app/data` | `deploy/v2/deploy.sh` |
| `tradeautonom-dashboard` | 8003 | `latest` | — | `deploy/dashboard/deploy.sh` |
| `ta-orchestrator` | 8090 | `latest` | — | `deploy/orchestrator/deploy.sh` |

### Cloudflare

| Resource | Value |
|----------|-------|
| Worker name | `tradeautonom` |
| Route | `bot.defitool.de/*` |
| Zone | `defitool.de` |
| D1 Database | `tradeautonom-history` |
| VPC Service | binds to `192.168.133.253:8090` |
| Secrets | `INGEST_TOKEN`, `BETTER_AUTH_SECRET`, `ORCH_TOKEN` |

### Dockerfile

All backend containers share `deploy/prod/Dockerfile`:
- Base: `python:3.11-slim`
- Installs gcc, pip dependencies from `requirements.txt`
- Copies `app/`, `static/`, `main.py`
- Healthcheck: HTTP GET to `/health`
- CMD: `python main.py`

---

## API Reference (Key Endpoints)

### Auth
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/auth/setup` | Set vault password (first time) |
| `POST` | `/auth/unlock` | Unlock vault with password |
| `POST` | `/auth/lock` | Lock vault (wipe keys from RAM) |
| `GET` | `/auth/status` | Check if vault is locked/unlocked |

### Bots
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/fn/bots` | List all bots |
| `POST` | `/fn/create` | Create new bot |
| `POST` | `/fn/{bot_id}/start` | Start bot |
| `POST` | `/fn/{bot_id}/stop` | Stop bot |
| `PUT` | `/fn/{bot_id}/config` | Update bot config |
| `GET` | `/fn/stream/{bot_id}` | SSE stream (real-time updates) |

### Settings
| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/settings/keys` | Update API keys |
| `GET` | `/settings/keys` | Get masked API key status |
| `POST` | `/nado/authorize` | Generate NADO linked signer key |

### Market Data
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/fn/markets/{exchange}` | Fetch available markets |
| `GET` | `/fn/orderbook/{exchange}/{symbol}` | Fetch orderbook |
| `GET` | `/fn/positions/{exchange}` | Fetch positions |

### Health
| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Container health check |

---

## Known Issues & Gotchas

### 1. Hot-Deploy Does Not Reach User Containers (9001-9003)

**Problem**: User containers run code from the Docker image. `./deploy.sh` only updates the shared volume mounted by the V3 main container (8005).

**Workaround**: After hot-deploying, also run `./deploy/v3/manage.sh update` to rebuild the image and restart user containers.

**Proper fix**: Add a shared code volume mount to `manage.sh` so user containers also pick up hot-deploys. See `docs/FIX_USER_CONTAINERS.md`.

### 2. Vault Data Loss on Container Rebuild

If a container is destroyed and recreated (e.g., during `manage.sh update`), the `/app/data/` volume must be correctly re-mounted. If the mount path changes or is omitted, `auth.json` and `secrets.enc` are lost.

### 3. Nado EIP-712 Signing

All Nado operations (order placement, cancellation) require EIP-712 typed data signatures. The signing key is the **linked signer key** (generated during authorization), not the wallet private key. If the linked signer key is lost or mismatched, re-authorize via Settings → NADO → Re-authorize.

### 4. Nado Min Order Size is USD Notional

Nado's `min_size` field from the `/symbols` endpoint is a **USD notional minimum**, not a quantity minimum. The client converts it to quantity using: `min_qty = ceil(min_notional / mid_price, size_increment)`.

### 5. Variational Requires curl_cffi

Standard `requests` library is blocked by Cloudflare. The Variational client uses `curl_cffi` which impersonates browser TLS fingerprints.

### 6. TWAP Auto-Chunk-Reduce

If `total_qty / num_chunks < min_order_size`, the engine automatically reduces `num_chunks` so each chunk meets the minimum. This is logged as "Auto-reducing num_chunks X → Y".

---

## Testing

```bash
# Run tests (from project root)
python -m pytest tests/

# Test a specific exchange client
python -m pytest tests/test_cf_worker_proxy.py

# Manual test: check exchange connectivity
curl http://localhost:8000/fn/markets/nado
curl http://localhost:8000/fn/orderbook/nado/SOL-PERP
```

---

## Troubleshooting

### Container Logs

```bash
# Via deploy scripts
./deploy/v3/deploy.sh --logs
./deploy/v3/manage.sh logs <user_id>

# Via Synology Docker UI
# Container Manager → select container → Log tab
```

### Common Errors

| Error | Cause | Fix |
|-------|-------|-----|
| `Signature does not match` | Stale NADO linked signer key | Re-authorize in Settings |
| `Qty X below min order size Y` | TWAP chunk too small | Reduce `num_chunks` or increase `quantity` |
| `product ID must be for a spot market` | `spot_leverage` flag on perp order | Fixed in code — ensure latest version |
| `422 Unprocessable Entity` (cancel) | Unsigned cancel request | Fixed — requires EIP-712 signed `cancel_orders` |
| `Token expired` (Variational) | JWT expired or vault data missing | Re-enter JWT in Settings |
| `Vault is locked` | Auto-unlock failed | Unlock via UI or check `.vault_session` |

### Health Check

```bash
# Check all containers
for port in 8005 9001 9002 9003; do
  echo "Port $port: $(curl -sf http://192.168.133.253:$port/health || echo OFFLINE)"
done
```
