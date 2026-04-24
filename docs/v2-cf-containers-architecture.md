# V2 — Cloudflare-native architecture

Status: **OMS-v2 live (Phases A–E complete), UserContainer-v2 live on
`user-v2.defitool.de` (Phases F.0–F.2 complete: feasibility proven, baked
image, Python engine running against OMS-v2, R2 persistence code layered
in pending manual R2 API token setup)**. V1 (Photon-VM Docker stack)
continues to run in parallel; no V1 user is routed to V2 yet.

See `docs/v2-oms-cloudflare-native.md` for OMS-v2 details.
The UserContainer-v2 implementation lives at
`deploy/cf-containers/user-v2/`.

## Why V2

V1 runs on a single Photon OS VM (`root@192.168.133.100`) with:

- Docker containers for OMS, orchestrator, user containers, and `tradeautonom-v3`.
- Manual memory tuning (1 GiB cgroup limits), tini for zombie reaping, persistent local volumes for bot state.
- Cloudflare Worker (`bot.defitool.de`) fronts the stack via Workers VPC binding.

Pain points that V2 addresses:

1. Single host is a single point of failure.
2. Cold-start recovery (container restart, vault re-injection, position resync) takes 30+ seconds.
3. Scaling to more users means provisioning more memory/CPU on the same VM.
4. All updates require SSH + `docker build`; no atomic rollback.

V2 moves to Cloudflare-native primitives where possible, with a user-level routing flag so V1 and V2 coexist until V1 can be retired.

## Architecture target

```
             Cloudflare                          Photon VM (192.168.133.100)
 ┌────────────────────────────────────────┐   ┌────────────────────────────────┐
 │ Worker (bot.defitool.de)               │   │                                │
 │  ↓ reads user.backend from D1          │   │                                │
 │  ├─ "photon"  → VPC → orchestrator ────┼──→│ ta-orchestrator → ta-user-*    │
 │  │                                     │   │ tradeautonom-v3, oms (V1)      │
 │  └─ "cf"      → DO / Container         │   │                                │
 │                                        │   │                                │
 │  ┌──────────────────────────────┐      │   │                                │
 │  │ OMS-v2: Pure Durable Objects │      │   │                                │
 │  │  (TypeScript, no container)  │      │   │                                │
 │  │  • ExtendedOms DO            │      │   │                                │
 │  │  • NadoOms DO                │      │   │                                │
 │  │  • GrvtOms DO                │      │   │                                │
 │  │  • VariationalOms DO → proxy │      │   │                                │
 │  │  • AggregatorDO (bot subs)   │      │   │                                │
 │  └──────────────────────────────┘      │   │                                │
 │                                        │   │                                │
 │  ┌──────────────────────────────┐      │   │                                │
 │  │ UserContainer-v2 (per user)  │      │   │                                │
 │  │  Python, same image as V1    │      │   │                                │
 │  │  State in Durable Object SQL │      │   │                                │
 │  │  ephemeral disk              │      │   │                                │
 │  └──────────────────────────────┘      │   │                                │
 └────────────────────────────────────────┘   └────────────────────────────────┘
```

## Routing — D1-flag per user

Table `user` in the existing `tradeautonom-history` D1 database gets a new column:

```sql
ALTER TABLE user ADD COLUMN backend TEXT NOT NULL DEFAULT 'photon';
-- allowed values: 'photon' | 'cf-containers'
```

Worker logic in `deploy/cloudflare/src/index.ts`:

```ts
const user = await getUserFromSession(session);
if (user.backend === 'cf-containers') {
  const userContainer = env.USER_CONTAINER.get(
    env.USER_CONTAINER.idFromName(user.id),
  );
  return userContainer.fetch(request);
}
return proxyToOrchestrator(request);  // existing V1 path
```

- Default for existing users: `photon`.
- New users created via admin UI default to `cf-containers` once V2 is stable.
- Admin toggle per user in `frontend/src/views/AdminView.vue`.

Migration is one-way per user, not transparent: switching a user's `backend` requires exporting their bots' state (configs, positions, timers) from V1 and writing it into the V2 Durable Object SQLite.

## Phased rollout

### Phase 0 — Docs + PoC

- `docs/v2-cf-containers-architecture.md` (this file)
- `docs/v2-oms-cloudflare-native.md` — detailed OMS-v2 rationale + DO pattern
- `deploy/cf-containers/proof-of-concept/` — minimal `ExtendedOms` DO that opens a live Extended WebSocket and serves a `getBook()` RPC

No impact on V1.

### Phase 1 — Storage abstraction

V1 and V2 share the core Python trading engine. To make `bot_registry.py` + `engine.py` + `state_machine.py` work against both local filesystem (V1) and Durable Object SQLite (V2), introduce a `BotStateStore` protocol:

```python
# app/storage/__init__.py
class BotStateStore(Protocol):
    async def save_config(self, bot_id: str, config: dict) -> None: ...
    async def load_config(self, bot_id: str) -> dict | None: ...
    async def save_position(self, bot_id: str, position: dict) -> None: ...
    async def load_position(self, bot_id: str) -> dict | None: ...
    async def save_timer(self, bot_id: str, timer: dict) -> None: ...
    async def load_timer(self, bot_id: str) -> dict | None: ...
    async def list_bots(self) -> list[str]: ...
    async def delete_bot(self, bot_id: str) -> None: ...
```

