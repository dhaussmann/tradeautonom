# AGENTS.md

Multi-exchange arbitrage trading bot (FastAPI backend, Vue 3 SPA frontend, Cloudflare Worker edge). Referenced in all OpenCode sessions.

## 1. Where to read deeper

| Purpose | Document |
|---------|----------|
| Architecture overview | `CLAUDE.md`, `docs/architecture.md` |
| Engineering deep-dive | `docs/DEVELOPER_GUIDE.md` |
| Ops runbook / NAS topology | `docs/DEPLOYMENT.md` |
| Strategy + P&L | `docs/STRATEGY_GUIDE.md` |
| Execution / TWAP / state machine | `docs/taker-execution.md`, `docs/safety-balance-verification.md` |
| WS connection rules | `docs/WEBSOCKET_ARCHITECTURE.md` |
| OMS + opt-in features | `docs/V5_OMS_AND_FEATURES.md` |
| Multi-user vault / key lifecycle | `docs/API_KEY_LIFECYCLE.md` |
| DNA bot | `docs/dna-bot.md` |
| Nado OMS watchdog | `docs/NADO_WATCHDOG_WEBSOCKET.md` |
| GRVT endpoints | `.claude/rules/grvt_api.md` |
| Recent fixes to avoid regressing | `docs/WEBSOCKET_REFACTOR_FIXES.md`, `RELEASENOTES.md` |
| V2 on Cloudflare (OMS Phase E live; UserContainer F.1–F.3 deployed: routing via `user.backend` flag is live) | `docs/v2-cf-containers-architecture.md`, `docs/v2-oms-cloudflare-native.md`, `deploy/cf-containers/oms-v2/` (OMS), `deploy/cf-containers/user-v2/` (user container), `deploy/cf-containers/proof-of-concept/` (research) |
| **Do not treat as current reference** | `docs/delta-neutral-algorithm.docx.md` (design spec), `docs/tradeautonom-integrationsplan-v2.docx.md` (plan), `docs/FIX_USER_CONTAINERS.md` (proposal), `docs/V4_WEB_DEVELOPER_GUIDE.md` (external API doc) |

## 2. Entry points (not obvious from filenames)

- **Backend:** `main.py` → `app.server:app` (uvicorn reloads `/app/app` inside container).
- **Frontend:** canonical UI is `frontend/` (Vue 3 SPA) → `frontend/dist/` → Cloudflare Worker `site.bucket`.
- **Legacy UI:** `static/index.html` + `static/dashboard.html` served at `/ui` (not the Vue app).
- **Orchestrator/OMS/Monitor:** separate services under `deploy/orchestrator/` and `deploy/monitor/` (not part of `app/`).
- **DNA bot import:** wrapped in try/except in `server.py` — missing deps log warning, don't crash startup. Preserve this pattern.

## 3. Dev commands agents get wrong

```bash
# Backend (loads .env via python-dotenv; not uvicorn CLI)
python main.py

# Frontend dev (Vite proxy defaults to stale IP; override in frontend/.env)
cd frontend && npm run dev

# Frontend build runs vue-tsc first — type errors fail the build
cd frontend && npm run build   # → frontend/dist/

# Local Docker (compose port 8002 overrides .env.example's 8000)
cd deploy/local && docker-compose up -d

# Tests: only one formal test exists; no pytest/ruff/mypy configured
python -m pytest tests/test_cf_worker_proxy.py   # needs VARIATIONAL_JWT
# test_nado_*.py at repo root are ad-hoc scripts, not a test suite
```

## 4. NAS & deployment truth

- **Canonical NAS:** `root@192.168.133.100`, deploy path `/opt/tradeautonom`, SSH key `~/.ssh/id_ed25519`.
- All `deploy/*/deploy.sh` read `NAS_HOST`, `NAS_USER`, `NAS_DEPLOY_PATH` from repo-root `.env`.
- **Deprecated:** root `./deploy.sh` hard-codes `192.168.133.253` — **do not use**. Prefer:
  - `./deploy/v3/manage.sh deploy-code` — hot-reload all v3 + user containers via shared mount `/opt/tradeautonom-v3/app`.
  - `./deploy/v3/manage.sh update` — rebuild image + restart all user containers.
