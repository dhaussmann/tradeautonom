# Nado Watchdog WebSocket System

Complete guide to the Nado WebSocket system with dynamic symbol management, health monitoring, and real-time orderbook streaming.

---

## Overview

The Nado Watchdog System manages WebSocket connections to the Nado exchange with automatic discovery, health monitoring, and dynamic symbol management. It replaces the static symbol list with an intelligent system that adapts to market activity.

**Key Features:**
- **Dynamic Symbol Management**: Automatically discovers and tracks active PERP markets
- **Health Monitoring**: Detects inactive markets and retries with exponential backoff
- **Real-time WebSocket**: Single shared connection for all symbols
- **REST API**: Full visibility into symbol states and health
- **Persistence**: State saved to disk, survives restarts

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    NADO WATCHDOG SYSTEM                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  ┌─────────────────┐      ┌──────────────────┐                  │
│  │  Watchdog Core  │◄────►│  WebSocket Mgr   │                  │
│  │  (state machine)│      │  (1 connection)  │                  │
│  └────────┬────────┘      └────────┬─────────┘                  │
│           │                        │                            │
│           ▼                        ▼                            │
│  ┌─────────────────┐      ┌──────────────────┐                  │
│  │  Health Monitor │      │  Nado Exchange   │                  │
│  │  (10s interval) │      │  (ws + REST)     │                  │
│  └─────────────────┘      └──────────────────┘                  │
│           │                                                     │
│           ▼                                                     │
│  ┌─────────────────┐      ┌──────────────────┐                  │
│  │  Discovery      │      │  Persistence     │                  │
│  │  (every 24h)    │      │  (on changes)    │                  │
│  └─────────────────┘      └──────────────────┘                  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │  OMS WebSocket   │
                    │  /ws endpoint    │
                    └──────────────────┘
```

---

## Symbol States

Each Nado symbol is in one of these states:

| State | Description | Transitions |
|-------|-------------|-------------|
| **ACTIVE** | Receiving regular updates | → SUSPECT (no update for 2min) |
| **SUSPECT** | No updates for 2 minutes, entering retry | → RETRYING |
| **RETRYING** | In retry loop with exponential backoff | → ACTIVE (recovery) or INACTIVE (max retries) |
| **INACTIVE** | Failed all 10 retry attempts | → CANDIDATE (daily reactivation) |
| **CANDIDATE** | New symbol from discovery, pending test | → TESTING |
| **TESTING** | Currently being tested (30 seconds) | → ACTIVE (has updates) or INACTIVE (no updates) |

### State Transition Diagram

```
                    ┌──────────┐
         ┌─────────│  ACTIVE  │◄──────────────────────────┐
         │         └────┬─────┘                           │
         │              │ 2min no update                  │
         │              ▼                                 │
         │         ┌──────────┐                          │
         │         │ SUSPECT  │                          │
         │         └────┬─────┘                          │
         │              │                                 │
         │              ▼                                 │
         │    ┌──────────────────┐    recovery          │
         └────┤    RETRYING      ├───────────────────────┘
              │  (10 attempts)   │
              └────────┬─────────┘
                       │ 10 failed retries
                       ▼
              ┌──────────────────┐
              │    INACTIVE      │◄──────────────────┐
              └────────┬─────────┘                   │
                       │ 24h reactivation           │
                       ▼                            │
              ┌──────────────────┐    no updates    │
              │   CANDIDATE      ├──────────────────┘
              └────────┬─────────┘
                       │ 30s test
                       ▼
              ┌──────────────────┐
              │    TESTING       │
              └──────────────────┘