- `DiskBotStateStore` (default, V1): reads/writes `/app/data/bots/<id>/{config,position,timer}.json`
- `DurableObjectBotStateStore` (V2): HTTP/RPC to the UserContainer's own DO SQLite — the container hosts both the Python trading engine AND a small local state API bridged to DO storage through its Worker wrapper

Config flag `V2_BACKEND=photon|cf-containers` in `app/config.py` picks the implementation at startup.

Full regression test required — this touches the hottest path in the engine.

### Phase 2 — Cloudflare Worker backend routing

- D1 migration: `0002_add_backend_column.sql`
- `deploy/cloudflare/src/lib/backend.ts`: routing helpers
- `deploy/cloudflare/src/containers.ts`: `UserContainer` + (future) OMS aggregator DO class
- Admin endpoint + UI toggle

Does not affect V1 traffic (default flag = `photon`, everyone continues on existing path).

### Phase 3 — OMS-v2 as Pure Durable Objects

**This replaces the original "OMS as CF Container" plan.**

OMS-v2 is rewritten in TypeScript as a set of Durable Objects, one per exchange. See `docs/v2-oms-cloudflare-native.md` for the full rationale + DO pattern.

Structure:

```
deploy/cf-containers/oms-v2/
├── wrangler.jsonc                # no containers, only durable_objects
├── src/
│   ├── index.ts                  # OMS Worker entrypoint
│   ├── aggregator.ts             # AggregatorDO: bot-subscriber routing (Hibernation)
│   ├── exchanges/
│   │   ├── base.ts               # abstract ExchangeOms DO
│   │   ├── extended.ts           # ExtendedOms DO
│   │   ├── nado.ts               # NadoOms DO (EIP-712 + x18 + inversion)
│   │   ├── grvt.ts               # GrvtOms DO
│   │   └── variational.ts        # VariationalOms DO → proxy.defitool.de
│   └── lib/
│       ├── orderbook.ts
│       ├── symbols.ts
│       ├── nado-math.ts
│       └── backoff.ts
└── package.json
```

Bot-client protocol (`/ws`, `/book/<exch>/<sym>`, `/status`, `/tracked`) kept **identical** to today's Python OMS so that the existing `app/data_layer.py::_run_oms_ws` client works unchanged against V2-OMS.

V1 users keep hitting `http://192.168.133.100:8099` (Photon OMS).
V2 users hit `https://oms-v2.defitool.de` (or similar subdomain).

Photon-OMS continues to run as long as any V1 user exists.

### Phase F — UserContainer-v2 as Cloudflare Containers

Implemented under `deploy/cf-containers/user-v2/`. The container runs the
**exact same V1 Python engine** (`app/*`, `main.py`, `requirements.txt`)
with no code forks — V1 and V2 share one trading engine. The only
differences from V1:

1. Code is **baked into the image** instead of mounted read-only from
   `/opt/tradeautonom-v3/app`. `wrangler deploy` rebuilds and rolls out.
   `APP_RELOAD=0` disables uvicorn's hot-reload.
2. `/app/data` is **ephemeral**. `app/cloud_persistence.py` syncs it
   to/from R2 (`tradeautonom-user-state/<user_id>.tar.gz`) on cold start
   and every 30 s when files change, plus a final flush on SIGTERM.
3. `FN_OPT_SHARED_MONITOR_URL=https://oms-v2.defitool.de` — bots
   subscribe to OMS-v2 instead of V1 Photon OMS.
4. One `Container` DO instance per user, addressed via
   `getContainer(env.USER_CONTAINER, idFromName(user_id))`.

Key files:
- `deploy/cf-containers/user-v2/container/Dockerfile` — Python 3.11 +
  tini + pinned V1 deps + baked `app/`, `static/`, `main.py`. Build
  context is the repo root (`image_build_context: "../../.."`) so COPY
  picks up the canonical paths, not duplicates.
- `deploy/cf-containers/user-v2/container/entrypoint.sh` — restores
  `/app/data` from R2 before `exec python main.py`. No-op when
  `V2_CLOUD_PERSISTENCE=0` (V1).
- `deploy/cf-containers/user-v2/src/user-container.ts` — `Container` DO
  class, `defaultPort=8000`, `sleepAfter="30m"`, `instance_type="standard-1"`,
  `max_instances=25`, baseline env vars.
- `deploy/cf-containers/user-v2/src/index.ts` — smoke-test Worker;
  accepts `/u/<user_id>/...`, forwards to the matching DO. This is a
  temporary route until Phase F.3 integrates the real auth-cookie
  routing into `deploy/cloudflare/src/index.ts`.
- `app/cloud_persistence.py` — R2 restore/flush module; guarded by
  `settings.v2_cloud_persistence`; boto3 talks to R2's S3-compatible
  endpoint.
