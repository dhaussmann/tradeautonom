# TradeAutonom — Deployment Guide

> **Wichtig (Mai 2026):** Der einzige unterstützte Deploy-Pfad ist **V2 auf
> Cloudflare Containers**. Das frühere V3-NAS-Setup (`deploy/v3/manage.sh`,
> `ta-user-*` Container auf `192.168.133.100`) ist **deprecated** und wird
> nicht mehr für neue Releases verwendet. Historische Befehle dazu sind am
> Ende dieses Dokuments im Abschnitt „Anhang A — Legacy V3 (NAS)" archiviert.

## Systemübersicht (V2 — Cloudflare-native)

```
┌────────────────────────────────────────────────────────────────────┐
│  Cloudflare (alle Komponenten edge-nativ)                          │
│  Account: e52977b75e8923af6487772b5e91c2b8                         │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │  bot.defitool.de       (Frontend + Auth + API-Gateway)      │  │
│  │  Worker: tradeautonom  (deploy/cloudflare/)                 │  │
│  │  D1 + KV + R2 (User-Vault, Sessions, Settings)              │  │
│  └────────────┬────────────────────────────────────────────────┘  │
│               │ Service-Binding USER_V2 (X-Internal-Token)         │
│               ▼                                                    │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │  user-v2.defitool.de   (Per-User Trading-Engine Container)  │  │
│  │  Worker + DO: UserContainer (deploy/cf-containers/user-v2/) │  │
│  │  Routing: /u/<user_id>/...                                  │  │
│  │  Image: user-v2-usercontainer (Python 3.11 + app/)          │  │
│  │  R2: tradeautonom-user-state (per-User tarball mit          │  │
│  │       auth.json, secrets.enc, bots/, dna_bot/, ...)         │  │
│  └─────────────────────────────────────────────────────────────┘  │
│                                                                    │
│  ┌─────────────────────────────────────────────────────────────┐  │
│  │  oms-v2.defitool.de    (Shared Orderbook Monitor + Arb-Scan)│  │
│  │  Worker + DO: AggregatorDO, ArbScannerDO, NadoOmsDO,        │  │
│  │              NadoRelayContainer  (deploy/cf-containers/     │  │
│  │              oms-v2/)                                       │  │
│  └─────────────────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────┘
```

Per-User-Trading läuft als **Cloudflare-Container hinter einem Durable
Object** (`UserContainer`). Jeder User bekommt seine eigene Container-Instanz
beim ersten Login; State (Vault, Bot-Configs, Positions) wird zwischen Cold
Starts via R2-Tarball persistiert.

---

## Voraussetzungen

**Lokale Maschine:**
- Node.js + npm (für Frontend-Build und Wrangler)
- Docker (Wrangler baut die Container-Images lokal und pusht zur CF-Registry)
- Cloudflare Login: `npx wrangler login` (einmalig)

**Repo-Root `.env`:** Wird hauptsächlich für lokale Entwicklung gelesen
(`python main.py`); für Deploys nicht erforderlich.

---

## Typischer Release-Workflow

```bash
# 1. UserContainer neu deployen (Python-Trading-Engine)
cd deploy/cf-containers/user-v2
# WICHTIG: bei Code-Änderungen V2_BUILD_TAG im Dockerfile bumpen,
# sonst greift der Docker-Build-Cache und das Image bleibt alt
npm run deploy

# 2. OMS-v2 (nur wenn OMS/Arb-Code geändert wurde)
cd ../oms-v2
npm run deploy

# 3. Frontend + Auth-Worker
cd ../../cloudflare
./deploy.sh
```

Pre-Flight: `npm run typecheck` läuft vor jedem Deploy automatisch
(`tsc --noEmit`).

---

## 1. UserContainer V2 (`deploy/cf-containers/user-v2/`)

Per-User Container mit der vollen FastAPI-Trading-Engine (`app/server.py`).
Code wird **ins Image gebacken** (kein Hot-Reload).

### Deploy

```bash
cd deploy/cf-containers/user-v2
npm run deploy
```

