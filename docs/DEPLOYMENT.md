# TradeAutonom — Deployment Guide

## Systemübersicht

```
┌──────────────────────────────────────────────────────────────────┐
│  Cloudflare (Edge)                                               │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  Workers + KV  —  bot.defitool.de/*                      │   │
│  │  Account: CloudflareOne - Demo Account                   │   │
│  │  Worker: tradeautonom                                    │   │
│  └─────────────────────┬────────────────────────────────────┘   │
│                         │ Workers VPC                            │
└─────────────────────────┼────────────────────────────────────────┘
                           │
┌─────────────────────────┼────────────────────────────────────────┐
│  Server  192.168.133.100                                         │
│                         │                                        │
│  ┌──────────────────────▼──────────────┐                        │
│  │  ta-orchestrator          :8090     │  User-Container-Manager │
│  └──────────────────────┬──────────────┘                        │
│                          │ Docker Socket                         │
│  ┌───────────────────────▼──────────────────────────────────┐   │
│  │  ta-user-XXXXXXXX  :9001                                 │   │
│  │  ta-user-YYYYYYYY  :9002   (je ein Container pro User)   │   │
│  │  ...               :9003..9008                           │   │
│  │                                                          │   │
│  │  Image:   tradeautonom:v3                                │   │
│  │  Code:    /opt/tradeautonom-v3/app  →  /app/app  (ro)   │   │
│  │  Daten:   ta-data-XXXXXXXX          →  /app/data (rw)   │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  tradeautonom-v3   :8005   (Legacy-Testcontainer)                │
│  oms               :8099   (Shared Orderbook Monitor)            │
│  portainer         :8000   (Docker UI)                           │
└──────────────────────────────────────────────────────────────────┘
```

---

## Voraussetzungen

**Lokale Maschine:**
- SSH-Zugang: Key `~/.ssh/id_ed25519` muss auf `root@192.168.133.100` autorisiert sein
- Node.js + npm (für Cloudflare Frontend-Build)

**`.env` im Projekt-Root** (wird von allen Deploy-Scripts gelesen):
```bash
NAS_HOST=192.168.133.100
NAS_USER=root
NAS_DEPLOY_PATH=/opt/tradeautonom
```

---

## Typischer Release-Workflow

```bash
# 1. Backend-Code deployen (sync + Docker Image bauen)
./deploy/v3/manage.sh update

# 2. User-Container neu starten (lädt neuen Python-Code)
ssh root@192.168.133.100 \
  "for c in \$(docker ps --filter name=ta-user- --format '{{.Names}}'); \
   do docker restart \$c && echo \"Restarted \$c\"; done"

# 3. Frontend deployen (Build + Cloudflare Workers)
./deploy/cloudflare/deploy.sh
```

---

## 1. Backend — User-Container (`deploy/v3/manage.sh`)

### Code-Update

```bash
./deploy/v3/manage.sh update
```

**Was passiert:**
1. Projekt wird per SSH+tar auf `192.168.133.100:/opt/tradeautonom-v3` übertragen
   - Ausgeschlossen: `.venv`, `.git`, `__pycache__`, `*.pyc`, `.env`, `data/`
2. Docker Image `tradeautonom:v3` wird auf dem Server neu gebaut
3. Legacy-Testcontainer `ta-testbot1` wird neu gestartet *(kann Port-Konflikt mit User-Containern erzeugen — harmlos, Code ist trotzdem deployed)*

> **Wichtig:** Die produktiven `ta-user-*` Container werden **nicht automatisch** neu gestartet.  
> Da `app/` als Read-Only-Volume eingebunden ist, muss Python neu gestartet werden um den Code zu laden:

```bash
ssh root@192.168.133.100 \
  "for c in \$(docker ps --filter name=ta-user- --format '{{.Names}}'); \
   do docker restart \$c && echo \"Restarted \$c\"; done"
```

### Weitere Befehle

