# API Key Lifecycle

Vollständige Dokumentation des API-Key-Prozesses von der Eingabe bis zur Nutzung im Trading-Container.

## Architektur-Überblick

```
┌─────────────┐    ┌──────────────────────┐    ┌───────────────┐    ┌────────────────┐
│   Browser    │───▶│  Cloudflare Worker   │───▶│  Orchestrator │───▶│  User-Container │
│  (Frontend)  │    │  bot.defitool.de     │    │  Port 8090    │    │  Port 9001+    │
└─────────────┘    └──────────────────────┘    └───────────────┘    └────────────────┘
                          │                                                │
                          ▼                                                │
                   ┌──────────────┐                                        │
                   │  Cloudflare  │        Plaintext Keys (RAM only) ──────┘
                   │  D1 Database │
                   │  (encrypted) │
                   └──────────────┘
```

**Persistente Quelle**: Cloudflare D1 (AES-256-GCM verschlüsselt)
**Laufzeit**: Container RAM (keine Datei auf Disk, kein `secrets.enc`)

## Phase 1: Key-Eingabe (User setzt Keys)

1. User öffnet Dashboard → Settings → API Keys
2. Frontend sendet `POST /api/secrets/keys` mit Key-Daten
3. Cloudflare Worker empfängt Request

### Worker-Verarbeitung (`index.ts → handleUpdateKeys`)

```
POST /api/secrets/keys
├─ Session-Auth via better-auth (D1-Session)
├─ loadSecrets(D1, userId, ENCRYPTION_KEY) → bestehende Keys laden
├─ filterUpdates(body) → nur MANAGED_KEYS, keine maskierten Werte
├─ merged = { ...existing, ...updates }
├─ saveSecrets(D1, userId, merged, ENCRYPTION_KEY) → verschlüsselt in D1 speichern
└─ autoInjectKeys(env, userId, merged) → sofort in Container pushen
```

### Verschlüsselung (`lib/crypto.ts`)

- **Algorithmus**: AES-256-GCM via Web Crypto API
- **Key-Ableitung**: PBKDF2-SHA256, 100.000 Iterationen
- **Secret**: `ENCRYPTION_KEY` Cloudflare-Secret (Wrangler)
- **Format**: `base64( salt[16] | iv[12] | ciphertext+tag )`
- **D1-Tabelle**: `user_secrets (user_id, encrypted TEXT, updated_at)`

### Verwaltete Keys (`lib/secrets.ts → MANAGED_KEYS`)

```
extended_api_key, extended_public_key, extended_private_key, extended_vault
grvt_api_key, grvt_private_key, grvt_trading_account_id
variational_jwt_token
nado_private_key, nado_linked_signer_key, nado_wallet_address, nado_subaccount_name
```

## Phase 2: Auto-Inject in Container

### Trigger 1: Direkt nach Key-Update

Wenn der User Keys speichert, pusht der Worker sie sofort:

```
handleUpdateKeys
└─ autoInjectKeys(env, userId, merged)
   ├─ D1: SELECT port FROM user_container WHERE user_id=? AND status='running'
   ├─ POST /orch/proxy/{userId}/internal/apply-keys → Orchestrator
   │   └─ Orchestrator proxied zu http://localhost:{port}/internal/apply-keys
   └─ Container: internal_apply_keys(keys) → RAM
```

### Trigger 2: Beim Frontend-Zugriff (Auto-Unlock)

Bei jedem Dashboard-Aufruf prüft der Worker `/api/auth/status`:

```
GET /api/auth/status
├─ Worker proxied GET /auth/status zum Container
│   └─ Container antwortet: { setup_required, locked, unlocked }
├─ Wenn unlocked=true → fertig, nichts zu tun
├─ Wenn locked/setup_required:
│   ├─ loadSecrets(D1, userId, ENCRYPTION_KEY) → Keys aus D1
│   ├─ Wenn Keys vorhanden:
│   │   └─ autoInjectKeys() → POST /internal/apply-keys
│   │       └─ Antwort: { unlocked: true, auto_injected: true }
│   └─ Wenn keine Keys in D1:
│       └─ Antwort: { setup_required: true, d1_has_keys: false }
```

## Phase 3: Container empfängt Keys

### Endpoint (`server.py → /internal/apply-keys`)