- Stale IP `192.168.133.253` also appears in `frontend/vite.config.ts` default, `V5_OMS_AND_FEATURES.md`, `infrastructure-*.md`. Trust CLAUDE.md / DEPLOYMENT.md: **`.100` is canonical**.
- All deploy paths **abort if any bot is non-IDLE** (probed via `/fn/bots`). Never deploy during a live trade.
- Typical release: `./deploy/v3/manage.sh update` → SSH loop restart `ta-user-*` → `./deploy/cloudflare/deploy.sh`.
- **Staging rule:** all code is tested on `tradeautonom-v3` (port 8005) first. Only after it verifies there do changes go out to the `ta-user-*` containers.
- **Note:** `docs/FIX_USER_CONTAINERS.md` is a proposal. Verify that `ta-user-*` containers actually mount `/opt/tradeautonom-v3/app` before relying on hot-reload.

## 5. Container / port / image map

| Container | Port | Image | Notes |
|-----------|------|-------|-------|
| `tradeautonom` | 8002 | `tradeautonom:latest` | Prod; code baked in |
| `tradeautonom-v2` | 8004 | `tradeautonom:v2` | Test/staging |
| `tradeautonom-v3` | 8005 | `tradeautonom:v3` | Shared RO code mount |
| `ta-user-*` | 9001–9008 (observed) | `tradeautonom:v3` | Per-user data volume + shared RO code; port assigned by `manage.sh create`; memory-capped at 1 GiB |
| `tradeautonom-dashboard` | 8003 | — | Read-only view |
| `ta-orchestrator` | 8090 | — | Requires `X-Orch-Token` header |
| `oms` / `ta-monitor` | 8099 | — | Shared orderbook monitor |
| `portainer` | 8000, 9443 | — | Not part of TradeAutonom; avoid port 8000 collisions |

Host is a **Photon OS VM** (`photon-machine`, 192.168.133.100), not Synology. Older docs (`V5_OMS_AND_FEATURES.md`, `infrastructure-*.md`) still describe Synology; those notes are pre-migration. `host.docker.internal` is still unreliable — always hardcode `192.168.133.100` for cross-container references.

**Container init / zombie-reaping:** the `tradeautonom:*` images use `tini` as `ENTRYPOINT` (PID 1). Docker `HEALTHCHECK` forks a Python subprocess every 30s; without a proper init, PID 1 (`main.py`) never reaps those forks and 500+ zombies accumulate per day → eventual OOM-kill on the cgroup. Never remove the `ENTRYPOINT ["/usr/bin/tini", "--"]` from the Dockerfiles or the `--init` flag from `manage.sh`/`deploy.sh` `docker run` invocations. See `docs/extended-builder-codes.md` peer doc for related container config.

## 6. Vault / key model (multi-user D1)

- Keys live only in **container RAM** (never on disk by design).
- Cloudflare Worker stores AES-256-GCM-encrypted blobs in D1 `user_secrets` (PBKDF2-SHA256, 100k iterations).
- On every `/api/auth/status`, Worker re-injects keys via Orchestrator → `POST /internal/apply-keys`.
- **Gap:** between container restart and first `/auth/status` hit, bots cannot trade (no keys in RAM yet).
- `docs/DEVELOPER_GUIDE.md` describes a legacy `secrets.enc` on-disk model — that applies only to standalone prod/v2/v3 containers, **not** to the current multi-user `ta-user-*` + Cloudflare Worker path. When working on multi-user, trust `docs/API_KEY_LIFECYCLE.md`.

## 7. Runtime quirks

- All settings via pydantic-settings `Settings` in `app/config.py` (~140 keys). Add new fields with defaults; never hardcode.
- `fn_enabled: bool = True` — legacy `_fn_engine` kill-switch. With SharedAuthWS active, set `FN_ENABLED=false` in container `.env` (Bug 8).
- Simulation mode: `ARB_SIMULATION_MODE=True` or `FN_SIMULATION_MODE=True`.
- Use `Decimal` for all price/qty math at exchange boundaries; floats cause phantom gaps (documented in `RELEASENOTES.md` and `safety-balance-verification.md`).
- Preserve DNA bot try/except import pattern in `server.py`.
- Bot state: Kill leaves positions open (must close manually); Reset resets state only (no trading); timer state survives container restarts.

## 8. WebSocket invariants (regression traps)

- **One SharedAuthWS per exchange; one Fill WS per exchange client.** A second authenticated connection with the same creds gets rejected by the exchange.
- Never run a second DataLayer with explicit `symbols_map` while SharedAuthWS is active.
- Use `async with websockets.connect(...)` — **not** `async for ws in websockets.connect(...)` which swallows HTTP 400 and breaks fallback/retry counting (Bug 7).
- OMS Extended feed: single shared WS to `/v1/orderbooks` (no market param). Do not reintroduce per-symbol connections (causes HTTP 429).