```

---

## WebSocket Usage

### Connect to OMS

```bash
wscat -c ws://192.168.133.100:8099/ws
```

### Subscribe to Nado Symbols

```json
{"action": "subscribe", "exchange": "nado", "symbol": "XRP-PERP"}
```

### Receive Updates

The server sends real-time orderbook updates:

```json
{
  "type": "book",
  "exchange": "nado",
  "symbol": "XRP-PERP",
  "bids": [[1.4547, 1340.0], [1.4546, 2500.0], ...],
  "asks": [[1.4548, 3820.0], [1.4549, 1800.0], ...],
  "timestamp_ms": 1713791234567
}
```

### Unsubscribe

```json
{"action": "unsubscribe", "exchange": "nado", "symbol": "XRP-PERP"}
```

### Subscribe to Multiple Symbols

```json
{"action": "subscribe", "exchange": "nado", "symbol": "BTC-PERP"}
{"action": "subscribe", "exchange": "nado", "symbol": "ETH-PERP"}
{"action": "subscribe", "exchange": "nado", "symbol": "SOL-PERP"}
```

---

## REST API Endpoints

### GET /nado/symbols

Returns status of all Nado symbols grouped by state.

**Example:**
```bash
curl http://192.168.133.100:8099/nado/symbols | python3 -m json.tool
```

**Response:**
```json
{
  "timestamp": "2026-04-22T12:15:46Z",
  "summary": {
    "total": 55,
    "active": 47,
    "retrying": 7,
    "inactive": 1
  },
  "symbols": {
    "active": [
      {"symbol": "BTC-PERP", "product_id": 2, "state": "active", "update_count": 8590}
    ],
    "retrying": [
      {"symbol": "ADA-PERP", "product_id": 60, "state": "retrying", "retry_attempt": 1}
    ],
    "inactive": [
      {"symbol": "SKR-PERP", "product_id": 44, "state": "inactive"}
    ]
  }
}
```

### GET /nado/symbol/{symbol}

Returns detailed information about a specific symbol.

**Example:**
```bash
curl http://192.168.133.100:8099/nado/symbol/XRP-PERP | python3 -m json.tool
```

**Response:**
```json
{
  "symbol": "XRP-PERP",
  "product_id": 10,
  "state": "active",
  "last_update": "2026-04-22T12:08:52Z",
  "seconds_since_update": 2.5,
  "update_count": 461,
  "book": {
    "bid_levels": 20,
    "ask_levels": 20,
    "has_data": true,
    "connected": true
  }
}
```

### GET /nado/health

Returns watchdog system health status.

**Example:**
```bash
curl http://192.168.133.100:8099/nado/health | python3 -m json.tool
```

**Response:**
```json
{
  "available": true,
  "timestamp": "2026-04-22T12:08:33Z",
  "statistics": {
    "total": 55,
    "by_state": {
      "active": 47,
      "retrying": 7,
      "inactive": 1
    }
  },
  "websocket": {
    "connected": true,
    "subscribed_symbols": 55,
    "endpoint": "wss://gateway.prod.nado.xyz/v1/subscribe"
  },
  "persistence": {
    "exists": true,
    "path": "/app/data/nado_watchdog_state.json"
  }
}
```

### POST /nado/force-discovery

Manually triggers symbol discovery and testing.

**Example:**
```bash
curl -X POST http://192.168.133.100:8099/nado/force-discovery | python3 -m json.tool
```

**Response:**
```json
{
  "success": true,
  "timestamp": "2026-04-22T12:00:00Z",
  "result": {
    "discovered": 2,
    "tested": 2,
    "promoted": 1,
    "new_symbols": ["NEWSYM-PERP"]
  }
}
```

### GET /book/nado/{symbol}

Returns current orderbook snapshot.

**Example:**
```bash
curl http://192.168.133.100:8099/book/nado/XRP-PERP | python3 -m json.tool
```

**Response:**
```json
{
  "exchange": "nado",
  "symbol": "XRP-PERP",
  "bids": [[1.4547, 1340.0], [1.4546, 2500.0], ...],
  "asks": [[1.4548, 3820.0], [1.4549, 1800.0], ...],
  "timestamp_ms": 1713791234567,
  "connected": true,
  "updates": 5870
}
```

---

## Configuration

Configuration is stored in `/app/nado_config.yaml`:

```yaml
# Default active symbols (loaded on first startup)
default_active_symbols:
  - BTC-PERP
  - ETH-PERP
  - SOL-PERP
  # ... 53 symbols total

