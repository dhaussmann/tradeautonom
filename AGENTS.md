# AGENTS.md

Multi-exchange arbitrage trading bot (FastAPI backend, Vue 3 SPA frontend, Cloudflare Worker edge). Referenced in all OpenCode sessions.

## 1. Where to read deeper

| Purpose | Document |
|---------|----------|
| Architecture overview | `CLAUDE.md`, `docs/architecture.md`, `docs/v2-cf-containers-architecture.md` |
| Engineering deep-dive | `docs/DEVELOPER_GUIDE.md` |
| Ops runbook (V2 / Cloudflare — canonical) | `docs/DEPLOYMENT.md` |
| Strategy + P&L | `docs/STRATEGY_GUIDE.md` |
| Execution / TWAP / state machine | `docs/taker-execution.md`, `docs/safety-balance-verification.md` |
| WS connection rules | `docs/WEBSOCKET_ARCHITECTURE.md` |
| OMS + opt-in features | `docs/V5_OMS_AND_FEATURES.md`, `docs/v2-oms-cloudflare-native.md` |
| Multi-user vault / key lifecycle | `docs/API_KEY_LIFECYCLE.md` |
| DNA bot | `docs/dna-bot.md` |
| Nado OMS watchdog | `docs/NADO_WATCHDOG_WEBSOCKET.md` |
| GRVT endpoints | `.claude/rules/grvt_api.md` |
| Recent fixes to avoid regressing | `docs/WEBSOCKET_REFACTOR_FIXES.md`, `RELEASENOTES.md` |
| V2 deploy targets (canonical) | `deploy/cf-containers/oms-v2/` (OMS), `deploy/cf-containers/user-v2/` (per-user trading container), `deploy/cloudflare/` (Frontend + Auth Worker) |
| **Do not treat as current reference** | `docs/delta-neutral-algorithm.docx.md` (design spec), `docs/tradeautonom-integrationsplan-v2.docx.md` (plan), `docs/FIX_USER_CONTAINERS.md` (V1/NAS proposal), `docs/V4_WEB_DEVELOPER_GUIDE.md` (external API doc) |

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

## 4. Deployment truth (V2 / Cloudflare — canonical)

The **only supported deploy target** is V2 on Cloudflare. The legacy V3
NAS stack (`deploy/v3/manage.sh`, `ta-user-*` on `192.168.133.100`) is
**deprecated** and must not be used for new releases. See
`docs/DEPLOYMENT.md` Anhang A for historical NAS commands if a legacy
container has to be touched.

- **UserContainer V2** — `deploy/cf-containers/user-v2/` (`user-v2.defitool.de`).
  Deploy: `cd deploy/cf-containers/user-v2 && npm run deploy`.
  Code is **baked into the image** — there is no shared-mount hot-reload
  in V2.
- **OMS V2** — `deploy/cf-containers/oms-v2/` (`oms-v2.defitool.de`).
  Deploy: `cd deploy/cf-containers/oms-v2 && npm run deploy`.
- **Frontend + Auth Worker** — `deploy/cloudflare/` (`bot.defitool.de`).
  Deploy: `./deploy/cloudflare/deploy.sh`.
- **CRITICAL: Bump `V2_BUILD_TAG`** in
  `deploy/cf-containers/user-v2/container/Dockerfile` on every Python
  code change. Without a fresh tag, BuildKit caches the `COPY app/`
  layer and a successful-looking `wrangler deploy` ships the old code.
  Convention: `<feature>-vNN`.
- **When new code goes live:** CF does NOT replace running container
  instances atomically. New code is loaded on the next **cold start** of
  a user's container — automatic on idle timeout, or forced via
  `POST https://user-v2.defitool.de/admin/recycle/<user_id>` with header
  `X-Internal-Token: <V2_SHARED_TOKEN>`.
