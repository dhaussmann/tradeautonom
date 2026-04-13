# TradeAutonom — Infrastruktur-Dokumentation

## Übersicht

TradeAutonom ist eine Multi-Tenant-Plattform für automatisierte Funding-Arbitrage auf dezentralen Perpetual-Börsen. Die Architektur besteht aus drei Schichten:

```
┌─────────────────────────────────────────────────┐
│              Cloudflare Worker (Edge)            │
│  • Vue SPA (KV)  • Auth (better-auth)           │
│  • D1 Database   • API-Routing                  │
└────────────────────┬────────────────────────────┘
                     │ VPC Tunnel
┌────────────────────▼────────────────────────────┐
│            Orchestrator (NAS, Port 8090)         │
│  • Container-Lifecycle  • Proxy  • Watchdog      │
└────────────────────┬────────────────────────────┘
                     │ Docker API
┌────────────────────▼────────────────────────────┐
│        Docker Container (pro User)               │
│  • Trading Engine  • Exchange Clients            │
│  • WebSocket Feeds • State Persistence           │
└─────────────────────────────────────────────────┘
```

---

## 1. Cloudflare Worker

Der Worker läuft auf Cloudflares Edge-Netzwerk und ist der einzige öffentlich erreichbare Endpunkt (`bot.defitool.de`).

### Aufgaben