# Watchdog behavior
watchdog:
  suspect_threshold_seconds: 120      # 2 minutes before retry
  retry_max_attempts: 10
  retry_base_delay_seconds: 10
  retry_max_delay_seconds: 7200       # 2 hours
  reactivation_check_interval_hours: 24
  state_persistence_path: "/app/data/nado_watchdog_state.json"

# Discovery settings
discovery:
  interval_hours: 24                  # Check for new symbols daily
  test_duration_seconds: 30           # Test new symbols for 30s
  nado_api_url: "https://gateway.prod.nado.xyz"

# WebSocket settings
websocket:
  endpoint: "wss://gateway.prod.nado.xyz/v1/subscribe"
  subscription_delay_ms: 50           # 50ms between subscribe messages
```

---

## Retry Schedule

When a symbol becomes SUSPECT, it enters the retry loop:

| Attempt | Delay | Cumulative Time |
|---------|-------|-----------------|
| 1 | 10s | 10s |
| 2 | 20s | 30s |
| 3 | 40s | 70s |
| 4 | 80s | 150s (2.5min) |
| 5 | 160s | 310s (5min) |
| 6 | 320s | 630s (10min) |
| 7 | 640s | 1270s (21min) |
| 8 | 1280s | 2550s (42min) |
| 9 | 2560s | 5110s (85min) |
| 10 | 7200s (2h) | ~3.4h total |

If the symbol receives any update during a retry, it immediately returns to ACTIVE state.

---

## Available Symbols

As of deployment, the system tracks 55 Nado PERP symbols:

### Crypto (Active)
- BTC-PERP, ETH-PERP, SOL-PERP, XRP-PERP
- BNB-PERP, DOGE-PERP, ADA-PERP, LINK-PERP
- BCH-PERP, LTC-PERP, XMR-PERP, NEAR-PERP
- And more...

### Stocks (Active)
- AAPL-PERP, TSLA-PERP, NVDA-PERP, MSFT-PERP
- AMZN-PERP, GOOGL-PERP, META-PERP

### Forex (Active)
- EURUSD-PERP, GBPUSD-PERP, USDJPY-PERP

### Currently Inactive
- SKR-PERP (no trading activity)

Check `/nado/symbols` endpoint for current status.

---

## Troubleshooting

### Check WebSocket Connection

```bash
# Health check
curl http://192.168.133.100:8099/nado/health

# View logs
ssh root@192.168.133.100 "docker logs oms --tail 100 | grep -i nado"
```

### Symbol Not Receiving Updates

1. Check symbol state: `GET /nado/symbol/{symbol}`
2. If INACTIVE or RETRYING, wait for retry or manual trigger: `POST /nado/force-discovery`
3. Some symbols have low liquidity and may legitimately have no updates

### WebSocket Not Connecting

1. Verify OMS is running: `curl http://192.168.133.100:8099/health`
2. Check Nado endpoint: `GET /nado/health` shows WebSocket status
3. Check logs for errors: `docker logs oms | grep -i error`

---

## Implementation Files

- `nado_watchdog.py` - Core state management
- `nado_persistence.py` - State persistence
- `nado_discovery.py` - Symbol discovery
- `nado_websocket.py` - WebSocket connection manager
- `nado_config.yaml` - Configuration

---

## Migration from Legacy System

**Before:** Static list of symbols, one WebSocket per symbol
**After:** Dynamic symbol management, single shared WebSocket

**No breaking changes:**
- Existing `/book/nado/{symbol}` endpoint works unchanged
- Existing `/ws` WebSocket endpoint works unchanged
- Legacy code remains as fallback if watchdog unavailable

---

## See Also

- [WebSocket Architecture](WEBSOCKET_ARCHITECTURE.md) - Overall WebSocket design
- [API Documentation](V4_WEB_DEVELOPER_GUIDE.md) - General API reference