```python
@app.post("/internal/apply-keys")
async def internal_apply_keys(body: dict):
    keys = body["keys"]  # Plaintext Keys
    _apply_secrets_to_settings(keys)  # → Settings-Objekt im RAM
    await _init_exchange_clients()    # Exchange-Clients initialisieren
    _vault_unlocked = True            # Vault als offen markieren
    # KEIN secrets.enc wird geschrieben!
```

**Wichtig**: Keys existieren **nur im RAM** des Containers. Es gibt **keine** `secrets.enc`-Datei auf Disk. Das ist by design.

### Was passiert in `_apply_secrets_to_settings`

Die Keys werden auf das globale `_settings`-Objekt gemappt:

```
extended_api_key      → settings.extended_api_key
extended_public_key   → settings.extended_public_key
extended_private_key  → settings.extended_private_key
grvt_api_key          → settings.grvt_api_key
grvt_private_key      → settings.grvt_private_key
variational_jwt_token → settings.variational_jwt_token
nado_private_key      → settings.nado_private_key
...
```

### Was passiert in `_init_exchange_clients`

Für jede Exchange wird ein Client erstellt, wenn die entsprechenden Keys vorhanden sind:

```
Extended:    ExtendedClient(api_key, public_key, private_key, vault)
GRVT:        GrvtClient(api_key, private_key, trading_account_id)
Variational: VariationalClient(jwt_token)
NADO:        NadoClient(private_key, wallet_address, ...)
```

## Phase 4: Container-Neustart

Bei einem Container-Neustart:

1. Container startet → Keys sind **weg** (RAM gelöscht)
2. Container meldet `setup_required: true` auf `/auth/status`
3. **Beim nächsten Frontend-Zugriff**:
   - Worker ruft `/api/auth/status` auf
   - Erkennt `setup_required` oder `locked`
   - Lädt Keys aus D1
   - Auto-Inject via `/internal/apply-keys`
   - Container ist wieder "unlocked"

**Lücke**: Zwischen Container-Start und erstem Frontend-Zugriff hat der Container keine Keys. Laufende Bots können in dieser Zeit nicht handeln.

## Netzwerkpfad

```
Browser → Cloudflare Worker (bot.defitool.de)
  → VPC Service Binding (NAS_BACKEND)
    → Orchestrator (192.168.133.253:8090)
      → /orch/proxy/{userId}/internal/apply-keys
        → httpx.AsyncClient → http://localhost:{port}/internal/apply-keys
          → Container (Port 9001+)
```

- **VPC Service Binding**: Cloudflare Tunnel-ähnlich, kein öffentliches Internet
- **Orch-Token**: `X-Orch-Token` Header zur Authentifizierung zwischen Worker und Orchestrator
- **Orchestrator-Proxy**: Leitet Requests basierend auf `user_id` → Container-Port weiter

## Dateien

| Datei | Zweck |
|-------|-------|
| `deploy/cloudflare/src/index.ts` | Worker: Routing, handleVaultStatus, handleUpdateKeys, autoInjectKeys |
| `deploy/cloudflare/src/lib/crypto.ts` | AES-256-GCM Verschlüsselung/Entschlüsselung |
| `deploy/cloudflare/src/lib/secrets.ts` | D1 CRUD: loadSecrets, saveSecrets, maskKeys, filterUpdates |
| `deploy/orchestrator/orchestrator.py` | Proxy: /orch/proxy/{user_id}/{path} → Container |
| `app/server.py` | Container: /internal/apply-keys, _apply_secrets_to_settings, _init_exchange_clients |

## Fehlerbehebung

### Container hat keine Keys nach Neustart
→ Normal. Keys werden beim nächsten Frontend-Zugriff automatisch aus D1 injiziert.

### Keys in D1 aber Container bleibt locked
→ Orchestrator prüfen: `docker logs ta-orchestrator`
→ Container-Port erreichbar? `/orch/proxy/{userId}/internal/apply-keys` testen.

### `secrets.enc` existiert nicht
→ **Korrekt**. Das D1-basierte System nutzt kein `secrets.enc`. Keys nur im RAM.

### Keys gehen nach Container-Neustart verloren
→ **By design**. D1 ist die persistente Quelle. Auto-Inject bei Frontend-Zugriff.

### User kann keine Keys setzen
→ Session-Auth prüfen (better-auth). D1 `user_secrets`-Tabelle prüfen.
→ `ENCRYPTION_KEY` Secret in Wrangler gesetzt?