- `app/server.py::lifespan` — starts the background flush task on app
  startup, awaits a final flush on shutdown.
- `app/config.py` — adds `v2_cloud_persistence`, `v2_flush_interval_s`,
  `user_id`, `r2_bucket`, `r2_endpoint`, `r2_access_key_id`, `r2_secret`,
  `app_reload`.

Status per sub-phase:
- **F.0 (feasibility PoC)** — complete. Confirmed that x10 Starknet,
  `pysdk.grvt_ccxt`, `curl_cffi`, WebSocket to OMS-v2 all work under CF
  Containers' Linux userspace. Found + fixed: `x10-python-trading-starknet`
  reorganised its package in 1.4.0 breaking V1 imports → pinned 1.3.1 in
  `requirements.txt` plus other versions matching the live V1 container.
  PoC deleted after F.1 landed.
- **F.1 (container + Worker wiring)** — complete. Deployed to
  `user-v2.defitool.de`. Verified:
    - Cold start ~4 s from fresh request
    - `/health` → `{"status":"ok","grvt_env":"prod"}`
    - `/auth/setup` + `/auth/unlock` work; vault survives within a single
      instance's lifetime
    - `/fn/status` returns live prices pulled from OMS-v2 (Extended SOL-USD,
      GRVT SOL_USDT_Perp)
    - `/fn/bots` starts the real BotRegistry cleanly
- **F.2 (R2 persistence)** — code complete, not yet activated. Needs an
  R2 API token (dashboard-only step) and the `V2_CLOUD_PERSISTENCE=1`
  env var set. Until then the container still works (same as V1 on first
  boot before any user state existed) but state is lost if CF recycles
  the instance. The module is designed as a strict no-op without creds,
  so shipping the code first is safe.

### Phase F.3 — Main-Worker routing (pending)

- D1 migration `0006_add_backend_column.sql`: `ALTER TABLE user ADD
  COLUMN backend TEXT NOT NULL DEFAULT 'photon';`
- `deploy/cloudflare/src/index.ts::handleUserApiProxy` gets a
  `backend === "cf"` branch that calls
  `env.USER_CONTAINER_V2.fetch(request)` instead of the Photon
  orchestrator path.
- Admin UI toggle + migration script (`scripts/migrate_user_to_cf.py`)
  that stops V1 container, rsyncs `app-data/` to local tmp, tars +
  uploads to R2, flips the D1 flag.
- Pre-flight check: block the flip if `/fn/bots` shows any non-IDLE
  state.

### Phase F.4+ — Canary rollout + decommissioning

- First flip: one test user with a small number of simulated bots.
- Observe 24–72 h.
- Widen the rollout one user at a time as confidence builds.
- When no `backend='photon'` users remain, decommission the Photon VM.

## V1 constraints to preserve

Nothing in V2 should compromise V1 behavior:

- `BotStateStore` abstraction must not change `DiskBotStateStore` semantics (same file paths, same JSON schema).
- Existing Worker routes (`/api/*`, VPC binding) continue to work.
- Photon OMS continues to serve `192.168.133.100:8099`.

## Known risks + open questions

| Topic | Risk | Mitigation |
|---|---|---|
| Python engine under CF Containers runtime | Uvicorn + FastAPI + httpx all untested on CF Containers runtime (rootless, no iptables) | Phase 0 PoC secondary test: run minimal FastAPI container; verify request routing |
| `curl_cffi` for Variational | Required for Cloudflare TLS-bypass; unavailable in Workers/DOs | Variational-v2 DO calls `proxy.defitool.de` (existing production workaround) |
| Outgoing WebSocket in DO prevents hibernation | Exchange-WS DOs never hibernate → billed 24/7 as GB-s | Accepted; DO GB-s is cheap. Cost comparison vs container must be run. |
| Custom headers in outbound WebSocket upgrade | Nado requires post-connect signed subscribe frames (not headers); Extended order book stream is public (no auth). Verified against Extended API docs. | PoC confirms `fetch(url, { headers: { Upgrade: "websocket", "User-Agent": ... } })` as the outbound-WS pattern |
| Nado EIP-712 signing in TypeScript | Python uses `eth_account`; TS equivalent is `ethers.js` | `ethers` dependency in Worker, signing in NadoOms DO |
| DO SQLite 128 MB limit | Bot history could exceed | Only "latest state" in DO, history externalized to D1 / R2 |
| RPC costs between DOs | Each delta triggers AggregatorDO fan-out | Batching + edge-cached subscriber lists |
| Cost per active user on CF Containers | Per-user DO + container running 24/7 vs. shared Photon process | Measured after first real users migrated |

## Related docs

- `docs/v2-oms-cloudflare-native.md` — OMS-v2 technical detail
- `docs/API_KEY_LIFECYCLE.md` — vault / D1 key injection (unchanged under V2)
- `docs/container-init-zombies.md` — V1 tini reaping
- `docs/DEPLOYMENT.md` — V1 deployment runbook
- Cloudflare docs:
  - https://developers.cloudflare.com/containers/
  - https://developers.cloudflare.com/durable-objects/best-practices/websockets/
