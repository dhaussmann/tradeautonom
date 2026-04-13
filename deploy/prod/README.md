# Production (prod)

Haupt-Trading-Instanz auf der Synology NAS.

| Eigenschaft | Wert |
|---|---|
| **Port** | 8002 (aus `.env` APP_PORT) |
| **Container** | `tradeautonom` |
| **Image** | `tradeautonom:latest` |
| **NAS-Pfad** | `/volume1/docker/tradeautonom/` |
| **Konfiguration** | `.env` auf NAS |
| **Daten** | `data/` Volume (Trade-Logs, State) |

## Deployment

```bash
./deploy/prod/deploy.sh              # Full: sync + build + start
./deploy/prod/deploy.sh --restart    # Nur Container neustarten
./deploy/prod/deploy.sh --logs       # Live-Logs
./deploy/prod/deploy.sh --stop       # Container stoppen
./deploy/prod/deploy.sh --status     # Health + Job-Status
```

## Dateien

- `deploy.sh` — Deploy-Skript (SSH → NAS)
- `Dockerfile` — Self-contained NAS Image (Code baked in)
- `docker-compose.yml` — Alternative zu deploy.sh

## Besonderheiten

- API-Keys in `.env` auf der NAS (nicht im Repo)
- Nutzt `Dockerfile` mit eingebautem Code (kein bind-mount)
- Healthcheck alle 30s