**Canonical endpoints:**
- Extended account stream: `wss://api.starknet.extended.exchange/stream.extended.exchange/v1/account` (header `X-Api-Key`)
- Nado: `wss://gateway.prod.nado.xyz/v1/subscribe` (not `v1/v1/...` — Bug 2). **Requires `Sec-WebSocket-Extensions: permessage-deflate`** (HTTP 403 otherwise). Also requires 30s WS ping. V2-OMS cannot connect directly from a Worker because CF Workers do not negotiate extensions; in V2 this is solved by `NadoRelayContainer` (Node.js `ws` library, `deploy/cf-containers/oms-v2/container/nado-relay/`). Do NOT attempt to re-introduce a direct `fetch(wss://...)` path in `NadoOms` DO.
- GRVT: `wss://market-data.grvt.io/ws/full` + `wss://trades.grvt.io/ws/full`

## 9. Execution / safety invariants

- **Snapshot baseline positions before chunk 1** — skipping triggers cascading phantom-gap repair.
- Gap authority = exchange position - target; never use `max(pos_gap, chunk_gap)`.
- Extended fills arrive in multiple rapid WS batches → `_wait_for_maker_fill` must 300ms settle + REST confirm (Bug 10).
- Taker IOC uses best ± **50 ticks** (literal), defining outer bound; actual fill is best available.
- TWAP auto-reduces `num_chunks` when `chunk_qty < min_order_size` (logged as "Auto-reducing...").
- Sync `GrvtCcxt.refresh_cookie()` must run via `asyncio.to_thread` (Bug 6).
- Circuit breaker reset only via `POST /fn/bots/{bot_id}/risk/reset-halt`.

## 10. Exchange client landmines

- **Extended:** use public properties (`markets_info`, `stark_account`, `config`); never reintroduce `__…` name-mangled access. Defensive `Decimal(str(x))` wrapping in `_round_qty`/`_round_price`. **Builder codes:** orders carry `builder_id` + `builder_fee` when `EXTENDED_BUILDER_ENABLED=true` (default, `extended_builder_id=177174`, `extended_builder_fee=0.00007`). Config lives in `app/config.py`, not the vault; non-secret. SDK kwargs `builder_id` / `builder_fee` are supported by `place_order` and `create_order_object`. Reference: https://docs.extended.exchange/extended-resources/builder-codes.
- **Nado:** EIP-712 signing required for orders + cancels; signing key is the **linked signer key**, not wallet key. `min_size` is USD notional → convert via `ceil(min_notional / mid_price, size_increment)`. Some symbols (ZRO, ZEC, XMR) deliver inverted bid/ask — detect and swap.
- **Variational:** requires `curl_cffi` (browser TLS impersonation); `requests` is blocked by Cloudflare. Auth via `vr-connected-address` header + `vr-token` cookie; use proxy `https://proxy.defitool.de/api`. JWT expires → re-enter in Settings.
- **GRVT:** see `.claude/rules/grvt_api.md` for WS endpoints.

## 11. Do / don't

| Do | Don't |
|---|---|
| Keep `Decimal` at exchange boundaries | Mix floats into price/qty math |
| Set `FN_ENABLED=false` when SharedAuthWS is active | Let legacy `_fn_engine` compete for Extended slots |
| Use `async with websockets.connect(...)` | Use `async for ws in websockets.connect(...)` |
| Snapshot baseline before chunk 1 | Skip snapshot (causes runaway repair) |
| Trust exchange position as gap authority | Use `max(pos_gap, chunk_gap)` |
| Prefer `./deploy/v3/manage.sh deploy-code` | Use root `./deploy.sh` (stale IP) |
| Verify `ta-user-*` mounts shared code before relying on hot-reload | Assume all user containers hot-reload by default |
| Treat `docs/API_KEY_LIFECYCLE.md` as vault source of truth (multi-user) | Assume `docs/DEVELOPER_GUIDE.md` `secrets.enc` model (legacy) |
| Treat `docs/delta-neutral-algorithm.docx.md`, `docs/tradeautonom-integrationsplan-v2.docx.md`, `docs/FIX_USER_CONTAINERS.md`, `docs/V4_WEB_DEVELOPER_GUIDE.md` as design/historical | Treat those four as current implementation references |
