# TradeAutonom — Architektur

Multi-Exchange Arbitrage & Delta-Neutral Trading Bot mit WebUI, deployed als Docker-Container auf einer Synology NAS.

## Übersicht

```
┌─────────────────────────────────────────────────────────┐
│                      WebUI (static/)                     │
│                    index.html + SSE Stream                │
└──────────────────────┬──────────────────────────────────┘
                       │ HTTP/SSE
┌──────────────────────▼──────────────────────────────────┐
│                  FastAPI Server (app/server.py)           │
│   Auth (Vault) │ Jobs API │ Trading API │ WebSocket Mgmt │
└──────┬─────────┬─────────┬──────────────────────────────┘
       │         │         │
┌──────▼───┐ ┌───▼────┐ ┌──▼──────────────────────────────┐
│ Crypto   │ │ Job    │ │ Engine (app/engine.py)            │
│ Vault    │ │ Manager│ │  ├─ FundingArbEngine (arbitrage)  │
│ (AES256) │ │        │ │  ├─ StateMachine (TWAP execution) │
└──────────┘ └────────┘ │  ├─ DataLayer (WS feeds + cache)  │
                        │  ├─ FundingMonitor                 │
                        │  └─ RiskManager                    │
                        └──────┬──────────────────────────────┘
                               │
       ┌───────────┬───────────┼───────────┬──────────────┐
       │           │           │           │              │
  ┌────▼───┐  ┌────▼────┐ ┌───▼────┐ ┌────▼────┐  ┌─────▼────┐
  │Extended│  │  GRVT   │ │Variat. │ │  Nado   │  │ (future) │
  │Client  │  │ Client  │ │ Client │ │ Client  │  │          │
  └────────┘  └─────────┘ └────────┘ └─────────┘  └──────────┘
```

## Module (`app/`)

| Modul | Beschreibung |
|---|---|
| `server.py` | FastAPI-Server: REST API, SSE-Stream, Auth/Vault, Job-CRUD |
| `engine.py` | Orchestrierung: startet DataLayer, FundingMonitor, StateMachine pro Job |
| `arbitrage.py` | Spread-Monitoring, Entry/Exit-Signale, Funding-Rate-Bewertung |
| `state_machine.py` | TWAP-Ausführung: Maker-Post-Only → Taker-IOC-Hedge, Repricing, Position-Repair |
| `data_layer.py` | WebSocket-Manager: Orderbook-Feeds, Position-Feeds, Fill-Events, REST-Fallback |
| `job_manager.py` | Multi-Job-Verwaltung: Start/Stop/Config, Persistenz, Tick-Loop |
| `config.py` | Pydantic Settings aus `.env` |
| `schemas.py` | Pydantic Request/Response-Modelle |
| `risk_manager.py` | Delta-Limits, Circuit-Breaker, Spread-Guards |
| `funding_monitor.py` | Funding-Rate-Polling für alle Exchanges |
| `safety.py` | Orderbook-Tiefe, Slippage-Checks |
| `executor.py` | Legacy Trade-Executor (Single-Order) |
| `exchange.py` | ExchangeClient Protocol-Definition |
| `crypto.py` | AES-256-GCM Encryption für Vault (API Keys) |
| `bot_registry.py` | Bot-Instanz-Registry |
| `ws_feeds.py` | WebSocket Feed-Helpers |

## Exchange Clients

| Exchange | Client | Typ | Besonderheiten |
|---|---|---|---|
| **Extended** | `extended_client.py` | CEX (StarkNet) | x10 SDK, Post-Only + IOC, WS Orderbook + Account Stream |
| **GRVT** | `grvt_client.py` | CEX | REST + WS (v1.fill, v1.position, v1.order), Cookie-Auth |
| **Variational** | `variational_client.py` | DEX (RFQ) | OLP als Maker, Quote→Market-Order Flow, curl_cffi (Cloudflare) |
| **Nado** | `nado_client.py` | DEX | WS Position + Fill Streams |

## Strategien

- **Arbitrage** (`strategy: "arbitrage"`): Entry bei großem Spread, Exit bei kleinem Spread (Mean-Reversion)
- **Delta-Neutral** (`strategy: "delta_neutral"`): Entry UND Exit bei kleinem Spread (Funding-Rate-Harvesting)

## Execution Flow (TWAP)

1. **Job-Start** → `engine.py` erstellt `StateMachine`
2. **Chunk-Loop**: Teilt Total-Qty in N Chunks auf
3. **Pro Chunk**:
   - Maker: Post-Only Order bei best bid/ask ± Offset
   - Chase-Loop: Timeout → Cancel → Reprice → Retry
   - Taker: IOC-Hedge sofort nach Maker-Fill
   - Position-Verify: REST-Query beider Exchanges, Delta von Baseline
   - Repair: IOC auf Taker-Seite falls Gap > min_repair_qty

## Instanzen

Alle Instanzen nutzen **denselben Code** (`app/`). Unterschiede sind rein infrastrukturell.

| Instanz | Port | Container | Zweck |
|---|---|---|---|
| **prod** | 8002 | `tradeautonom` | Production Trading |
| **v2** | 8004 | `tradeautonom-v2` | Test/Staging |
| **v3** | 8005 | `tradeautonom-v3` | Multi-User mit Vault/Encryption |
| **dashboard** | 8003 | `tradeautonom-dashboard` | Read-Only Account-Übersicht |
| **local** | 8000 | `tradeautonom` | Dev-Setup (Code bind-mounted) |

Details: siehe `deploy/*/README.md`

## Deployment

Alle Instanzen werden auf eine **Synology NAS** deployed via SSH + Docker:

```bash
# Production
./deploy/prod/deploy.sh

# Test
./deploy/v2/deploy.sh

# Multi-User
./deploy/v3/deploy.sh

# Dashboard
./deploy/dashboard/deploy.sh

# Lokal (Dev)
cd deploy/local && docker-compose up -d
```

## Daten-Persistenz

- **`data/`** — Trade-Logs, Position-State, verschlüsselte API-Keys (pro Instanz isoliert)
- **`.env`** — Konfiguration (nicht deployed, liegt auf NAS separat)