- **Statische Assets**: Das Vue-Frontend wird aus Workers KV ausgeliefert (SPA mit Fallback auf `index.html`).
- **Authentifizierung**: [better-auth](https://better-auth.com/) mit D1 als Datenbank. Unterstützt Email/Passwort-Registrierung und -Login. Sessions laufen 30 Tage, werden alle 24h refreshed.
- **API-Routing**: Alle `/api/*`-Anfragen werden authentifiziert und an den richtigen Backend-Container geroutet.
- **History & Journal**: Daten wie Trades, Equity-Snapshots, Fills und Funding-Payments werden direkt in D1 gespeichert und gelesen — kein Container nötig.
- **Secrets-Management**: API-Keys der Börsen werden verschlüsselt in D1 gespeichert (siehe Abschnitt 3).

### Datenbank (D1)

Cloudflare D1 ist eine serverlose SQLite-Datenbank. Tabellen:

| Tabelle | Zweck |
|---------|-------|
| `user` | Benutzerkonten (ID, Name, Email) |
| `session` | Login-Sessions |
| `account` | Auth-Provider-Daten |
| `user_container` | Zuordnung User → Docker-Container (Port, Name, Status) |
| `user_secrets` | Verschlüsselte API-Keys pro User |
| `equity_snapshots` | Equity-Zeitreihen |
| `position_snapshots` | Positions-Zeitreihen |
| `trades` | Erkannte geschlossene Trades |
| `journal_*` | Orders, Fills, Funding, Points, Positions |

---

## 2. Container-Architektur

### Warum Docker-Container?

Jeder User erhält einen eigenen Docker-Container aus folgenden Gründen:

1. **Isolation**: API-Keys, Positionen und Trading-State eines Users sind vollständig von anderen isoliert. Ein Bug oder Absturz betrifft nur den einen User.
2. **Sicherheit**: API-Keys werden nur im Container des jeweiligen Users entschlüsselt und gehalten — nie im shared Worker.
3. **Ressourcen-Kontrolle**: Jeder Container hat ein festes Memory-Limit (512 MB) und CPU-Quota (0.5 Cores). Ein User kann nicht die Ressourcen anderer beeinträchtigen.
4. **State-Persistence**: Position-State, Timer und Bot-Konfiguration werden im Container-Volume (`/app/data`) persistiert und überleben Container-Neustarts.
5. **Unabhängige Upgrades**: Container können einzeln aktualisiert werden, ohne andere User zu beeinflussen.

### Container-Inhalt

Jeder Container läuft als FastAPI-Server und enthält:

- **Trading Engine** (`engine.py`): Orchestriert Entry, Holding und Exit von Positionen.
- **State Machine** (`state_machine.py`): Maker-Taker TWAP-Ausführung.
- **Exchange Clients**: API-Clients für Extended, GRVT und Variational.
- **DataLayer** (`data_layer.py`): Echtzeit-WebSocket-Orderbook-Feeds.
- **Funding Monitor** (`funding_monitor.py`): Überwacht Funding-Rates.
- **Risk Manager** (`risk_manager.py`): Laufende Risikoüberwachung.
- **Journal Collector**: Sammelt Fills, Funding und Points und sendet sie an D1.

### Orchestrator

Der Orchestrator ist ein FastAPI-Service auf dem NAS (Port 8090), der den Docker-Daemon steuert:

- **Auto-Provisioning**: Beim ersten Login eines Users erstellt der CF Worker automatisch einen Container über den Orchestrator.
- **Proxy**: Routet API-Requests vom Worker an den richtigen Container basierend auf dem User-Port.
- **Watchdog**: Prüft alle 60 Sekunden den Status aller Container. Gestoppte Container werden automatisch neu gestartet (max. 3 Restarts in 5 Minuten, danach `crash_loop`-Status).
- **Lifecycle**: Start, Stop, Delete, Logs und Stats pro Container.

### Container-Lifecycle

```
User registriert sich
        ↓
Erster API-Aufruf → CF Worker erkennt: kein Container
        ↓
Worker ruft POST /orch/containers auf
        ↓
Orchestrator erstellt Docker-Container:
  • Image: tradeautonom:v3
  • Port: automatisch (ab 9001)
  • Volume: ta-data-{user_id}
  • Env: USER_ID, APP_PORT
        ↓
Worker wartet auf Health-Check (max 20s)
        ↓
Container ready → D1-Eintrag in user_container
        ↓
API-Keys aus D1 werden automatisch injiziert
```

---

## 3. Secrets / API-Key-Management

### Verschlüsselung

API-Keys werden **niemals im Klartext** gespeichert. Der Ablauf:

1. User gibt API-Keys im Frontend ein (Settings-Seite).
2. Keys werden per HTTPS an den CF Worker gesendet.
3. Worker verschlüsselt die Keys mit **AES-256-GCM**:
   - Schlüssel: Abgeleitet via **PBKDF2** (100.000 Iterationen, SHA-256) aus dem `ENCRYPTION_KEY` (Cloudflare Secret).
   - Jeder Verschlüsselungsvorgang erzeugt einen zufälligen Salt (16 Bytes) und IV (12 Bytes).
   - Format in D1: `base64(salt[16] | iv[12] | ciphertext + GCM-tag)`
4. Verschlüsselter Blob wird in `user_secrets` gespeichert.

### Verwaltete Keys

| Key | Börse | Zweck |
|-----|-------|-------|
| `extended_api_key` | Extended | API-Authentifizierung |
| `extended_public_key` | Extended | Signierung |
| `extended_private_key` | Extended | Signierung |
| `extended_vault` | Extended | Vault-ID |
| `grvt_api_key` | GRVT | API-Authentifizierung |
| `grvt_private_key` | GRVT | Signierung |
| `grvt_trading_account_id` | GRVT | Account-ID |
| `variational_jwt_token` | Variational | JWT-Token |

### Auto-Injection

Beim Container-Start oder Login werden die Keys automatisch in den Container injiziert:

1. CF Worker liest verschlüsselten Blob aus D1.
2. Entschlüsselung im Worker (PBKDF2 + AES-256-GCM).
3. Keys werden per `POST /internal/apply-keys` an den Container gesendet (über VPC-Tunnel, nicht öffentlich).
4. Container initialisiert Exchange-Clients mit den Keys.

### Maskierung

Im Frontend werden Keys immer maskiert angezeigt (`***abcd`). Nur die letzten 4 Zeichen sind sichtbar. Updates werden mit den bestehenden Keys gemergt — unveränderte Felder bleiben erhalten.

---

## 4. Netzwerk & Sicherheit

### VPC-Tunnel

Die Verbindung zwischen Cloudflare Worker und NAS läuft über einen **Cloudflare VPC Service** (Workers VPC). Das NAS ist nicht direkt aus dem Internet erreichbar.

### Authentifizierungs-Schichten

| Schicht | Mechanismus |
|---------|-------------|
| User → Worker | better-auth Session-Cookie (HTTP-only) |
| Worker → Orchestrator | `X-Orch-Token` Header (shared secret) |
| Worker → Container | Via Orchestrator-Proxy (kein direkter Zugriff) |
| History/Journal Ingest | `INGEST_TOKEN` Header |
| Admin-Endpunkte | Session + Email-Whitelist (`ADMIN_EMAILS`) |

### Secrets (Cloudflare)

Folgende Secrets werden über `wrangler secret put` konfiguriert:

- `BETTER_AUTH_SECRET` — Session-Signierung
- `ENCRYPTION_KEY` — AES-Schlüssel für API-Keys
- `INGEST_TOKEN` — Auth für History/Journal-Ingest
- `ORCH_TOKEN` — Auth für Orchestrator-Kommunikation
- `ADMIN_EMAILS` — Komma-getrennte Admin-Email-Liste

---

## 5. Deployment

### Build & Deploy

```bash
cd deploy/cloudflare
bash deploy.sh
```

Das Script:
1. Baut das Vue-Frontend (`npx vite build`)
2. Installiert Worker-Dependencies (`npm ci`)
3. Deployt Worker + Assets via `wrangler deploy`

### Container-Image aktualisieren

```bash
# Auf dem NAS:
docker build -t tradeautonom:v3 -f Dockerfile.nas .

# Alle Container neu starten:
# Orchestrator-Watchdog erkennt gestoppte Container und startet sie mit dem neuen Image
```

### D1-Migrationen

```bash
cd deploy/cloudflare
npx wrangler d1 migrations apply tradeautonom-history --remote
```
