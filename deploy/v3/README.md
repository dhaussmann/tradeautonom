# V3 — Multi-User mit Vault

Multi-User-Instanz mit verschlüsseltem API-Key-Speicher (Vault). Keine `.env`-basierte API-Key-Konfiguration — alles über die WebUI.

| Eigenschaft | Wert |
|---|---|
| **Port** | 8005 |
| **Container** | `tradeautonom-v3` |
| **Image** | `tradeautonom:v3` |
| **NAS-Pfad** | `/volume1/docker/tradeautonom-v3/` |
| **Konfiguration** | Minimale `.env.container` (nur Port + Host) |
| **Daten** | Eigenes `data/` Volume (verschlüsselte Keys + State) |

## Deployment

```bash
./deploy/v3/deploy.sh              # Full: sync + build + start
./deploy/v3/deploy.sh --restart    # Nur Container neustarten
./deploy/v3/deploy.sh --logs       # Live-Logs
./deploy/v3/deploy.sh --stop       # Container stoppen
./deploy/v3/deploy.sh --status     # Health-Status
```

## Multi-User Management

```bash
./deploy/v3/manage.sh create <user_id>    # Neuen User-Container erstellen
./deploy/v3/manage.sh list                # Alle User auflisten
./deploy/v3/manage.sh start <user_id>     # User-Container starten
./deploy/v3/manage.sh stop <user_id>      # User-Container stoppen
./deploy/v3/manage.sh destroy <user_id>   # User-Container entfernen
./deploy/v3/manage.sh logs <user_id>      # User-Logs
./deploy/v3/manage.sh update              # Image rebuild + alle User neustarten
```

## Besonderheiten

- **Vault**: AES-256-GCM verschlüsselte API-Keys in `data/secrets.enc`
- **Auth**: Password-basiertes Setup/Unlock über WebUI (`/auth/setup`, `/auth/unlock`)
- **Keine `.env`-Keys**: Container startet im LOCKED-Zustand, Keys werden über UI eingegeben
- **`manage.sh`**: Erstellt isolierte User-Container mit eigenem Port + Data-Volume
- Nutzt dasselbe `Dockerfile` wie prod (`deploy/prod/Dockerfile`)
