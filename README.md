# TradeAutonom — GRVT Trade Execution Service

A Python service that executes trades on [GRVT](https://grvt.io) (Gravity Markets) with full safety checks. Supports:

1. **Single market order execution** — send a token, quantity, and expected price; the service validates order-book depth and slippage before placing the order.
2. **XAU / PAXG arbitrage** — monitors the spread between two correlated gold instruments. Opens a long/short spread when the price gap is small, and closes it when the gap widens.

## Architecture

```
POST /trade          →  TradeExecutor  →  safety checks  →  GRVT market order
POST /arb/trigger    →  ArbitrageEngine →  safety checks  →  GRVT market order (2 legs)
GET  /arb/check      →  spread snapshot + recommended action (no execution)
POST /arb/spread     →  spread query with optional instrument override
GET  /account/summary
GET  /account/positions?symbols=BTC_USDT_Perp,ETH_USDT_Perp
GET  /health
```

## Safety Checks

Every order goes through these validations before execution:

- **Order-book depth** — walks the book to confirm enough liquidity exists for the requested quantity.
- **Slippage validation** — computes the volume-weighted average fill price and rejects if slippage exceeds the configured threshold.
- **Arb leg unwinding** — if one leg of an arb trade fails, the service immediately attempts to unwind the completed leg to avoid unhedged exposure.

## Setup

### 1. Prerequisites

- Python 3.11+
- A GRVT account with API key (see [API Setup Guide](https://api-docs.grvt.io/api_setup/))

### 2. Install

```bash
cd tradeautonom
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure

Copy the example env file and fill in your credentials:

```bash
cp .env.example .env
```

Required variables:

| Variable | Description |
|---|---|
| `GRVT_API_KEY` | Your GRVT API key |
| `GRVT_PRIVATE_KEY` | Private key for order signing |
| `GRVT_TRADING_ACCOUNT_ID` | Your trading (sub) account ID |
| `GRVT_ENV` | `testnet` or `prod` |

Optional overrides:

| Variable | Default | Description |
|---|---|---|
| `DEFAULT_SLIPPAGE_PCT` | `0.5` | Default max slippage % |
| `MAX_SLIPPAGE_PCT` | `2.0` | Hard cap on slippage % |
| `MIN_ORDER_BOOK_DEPTH_USD` | `1000.0` | Min required book depth |
| `ARB_ENTRY_SPREAD` | `1.0` | Open arb when spread ≤ this |
| `ARB_EXIT_SPREAD` | `5.0` | Close arb when spread ≥ this |
| `ARB_QUANTITY` | `1.0` | Quantity for each arb leg |
| `ARB_XAU_INSTRUMENT` | `XAU_USDT_Perp` | First arb instrument |
| `ARB_PAXG_INSTRUMENT` | `PAXG_USDT_Perp` | Second arb instrument |

### 4. Run

```bash
python main.py
```

The API will be available at `http://localhost:8000`. Interactive docs at `http://localhost:8000/docs`.

## API Usage

### Execute a single trade

```bash
curl -X POST http://localhost:8000/trade \
  -H "Content-Type: application/json" \
  -d '{
    "symbol": "BTC_USDT_Perp",
    "side": "buy",
    "quantity": 0.01,
    "expected_price": 87000.0,
    "slippage_pct": 0.5
  }'
```

### Check arb spread

```bash
curl http://localhost:8000/arb/check
```

### Trigger arb entry

```bash
curl -X POST http://localhost:8000/arb/trigger \
  -H "Content-Type: application/json" \
  -d '{"action": "ENTRY"}'
```

### Trigger arb exit

```bash
curl -X POST http://localhost:8000/arb/trigger \
  -H "Content-Type: application/json" \
  -d '{"action": "EXIT"}'
```

### Override arb parameters at trigger time

```bash
curl -X POST http://localhost:8000/arb/trigger \
  -H "Content-Type: application/json" \
  -d '{
    "action": "ENTRY",
    "entry_spread": 0.5,
    "quantity": 2.0
  }'
```

## Arbitrage Flow

```
1. Spread narrows to ≤ $1 (entry_spread)
   → POST /arb/trigger {"action": "ENTRY"}
   → LONG the cheaper token, SHORT the expensive one
   → Both legs validated for depth + slippage

2. Spread widens to ≥ $5 (exit_spread)
   → POST /arb/trigger {"action": "EXIT"}
   → Close both positions
   → Profit = spread_exit - spread_entry - fees

If any leg fails, the service automatically tries to unwind
the completed leg to prevent unhedged exposure.
```

## Project Structure

```
tradeautonom/
├── main.py              # Entry point (uvicorn)
├── requirements.txt
├── .env.example
├── README.md
└── app/
    ├── __init__.py
    ├── config.py        # Pydantic settings from .env
    ├── grvt_client.py   # GRVT SDK wrapper (auth, orders, market data)
    ├── safety.py        # Order-book depth + slippage checks
    ├── executor.py      # Trade executor with pre-trade validation
    ├── arbitrage.py     # XAU/PAXG spread trading engine
    ├── schemas.py       # Pydantic request/response models
    └── server.py        # FastAPI endpoints
```