**Was passiert:**
1. `wrangler deploy` baut das Docker-Image lokal anhand
   `container/Dockerfile` mit `image_build_context = "../../.."` (Repo-Root,
   damit `COPY app/`, `main.py`, `requirements.txt` funktionieren)
2. Image wird zur Cloudflare-Container-Registry gepusht
3. Worker (`src/index.ts`) und DO-Klasse `UserContainer` werden aktualisiert
4. Neue Container-Instanzen verwenden ab sofort das neue Image

### Cache-Bust für Code-Änderungen — KRITISCH

Im Dockerfile gibt es eine Zeile:

```dockerfile
ENV V2_BUILD_TAG=dna-cooldown-after-close-v54
```

Diese **muss bei jeder Python-Code-Änderung gebumpt werden**, sonst
markiert BuildKit die `COPY app/`-Layer als CACHED und das Image enthält
weiterhin den alten Code — obwohl der Push erfolgreich aussieht.

Konvention: `<feature-name>-vNN`. Vor dem Deploy bumpen, nach dem Deploy
committen.

### Wann der neue Code in Container-Instanzen aktiv wird

Cloudflare ersetzt laufende Instanzen **nicht atomar**. Der neue Code wird
aktiv, wenn die User-Container-Instanz das nächste Mal **kalt startet**:

- Automatisch nach Idle-Timeout (CF Container-Scaling)
- Nach Recycle: `POST https://user-v2.defitool.de/admin/recycle/<user_id>`
  mit Header `X-Internal-Token: <V2_SHARED_TOKEN>`
- Beim nächsten Cold-Start nach User-Login

R2-Persistenz (`app/cloud_persistence.py` + Worker `/__state/restore`)
restored automatisch `data/dna_bot/`, `data/bots/`, `data/auth.json` etc.,
sodass Cold Starts keine User-Daten verlieren.

### Worker-Endpoints (Auswahl)

| Endpoint | Zweck |
|---|---|
| `/u/<user_id>/<path>` | Forward an die Container-Instanz (X-Internal-Token erforderlich) |
| `/__state/restore?user_id=<id>` | Container holt sein R2-Tarball beim Cold-Start |
| `/__state/flush?user_id=<id>` | Container schreibt aktuelles `data/` als Tarball nach R2 (alle 30s) |
| `/admin/recycle/<user_id>` | Stoppt den DO, erzwingt Cold-Start mit neuen EnvVars |

### Konfiguration (`wrangler.jsonc`)

| Parameter | Wert |
|---|---|
| Worker Name | `user-v2` |
| Custom Domain | `user-v2.defitool.de` |
| Container Class | `UserContainer` |
| Instance Type | `standard-1` (1 vCPU / 4 GB RAM) |
| Max Instances | 25 |
| R2 Bucket | `tradeautonom-user-state` (binding `STATE_BUCKET`) |
| Analytics Engine | `tradeautonom-persistence` (binding `PERSIST_LOG`) |
| Compatibility Date | `2026-04-24` (mit `nodejs_compat`) |

### Secrets

```bash
cd deploy/cf-containers/user-v2
npx wrangler secret put V2_SHARED_TOKEN   # Auth-Token zwischen tradeautonom ↔ user-v2
```

---

## 2. OMS V2 (`deploy/cf-containers/oms-v2/`)

Shared Orderbook Monitor und Arbitrage-Scanner. Wird vom DNA-Bot in jeder
User-Container-Instanz via WebSocket konsumiert (`oms-v2.defitool.de/ws/arb`).

### Deploy

```bash
cd deploy/cf-containers/oms-v2
npm run deploy
```

**Architektur-Bestandteile:**
- `AggregatorDO` — sammelt Orderbooks aller Exchanges, broadcastet diff-encoded
- `ArbScannerDO` — scannt alle 200 ms nach Cross-Exchange-Arb-Opportunities
- `NadoOmsDO` — Nado-Account-Stream und Builder-Code-Tracking
- `NadoRelayContainer` (Node.js mit `ws`-Library) — Nado-WS-Relay,
  weil CF Workers `permessage-deflate` nicht aushandeln können
