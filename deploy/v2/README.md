# V2 — Test / Staging

Test-Instanz, läuft parallel zur Production auf separatem Port und Data-Volume.

| Eigenschaft | Wert |
|---|---|
| **Port** | 8004 |
| **Container** | `tradeautonom-v2` |
| **Image** | `tradeautonom:v2` |
| **NAS-Pfad** | `/volume1/docker/tradeautonom-v2/` |
| **Konfiguration** | `.env` kopiert von prod (falls nicht vorhanden) |
| **Daten** | Eigenes `data/` Volume (isoliert von prod) |

## Deployment

```bash
./deploy/v2/deploy.sh              # Full: sync + build + start
./deploy/v2/deploy.sh --restart    # Nur Container neustarten
./deploy/v2/deploy.sh --logs       # Live-Logs
./deploy/v2/deploy.sh --stop       # Container stoppen
./deploy/v2/deploy.sh --status     # Health-Status
```

## Besonderheiten

- Nutzt dasselbe `Dockerfile` wie prod (`deploy/prod/Dockerfile`)
- Eigenes Data-Volume — keine Kollision mit Production
- `.env` wird beim ersten Start von prod kopiert, kann danach unabhängig angepasst werden