```bash
./deploy/v3/manage.sh list                  # Alle registrierten User + Ports anzeigen
./deploy/v3/manage.sh create <user_id>      # Neuen User-Container anlegen
./deploy/v3/manage.sh start  <user_id>      # Container starten
./deploy/v3/manage.sh stop   <user_id>      # Container stoppen
./deploy/v3/manage.sh destroy <user_id>     # Container + Daten löschen (irreversibel!)
./deploy/v3/manage.sh logs   <user_id>      # Live-Logs (Ctrl+C zum Beenden)
./deploy/v3/manage.sh status <user_id>      # Container-Status + Health
```

### Aufbau eines User-Containers

| Eigenschaft | Wert |
|---|---|
| Docker Image | `tradeautonom:v3` |
| Code-Volume | `/opt/tradeautonom-v3/app` → `/app/app` (read-only, shared) |
| Daten-Volume | `ta-data-{uid[:8]}` → `/app/data` (persistent, named Docker volume) |
| Port-Range | 9001 – 9008 |
| Env-Vars | Direkt vom Orchestrator übergeben (kein `.env`-File im Container) |
| Restart-Policy | `unless-stopped` |
| DNS | `1.1.1.1`, `1.0.0.1` |

---

## 2. Orchestrator (`deploy/orchestrator/deploy.sh`)

Der Orchestrator ist verantwortlich für den Lifecycle aller User-Container (erstellen, starten, stoppen, Watchdog).

### Erstes Setup / Rebuild

```bash
ORCH_TOKEN=<token> ./deploy/orchestrator/deploy.sh
```

**Was passiert:**
1. `orchestrator.py`, `requirements.txt`, `Dockerfile` → `/opt/tradeautonom-orchestrator/` per SCP
2. Docker Image `tradeautonom-orchestrator:latest` wird auf dem Server gebaut
3. Container `ta-orchestrator` wird gestartet mit:
   - `--network host` (erreicht User-Container auf localhost)
   - `/var/run/docker.sock` gemountet (Docker API Zugriff)
   - State-Datei: `/opt/tradeautonom-orchestrator/data/orchestrator_state.json`

### Weitere Befehle

```bash
ORCH_TOKEN=<token> ./deploy/orchestrator/deploy.sh --restart   # Nur neu starten
ORCH_TOKEN=<token> ./deploy/orchestrator/deploy.sh --logs      # Live-Logs
ORCH_TOKEN=<token> ./deploy/orchestrator/deploy.sh --stop      # Stoppen
ORCH_TOKEN=<token> ./deploy/orchestrator/deploy.sh --status    # Health-Check
```

### Orchestrator API (manuell)

Alle Requests benötigen Header: `X-Orch-Token: <ORCH_TOKEN>`

```bash
BASE=http://192.168.133.100:8090

# Alle Container anzeigen
curl $BASE/orch/containers -H "X-Orch-Token: <token>"

# Container neu erstellen (wenn verloren)
curl -X POST $BASE/orch/containers \
  -H "X-Orch-Token: <token>" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "<full_uid>", "port": <port>}'

# Container starten / stoppen
curl -X POST $BASE/orch/containers/<uid>/start -H "X-Orch-Token: <token>"
curl -X POST $BASE/orch/containers/<uid>/stop  -H "X-Orch-Token: <token>"

# Logs eines Containers
curl $BASE/orch/containers/<uid>/logs -H "X-Orch-Token: <token>"

# Health
curl $BASE/orch/health
```

---

## 3. Frontend — Cloudflare Workers (`deploy/cloudflare/deploy.sh`)

### Vollständiges Deploy (Standard)

```bash
./deploy/cloudflare/deploy.sh
```

**Was passiert:**
1. Vue-App wird gebaut: `cd frontend && npm ci && npm run build` → `frontend/dist/`
2. Worker-Dependencies werden installiert (`deploy/cloudflare/node_modules/`)
3. `wrangler deploy` lädt neue Assets hoch, entfernt veraltete, deployed den Worker