- **State persistence:** R2 bucket `tradeautonom-user-state`. Each user
  has one tarball (`<user_id>.tar.gz`) containing `data/` (auth.json,
  secrets.enc, bots/, dna_bot/, …). `app/cloud_persistence.py` restores
  on cold start and flushes every 30s via `/__state/{restore,flush}` on
  the user-v2 Worker.
- **Per-user backend routing:** D1 `user.backend` field decides the
  proxy target — `v2` (default) → `user-v2.defitool.de`, `nas`
  (legacy) → NAS container. Bestehende NAS-User werden nach und nach auf
  V2 migriert.
- **Do not run** `./deploy.sh` (root, hardcoded `192.168.133.253`),
  `./deploy/v3/manage.sh ...` for new releases, or any
  `ssh root@192.168.133.100 docker restart ...` workflow. Those paths
  exist only for historical NAS containers under `user.backend = "nas"`.

## 5. V2 component / domain / image map

| Component | Domain | Image / Class | Notes |
|-----------|--------|---------------|-------|
| Frontend + Auth Worker | `bot.defitool.de` | `tradeautonom` Worker + `frontend/dist/` assets | Vue 3 SPA, vue-tsc gates the build |
| User trading containers | `user-v2.defitool.de` | `user-v2-usercontainer` (DO `UserContainer`) | Per-user, `instance_type: standard-1`, `max_instances: 25` |
| OMS / Arb scanner | `oms-v2.defitool.de` | `oms-v2` Worker + `AggregatorDO` + `ArbScannerDO` + `NadoOmsDO` + `NadoRelayContainer` + `RisexFeed` | Single multi-tenant instance |
| State persistence | (R2 bucket) | `tradeautonom-user-state` | One tarball per user; restored on cold start |
| Persistence telemetry | (Analytics Engine) | `tradeautonom-persistence` | last_flush_ts, last_restore_ts, tar_size, status |

CF Container instance lifecycle: cold-start on first request after
deploy/recycle/idle-evict; warm thereafter until idle timeout. There is
no "host" to SSH into — debugging happens via `npx wrangler tail` and
the Cloudflare dashboard.

**Container init / zombie-reaping:** The user-v2 image uses `tini` as
`ENTRYPOINT` (PID 1). Docker `HEALTHCHECK` forks a Python subprocess
every 30 s; without a proper init, PID 1 (`main.py`) never reaps those
forks and zombies accumulate. Never remove
`ENTRYPOINT ["/usr/bin/tini", "--"]` from
`deploy/cf-containers/user-v2/container/Dockerfile`.

### Legacy NAS (deprecated, reference only)

The old V3 stack on Photon OS VM `root@192.168.133.100`
(`tradeautonom-v3:8005`, `ta-user-*:9001-9008`, `ta-orchestrator:8090`,
`oms:8099`) still runs for users with `user.backend = "nas"`. **No new
features go there.** If you must touch a NAS container, see
`docs/DEPLOYMENT.md` Anhang A. Stale IP `192.168.133.253` in older docs
is wrong; canonical legacy IP is `.100`.

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
| Deploy via `cd deploy/cf-containers/user-v2 && npm run deploy` (V2/CF) | Use `./deploy/v3/manage.sh ...`, root `./deploy.sh`, or any NAS-SSH workflow |
| Bump `V2_BUILD_TAG` in user-v2 Dockerfile on every Python change | Trust a successful `wrangler deploy` to ship new code without a tag bump |
| Force a CF Container cold-start via `/admin/recycle/<user_id>` to pick up new code | Assume running container instances auto-update after deploy |
| Treat `docs/API_KEY_LIFECYCLE.md` as vault source of truth (multi-user) | Assume `docs/DEVELOPER_GUIDE.md` `secrets.enc` model (legacy) |
| Treat `docs/delta-neutral-algorithm.docx.md`, `docs/tradeautonom-integrationsplan-v2.docx.md`, `docs/FIX_USER_CONTAINERS.md`, `docs/V4_WEB_DEVELOPER_GUIDE.md` as design/historical | Treat those four as current implementation references |