- `RisexFeed` — RISEx-WebSocket-Aggregation

### Konfiguration (`wrangler.jsonc`)

| Parameter | Wert |
|---|---|
| Worker Name | `oms-v2` |
| Custom Domain | `oms-v2.defitool.de` |

---

## 3. Frontend + Auth-Worker (`deploy/cloudflare/`)

### Vollständiges Deploy (Standard)

```bash
./deploy/cloudflare/deploy.sh
```

**Was passiert:**
1. Vue-App wird gebaut: `cd frontend && npm ci && npm run build` →
   `frontend/dist/` (vue-tsc läuft vor Vite — Type-Errors brechen den Build ab)
2. Worker-Dependencies installiert (`deploy/cloudflare/node_modules/`)
3. `wrangler deploy` lädt Assets hoch, entfernt veraltete, deployed Worker

### Optionen

```bash
./deploy/cloudflare/deploy.sh --worker   # Nur Worker (ohne Frontend-Build)
./deploy/cloudflare/deploy.sh --build    # Nur Frontend bauen (ohne Deploy)
```

### Konfiguration (`deploy/cloudflare/wrangler.jsonc`)

| Parameter | Wert |
|---|---|
| `account_id` | `e52977b75e8923af6487772b5e91c2b8` |
| Worker Name | `tradeautonom` |
| Route | `bot.defitool.de/*` |
| D1 Datenbank | `tradeautonom-history` (`1ae6c186-e7d3-4baf-bc05-f9ba999eca93`) |
| KV-Namespaces | Sessions, Settings, Vault |
| Service Binding | `USER_V2` → `user-v2` Worker |
| Cron | Täglich 03:00 UTC (Cleanup) |

### Secrets

```bash
cd deploy/cloudflare
npx wrangler secret put BETTER_AUTH_SECRET  # Session-Signatur-Secret
npx wrangler secret put V2_SHARED_TOKEN     # Forward an user-v2 (Service Binding)
npx wrangler secret put INGEST_TOKEN        # Execution Log Ingest Token
```

### D1-Migrationen

```bash
cd deploy/cloudflare
npx wrangler d1 migrations apply tradeautonom-history --remote
```

---

## 4. Routing & Backend-Auswahl pro User

Ein User-Datensatz in D1 hat ein Feld `user.backend`:

| Wert | Backend |
|---|---|
| `v2` (Standard für Neuanlage) | `user-v2.defitool.de` Container |
| `nas` *(deprecated)* | NAS-`ta-user-*` Container — nicht mehr für neue User verwendet |

Der `tradeautonom`-Worker entscheidet bei jedem API-Request anhand dieses
Felds, wohin proxiert wird. Bestehende NAS-User können via Migration auf
V2 umgestellt werden (separater Schritt, ggf. mit State-Transfer aus
`/opt/tradeautonom-v3/data` nach R2).

---

## 5. Dependency-Update (`requirements.txt`)

Bei Python-Package-Änderungen das `V2_BUILD_TAG` im
`deploy/cf-containers/user-v2/container/Dockerfile` bumpen und neu deployen:

```bash
cd deploy/cf-containers/user-v2
# Dockerfile: V2_BUILD_TAG=… bumpen
npm run deploy
```

`COPY requirements.txt` liegt VOR der `COPY app/` Zeile, damit pip-Installs
nur bei tatsächlichen `requirements.txt`-Änderungen laufen.

---

## Troubleshooting

### Code-Änderungen nach Deploy nicht sichtbar

Häufigste Ursache: `V2_BUILD_TAG` im Dockerfile nicht gebumpt → Docker hat
die `COPY app/`-Layer aus dem Cache genommen. Symptome: Deploy meldet
"SUCCESS", neuer Image-Hash steht in der wrangler-Ausgabe, aber laufende
Container verhalten sich wie vor dem Deploy.

**Fix:** Tag bumpen und erneut deployen. Bei sehr alten Caches:
`docker builder prune` lokal.

### Container-Instanz verwendet alten Code

CF ersetzt Container-Instanzen nicht atomar. Recycle erzwingen:

```bash
curl -X POST https://user-v2.defitool.de/admin/recycle/<user_id> \
  -H "X-Internal-Token: <V2_SHARED_TOKEN>"
```

Beim nächsten Request des Users startet der Container kalt mit dem neuen
Image. R2-Restore lädt automatisch den State.

### `wrangler deploy` schlägt mit Auth-Fehler fehl

```bash
npx wrangler login
# oder
export CLOUDFLARE_API_TOKEN=<token>
```

Account muss `e52977b75e8923af6487772b5e91c2b8` (CloudflareOne - Demo
Account) sein — in allen `wrangler.jsonc` hartkodiert.

### Frontend-Build schlägt mit TypeScript-Fehler fehl

`vue-tsc` läuft vor `vite build`. Typen in `frontend/src/types/bot.ts`,
`gold-spread.ts` etc. müssen zu Backend-Feldern passen. Fehlende Felder
ergänzen, neu builden.

### R2-Restore liefert 404 für einen User

Erste Container-Instanz nach Onboarding hat noch keinen Tarball. Das ist
normal — `cloud_persistence.py` startet mit leerem `data/` und schreibt
beim nächsten Flush (alle 30s) den ersten Tarball.

### Telemetrie zur Persistenz

Analytics Engine `tradeautonom-persistence` enthält `last_flush_ts`,
`last_restore_ts`, `tar_size`, `status` pro User. Abrufbar über die
Cloudflare-API oder das Admin-Dashboard.

---

## Anhang A — Legacy V3 (NAS) — DEPRECATED

> **Nur noch zur Referenz.** Diese Befehle erzeugen weiterhin laufende
> NAS-Container, sollten aber für **keine neuen Releases** verwendet werden.
> Hauptgrund für Deprecation: V2 (CF) skaliert per User automatisch, hat
> R2-Persistenz, kein VPN-Tunnel zum NAS, und ist regional verteilt.
>
> Bestehende NAS-User können bis zu ihrer Migration weiter über das
> `user.backend = "nas"` Feld bedient werden.

### Historische NAS-Topologie

| Container | Port | Notiz |
|---|---|---|
| `ta-orchestrator` | 8090 | User-Container-Manager (`X-Orch-Token`) |
| `ta-user-*` | 9001-9008 | Per-User Trading-Engine, shared `/opt/tradeautonom-v3/app` Mount |
| `tradeautonom-v3` | 8005 | Staging mit gleichem shared Mount |
| `oms` | 8099 | Shared Orderbook Monitor (V1) |

Host: `root@192.168.133.100` (Photon OS VM, **nicht** Synology trotz älterer
Doc-Spuren). SSH-Key: `~/.ssh/id_ed25519`.

### Historische Befehle (nur falls NAS-Stack noch betreut werden muss)

```bash
# Code-Update (rsync + Image-Build)
./deploy/v3/manage.sh update

# Hot-Reload aller User-Container (nutzt shared /app-Mount)
./deploy/v3/manage.sh deploy-code

# Lifecycle einzelner Container
./deploy/v3/manage.sh list
./deploy/v3/manage.sh create <user_id>
./deploy/v3/manage.sh start  <user_id>
./deploy/v3/manage.sh stop   <user_id>
./deploy/v3/manage.sh logs   <user_id>

# Orchestrator
ORCH_TOKEN=<token> ./deploy/orchestrator/deploy.sh --restart
```

`./deploy.sh` im Repo-Root ist **doppelt deprecated** (verweist auf alte
IP `192.168.133.253`) — nicht verwenden.

### Migration NAS → V2

1. State des Users aus `/opt/tradeautonom-v3/data/<user_dir>/` als Tarball
   nach R2 hochladen (Schlüssel: `<user_id>.tar.gz`)
2. D1-Eintrag setzen: `UPDATE user SET backend='v2' WHERE id=...`
3. Bei nächstem Login lädt `cloud_persistence.py` den Tarball automatisch
4. NAS-Container kann gestoppt und gelöscht werden

(Detaillierter Migrations-Runbook folgt separat, sobald produktive
Migrationen anstehen.)
