# TradeAutonom

Multi-Exchange Arbitrage Bot mit WebUI. Unterstützt **GRVT** und **Extended Exchange**.

---

## Quick Start (Clean Setup)

### 1. Repository klonen

```bash
git clone https://github.com/dhaussmann/tradeautonom.git
cd tradeautonom
```

### 2. Python Environment einrichten

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Konfiguration (.env)

```bash
cp .env.example .env
```

`.env` ausfüllen:

| Variable | Pflicht | Beschreibung |
|---|---|---|
| `GRVT_API_KEY` | Ja | GRVT API Key |
| `GRVT_PRIVATE_KEY` | Ja | Private Key für Order-Signierung |
| `GRVT_TRADING_ACCOUNT_ID` | Ja | Trading (Sub-)Account ID |
| `GRVT_ENV` | Ja | `testnet` oder `prod` |

Optionale Parameter:

| Variable | Default | Beschreibung |
|---|---|---|
| `APP_HOST` | `0.0.0.0` | Server Host |
| `APP_PORT` | `8000` | Server Port |
| `DEFAULT_SLIPPAGE_PCT` | `0.5` | Max Slippage % |
| `MIN_ORDER_BOOK_DEPTH_USD` | `1000.0` | Min Orderbook-Tiefe (USD) |
| `ARB_SPREAD_ENTRY_LOW` | `2.0` | Entry wenn Spread <= Wert |
| `ARB_SPREAD_EXIT_HIGH` | `8.0` | Exit wenn Spread >= Wert |
| `ARB_MAX_EXEC_SPREAD` | `5.0` | Max Bid-Ask Execution Cost |
| `ARB_QUANTITY` | `1.0` | Quantity pro Leg |
| `ARB_XAU_INSTRUMENT` | `XAU_USDT_Perp` | Instrument Leg A |
| `ARB_PAXG_INSTRUMENT` | `PAXG_USDT_Perp` | Instrument Leg B |
| `ARB_LEG_A_EXCHANGE` | `grvt` | Exchange für Leg A (`grvt` oder `extended`) |
| `ARB_LEG_B_EXCHANGE` | `grvt` | Exchange für Leg B (`grvt` oder `extended`) |
| `ARB_CHUNK_SIZE` | `1.0` | Order-Chunk-Größe |
| `ARB_CHUNK_DELAY_MS` | `500` | Pause zwischen Chunks (ms) |
| `ARB_SIMULATION_MODE` | `False` | Paper-Trading (keine echten Orders) |
| `EXTENDED_API_BASE_URL` | `https://api.starknet.extended.exchange/api/v1` | Extended API URL |
| `EXTENDED_API_KEY` | *(leer)* | Extended API Key (für Trading) |
| `EXTENDED_PUBLIC_KEY` | *(leer)* | Extended Stark Public Key |
| `EXTENDED_PRIVATE_KEY` | *(leer)* | Extended Stark Private Key |
| `EXTENDED_VAULT` | `0` | Extended Vault Nummer |

### 4. Starten

```bash
python main.py
```

Öffne im Browser:
- **Dashboard:** http://localhost:8000/ui
- **API Docs:** http://localhost:8000/docs

---

## Docker (Alternative)

```bash
# Build
docker build -f docker/Dockerfile -t tradeautonom:latest .

# Run
docker run -d --name tradeautonom -p 8000:8000 --env-file .env tradeautonom:latest

# Oder mit docker-compose
cd docker && docker-compose up -d
```

---

## Features

- **Multi-Exchange** — GRVT + Extended Exchange, pro Leg frei konfigurierbar
- **Spread Monitoring** — Live-Spread zwischen zwei beliebigen Instrumenten
- **Arbitrage Engine** — Automatischer Entry/Exit basierend auf Spread-Schwellwerten
- **Simulation Mode** — Paper-Trading ohne echte Orders
- **Order Chunking** — Große Orders in kleinere Chunks aufteilen
- **Safety Checks** — Orderbook-Tiefe, Slippage, Liquiditätsprüfung, Leg-Unwinding
- **WebUI Dashboard** — Echtzeit-Monitoring, Konfiguration, manuelle Trades

## API Übersicht

| Endpoint | Methode | Beschreibung |
|---|---|---|
| `/ui` | GET | WebUI Dashboard |
| `/health` | GET | Health Check |
| `/arb/check` | GET | Spread + Empfehlung (ohne Execution) |
| `/arb/status` | GET | Engine-Status + Konfiguration |
| `/arb/config` | POST | Konfiguration zur Laufzeit ändern |
| `/arb/trigger` | POST | Manueller Entry/Exit |
| `/arb/auto` | POST | Auto-Check + Execute |
| `/exchanges` | GET | Verfügbare Exchanges |
| `/exchanges/markets?exchange=X` | GET | Instrumente einer Exchange |
| `/trade` | POST | Einzelne Market Order |
| `/account/summary` | GET | Kontoinformationen |
| `/account/positions` | GET | Offene Positionen |

## Projektstruktur

```
tradeautonom/
├── main.py                  # Entry Point (uvicorn)
├── requirements.txt
├── .env.example
├── docker/
│   ├── Dockerfile           # Multi-Stage Build (Python 3.11-slim)
│   └── docker-compose.yml
├── static/
│   └── index.html           # WebUI Dashboard
└── app/
    ├── config.py            # Settings aus .env (pydantic-settings)
    ├── exchange.py          # ExchangeClient Protocol
    ├── grvt_client.py       # GRVT SDK Wrapper
    ├── extended_client.py   # Extended Exchange REST Client
    ├── safety.py            # Orderbook-Tiefe + Slippage Checks
    ├── executor.py          # Trade Executor mit Validierung
    ├── arbitrage.py         # Spread-Trading Engine (Multi-Exchange)
    ├── schemas.py           # Pydantic Request/Response Models
    └── server.py            # FastAPI Endpoints
```
