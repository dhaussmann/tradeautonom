# TradeAutonom — Infrastructure Documentation

## Overview

TradeAutonom is a multi-tenant platform for automated funding arbitrage on decentralized perpetual exchanges. The architecture consists of three layers:

```
┌─────────────────────────────────────────────────┐
│            Cloudflare Worker (Edge)              │
│  • Vue SPA (KV)  • Auth (better-auth)           │
│  • D1 Database   • API Routing                  │
└────────────────────┬────────────────────────────┘
                     │ VPC Tunnel
┌────────────────────▼────────────────────────────┐
│          Orchestrator (NAS, Port 8090)           │
│  • Container Lifecycle  • Proxy  • Watchdog      │
└────────────────────┬────────────────────────────┘
                     │ Docker API
┌────────────────────▼────────────────────────────┐
│         Docker Container (per User)              │
│  • Trading Engine  • Exchange Clients            │
│  • WebSocket Feeds • State Persistence           │
└─────────────────────────────────────────────────┘
```

---

## 1. Cloudflare Worker

The Worker runs on Cloudflare's edge network and is the only publicly reachable endpoint (`bot.defitool.de`).

### Responsibilities

- **Static Assets**: The Vue frontend is served from Workers KV (SPA with `index.html` fallback).
- **Authentication**: [better-auth](https://better-auth.com/) backed by D1. Supports email/password registration and login. Sessions last 30 days, refreshed every 24h.
- **API Routing**: All `/api/*` requests are authenticated and routed to the correct backend container.
- **History & Journal**: Data like trades, equity snapshots, fills, and funding payments are stored and read directly from D1 — no container required.
- **Secrets Management**: Exchange API keys are stored encrypted in D1 (see Section 3).

### Database (D1)

Cloudflare D1 is a serverless SQLite database. Tables:

| Table | Purpose |
|-------|---------|
| `user` | User accounts (ID, name, email) |
| `session` | Login sessions |
| `account` | Auth provider data |
| `user_container` | Mapping user → Docker container (port, name, status) |
| `user_secrets` | Encrypted API keys per user |
| `equity_snapshots` | Equity time series |
| `position_snapshots` | Position time series |
| `trades` | Detected closed trades |
| `journal_*` | Orders, fills, funding, points, positions |

---

## 2. Container Architecture

### Why Docker Containers?

Each user gets a dedicated Docker container for the following reasons:

1. **Isolation**: API keys, positions, and trading state are fully isolated between users. A bug or crash only affects a single user.
2. **Security**: API keys are only decrypted and held inside the user's own container — never in the shared Worker.
3. **Resource Control**: Each container has a fixed memory limit (512 MB) and CPU quota (0.5 cores). One user cannot impact another's resources.
4. **State Persistence**: Position state, timers, and bot configuration are persisted in the container volume (`/app/data`) and survive restarts.
5. **Independent Upgrades**: Containers can be updated individually without affecting other users.

### Container Contents

Each container runs a FastAPI server containing:

- **Trading Engine** (`engine.py`): Orchestrates entry, holding, and exit of positions.
- **State Machine** (`state_machine.py`): Maker-Taker TWAP execution.
- **Exchange Clients**: API clients for Extended, GRVT, and Variational.
- **DataLayer** (`data_layer.py`): Real-time WebSocket orderbook feeds.
- **Funding Monitor** (`funding_monitor.py`): Monitors funding rates.
- **Risk Manager** (`risk_manager.py`): Continuous risk monitoring.
- **Journal Collector**: Collects fills, funding, and points and sends them to D1.

### Orchestrator

The Orchestrator is a FastAPI service on the NAS (port 8090) that controls the Docker daemon:

- **Auto-Provisioning**: On a user's first login, the CF Worker automatically creates a container via the Orchestrator.
- **Proxy**: Routes API requests from the Worker to the correct container based on the user's port.
- **Watchdog**: Checks all container statuses every 60 seconds. Crashed containers are auto-restarted (max 3 restarts within 5 minutes, then `crash_loop` status).
- **Lifecycle**: Start, stop, delete, logs, and stats per container.

### Container Lifecycle

```
User registers
        ↓
First API call → CF Worker detects: no container
        ↓
Worker calls POST /orch/containers
        ↓
Orchestrator creates Docker container:
  • Image: tradeautonom:v3
  • Port: auto-assigned (starting at 9001)
  • Volume: ta-data-{user_id}
  • Env: USER_ID, APP_PORT
        ↓
Worker polls for health check (max 20s)
        ↓
Container ready → D1 entry in user_container
        ↓
API keys from D1 are auto-injected
```

---

## 3. Secrets / API Key Management

### Encryption

API keys are **never stored in plaintext**. The flow:

1. User enters API keys in the frontend (Settings page).
2. Keys are sent to the CF Worker over HTTPS.
3. Worker encrypts the keys with **AES-256-GCM**:
   - Key: Derived via **PBKDF2** (100,000 iterations, SHA-256) from the `ENCRYPTION_KEY` (Cloudflare Secret).
   - Each encryption operation generates a random salt (16 bytes) and IV (12 bytes).
   - Format in D1: `base64(salt[16] | iv[12] | ciphertext + GCM-tag)`
4. Encrypted blob is stored in `user_secrets`.

### Managed Keys

| Key | Exchange | Purpose |
|-----|----------|---------|
| `extended_api_key` | Extended | API authentication |
| `extended_public_key` | Extended | Signing |
| `extended_private_key` | Extended | Signing |
| `extended_vault` | Extended | Vault ID |
| `grvt_api_key` | GRVT | API authentication |
| `grvt_private_key` | GRVT | Signing |
| `grvt_trading_account_id` | GRVT | Account ID |
| `variational_jwt_token` | Variational | JWT token |

### Auto-Injection

On container start or login, keys are automatically injected into the container:

1. CF Worker reads encrypted blob from D1.
2. Decryption in the Worker (PBKDF2 + AES-256-GCM).
3. Keys are sent via `POST /internal/apply-keys` to the container (over VPC tunnel, not publicly accessible).
4. Container initializes exchange clients with the keys.

### Masking

In the frontend, keys are always shown masked (`***abcd`). Only the last 4 characters are visible. Updates are merged with existing keys — unchanged fields are preserved.

---

## 4. Network & Security

### VPC Tunnel

The connection between the Cloudflare Worker and NAS runs through a **Cloudflare VPC Service** (Workers VPC). The NAS is not directly reachable from the internet.

### Authentication Layers

| Layer | Mechanism |
|-------|-----------|
| User → Worker | better-auth session cookie (HTTP-only) |
| Worker → Orchestrator | `X-Orch-Token` header (shared secret) |
| Worker → Container | Via Orchestrator proxy (no direct access) |
| History/Journal Ingest | `INGEST_TOKEN` header |
| Admin endpoints | Session + email whitelist (`ADMIN_EMAILS`) |

### Secrets (Cloudflare)

The following secrets are configured via `wrangler secret put`:

- `BETTER_AUTH_SECRET` — Session signing
- `ENCRYPTION_KEY` — AES key for API keys
- `INGEST_TOKEN` — Auth for history/journal ingest
- `ORCH_TOKEN` — Auth for orchestrator communication
- `ADMIN_EMAILS` — Comma-separated admin email list

---

## 5. Deployment

### Build & Deploy

```bash
cd deploy/cloudflare
bash deploy.sh
```

The script:
1. Builds the Vue frontend (`npx vite build`)
2. Installs Worker dependencies (`npm ci`)
3. Deploys Worker + assets via `wrangler deploy`

### Updating the Container Image

```bash
# On the NAS:
docker build -t tradeautonom:v3 -f Dockerfile.nas .

# Restart all containers:
# The Orchestrator watchdog detects stopped containers and restarts them with the new image
```

### D1 Migrations

```bash
cd deploy/cloudflare
npx wrangler d1 migrations apply tradeautonom-history --remote
```
