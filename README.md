# TradeAutonom

Multi-Exchange Arbitrage & Delta-Neutral Trading Bot mit WebUI. Unterstützt **Extended**, **GRVT**, **Variational** und **Nado**.

---

## Quick Start

```bash
# 1. Clone + Setup
git clone https://github.com/dhaussmann/tradeautonom.git
cd tradeautonom
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Konfiguration
cp .env.example .env   # .env ausfüllen (API Keys, Ports, etc.)

# 3. Starten
python main.py         # → http://localhost:8000/ui
```

---

## Features

- **4 Exchanges** — Extended (StarkNet), GRVT, Variational (RFQ-DEX), Nado
- **2 Strategien** — Arbitrage (Mean-Reversion) + Delta-Neutral (Funding-Harvesting)
- **TWAP Execution** — Maker Post-Only → Taker IOC-Hedge, auto Repricing + Repair
- **Multi-Job** — Mehrere Arb-Jobs parallel, unabhängige Konfiguration
- **Vault** — AES-256-GCM verschlüsselte API-Keys, Password-Auth via WebUI
- **Live-Monitoring** — SSE-Stream, Spread-Charts, Position-Tracking, Activity-Log
- **Risk Management** — Delta-Limits, Circuit-Breaker, Spread-Guards, Position-Verify

---

## Instanzen

Alle Instanzen nutzen **denselben Code** (`app/`). Unterschiede sind rein infrastrukturell.

| Instanz | Port | Zweck | Deploy |
|---|---|---|---|
| **prod** | 8002 | Production Trading | `./deploy/prod/deploy.sh` |
| **v2** | 8004 | Test / Staging | `./deploy/v2/deploy.sh` |
| **v3** | 8005 | Multi-User + Vault | `./deploy/v3/deploy.sh` |
| **dashboard** | 8003 | Read-Only Account-Übersicht | `./deploy/dashboard/deploy.sh` |
| **local** | 8000 | Dev (Code bind-mounted) | `cd deploy/local && docker-compose up -d` |

Details: siehe `deploy/*/README.md`

---

## Projektstruktur

```
tradeautonom/
├── main.py                        # Entry Point (uvicorn)
├── requirements.txt
├── .env / .env.example
│
├── app/                           # Haupt-Applikation
│   ├── server.py                  #   FastAPI Server + Auth + SSE
│   ├── engine.py                  #   Orchestrierung (DataLayer, StateMachine)
│   ├── arbitrage.py               #   Spread-Monitoring + Entry/Exit-Signale
│   ├── state_machine.py           #   TWAP: Maker→Taker, Repricing, Repair
│   ├── data_layer.py              #   WS Orderbook/Position/Fill Feeds
│   ├── job_manager.py             #   Multi-Job Verwaltung + Persistenz
│   ├── config.py                  #   Pydantic Settings
│   ├── schemas.py                 #   Request/Response Models
│   ├── risk_manager.py            #   Delta-Limits, Circuit-Breaker
│   ├── funding_monitor.py         #   Funding-Rate Polling
│   ├── extended_client.py         #   Extended Exchange (x10 SDK)
│   ├── grvt_client.py             #   GRVT Exchange
│   ├── variational_client.py      #   Variational DEX (RFQ)
│   ├── nado_client.py             #   Nado Exchange
│   ├── exchange.py                #   ExchangeClient Protocol
│   ├── crypto.py                  #   AES-256-GCM Vault
│   └── ...
│
├── static/                        # WebUI
│   ├── index.html                 #   Trading Dashboard
│   └── dashboard.html             #   Account Dashboard
│
├── deploy/                        # Deployment-Konfigurationen
│   ├── prod/                      #   Production (Port 8002)
│   ├── v2/                        #   Test/Staging (Port 8004)
│   ├── v3/                        #   Multi-User (Port 8005)
│   ├── dashboard/                 #   Read-Only Dashboard (Port 8003)
│   └── local/                     #   Dev-Setup (bind-mount)
│
├── dashboard/                     # Dashboard-Server (eigener Code)
├── scripts/                       # Utility-Skripte
│   ├── monitor.sh                 #   Remote Monitoring CLI
│   └── analyze_chunks.py          #   Debug-Tool
│
└── docs/                          # Dokumentation
    ├── architecture.md            #   Architektur + Module + Flow
    ├── safety-balance-verification.md
    └── taker-execution.md
```

Ausführliche Architektur-Doku: siehe [docs/architecture.md](docs/architecture.md)
