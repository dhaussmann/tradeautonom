# Dashboard — Read-Only Account-Übersicht

Separater Container mit eigenem Code (`dashboard/server.py`), zeigt Account-Daten ohne Trading-Funktionalität.

| Eigenschaft | Wert |
|---|---|
| **Port** | 8003 (aus `.env` DASHBOARD_PORT) |
| **Container** | `tradeautonom-dashboard` |
| **Image** | `tradeautonom-dashboard:latest` |
| **NAS-Pfad** | Nutzt prod-Pfad (`/volume1/docker/tradeautonom/`) |
| **Konfiguration** | `.env` von prod |
| **Code** | `dashboard/server.py` + `dashboard/main.py` (eigener Code, nicht `app/`) |

## Deployment

```bash
./deploy/dashboard/deploy.sh              # Full: sync + build + start
./deploy/dashboard/deploy.sh --restart    # Nur Container neustarten
./deploy/dashboard/deploy.sh --logs       # Live-Logs
./deploy/dashboard/deploy.sh --stop       # Container stoppen
./deploy/dashboard/deploy.sh --status     # Health-Status
```

## Besonderheiten

- **Eigener Code**: Nutzt `dashboard/server.py` statt `app/server.py`
- **Eigenes Dockerfile**: `deploy/dashboard/Dockerfile`
- **Read-Only**: Kein Trading, nur Account-Ansicht
- Shared `.env` mit prod (gleiche Exchange-Credentials)