### Optionen

```bash
./deploy/cloudflare/deploy.sh --worker   # Nur Worker deployen (kein Frontend-Build)
./deploy/cloudflare/deploy.sh --build    # Nur Frontend bauen (kein Deploy)
```

### Konfiguration (`deploy/cloudflare/wrangler.jsonc`)

| Parameter | Wert |
|---|---|
| `account_id` | `e52977b75e8923af6487772b5e91c2b8` (CloudflareOne - Demo Account) |
| Worker Name | `tradeautonom` |
| Route | `bot.defitool.de/*` |
| D1 Datenbank | `tradeautonom-history` (`1ae6c186-e7d3-4baf-bc05-f9ba999eca93`) |
| VPC NAS Backend | Service ID `019d5d2b-cd0c-72b0-ae1e-43dee260ab29` |
| VPC OMS Backend | Service ID `019d9287-7110-7172-9dec-2442103640b4` |
| Orchestrator Origin | `http://192.168.133.100:8090` |
| Cron | Täglich 03:00 UTC (Cleanup) |

### Secrets (einmalig setzen, bleiben auf Cloudflare gespeichert)

```bash
cd deploy/cloudflare
npx wrangler secret put ORCH_TOKEN          # Orchestrator Auth-Token
npx wrangler secret put BETTER_AUTH_SECRET  # Session-Signatur-Secret
npx wrangler secret put INGEST_TOKEN        # Execution Log Ingest Token
```

### D1 Datenbank-Migrationen

Neue SQL-Dateien unter `deploy/cloudflare/migrations/` werden folgendermaßen auf Cloudflare angewendet:

```bash
cd deploy/cloudflare
npx wrangler d1 migrations apply tradeautonom-history --remote
```

---

## 4. Dependency-Update (`requirements.txt`)

Wenn sich Python-Pakete ändern, reicht ein Volume-Update nicht — das Docker Image muss neu gebaut und die Container neu erstellt werden:

```bash
# 1. Image neu bauen
./deploy/v3/manage.sh update

# 2. Alle User-Container neu starten (nutzen neues Image erst nach Neustart)
ssh root@192.168.133.100 \
  "for c in \$(docker ps --filter name=ta-user- --format '{{.Names}}'); \
   do docker restart \$c && echo \"Restarted \$c\"; done"
```

---

## Troubleshooting

### `Port already allocated` beim `manage.sh update`

`manage.sh update` versucht am Ende `ta-testbot1` auf Port 9001 zu starten. Da User-Container Port 9001 belegen, schlägt das fehl. **Der Fehler ist harmlos** — Code und Image wurden trotzdem korrekt deployed. User-Container manuell neu starten.

### User-Container fehlen (nach Server-Neustart o.Ä.)

```bash
# 1. State prüfen
ssh root@192.168.133.100 "cat /opt/tradeautonom-orchestrator/data/orchestrator_state.json"

# 2. Fehlende Container über API neu erstellen
curl -X POST http://192.168.133.100:8090/orch/containers \
  -H "X-Orch-Token: <token>" \
  -H "Content-Type: application/json" \
  -d '{"user_id": "<full_uid>", "port": <port>}'
```

### Code-Änderungen nach Deploy nicht sichtbar

Python lädt Module beim Start — kein Hot-Reload. Nach jedem Code-Update Container neu starten:
```bash
ssh root@192.168.133.100 "docker restart ta-user-<name>"
```

### Cloudflare-Deploy fragt nach Account

Falls wrangler nach dem Account fragt: `account_id` in `deploy/cloudflare/wrangler.jsonc` prüfen. Muss `e52977b75e8923af6487772b5e91c2b8` sein.

### Frontend-Build schlägt mit TypeScript-Fehler fehl

TypeScript-Typen in `frontend/src/types/bot.ts` müssen zu neuen Backend-Feldern passen. Fehlende Felder dort ergänzen, dann erneut deployen.
