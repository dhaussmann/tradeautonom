# Local — Dev-Setup

Lokales Development-Setup mit Code-Bind-Mount. Code-Änderungen erfordern nur einen Container-Restart, kein Rebuild.

| Eigenschaft | Wert |
|---|---|
| **Port** | 8000 (default) oder aus `.env` APP_PORT |
| **Container** | `tradeautonom` |
| **Code** | Bind-mounted (`app/`, `static/`, `main.py` als `:ro`) |
| **Konfiguration** | `.env` im Projekt-Root |
| **Daten** | `data/` im Projekt-Root |

## Starten

```bash
# Mit docker-compose
cd deploy/local && docker-compose up -d

# Oder direkt mit Python (ohne Docker)
python main.py
```

## Besonderheiten

- **Kein Code-Bake**: Dockerfile installiert nur Dependencies, Code wird per Volume gemountet
- **Schneller Dev-Cycle**: Code ändern → `docker-compose restart` (kein Build nötig)
- **Rebuild nur bei `requirements.txt`-Änderungen**: `docker-compose up -d --build`
