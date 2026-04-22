# Nado Watchdog System

Dynamic symbol management for Nado exchange with automatic discovery, health monitoring, and persistence.

## Overview

The Nado Watchdog System replaces the static symbol list with a dynamic system that:

1. **Monitors symbol health** - Tracks updates and detects inactive markets
2. **Auto-discovers new symbols** - Daily check for new PERP markets
3. **Manages retries** - Exponential backoff retry for suspect symbols
4. **Persists state** - Saves state to disk, only on changes
5. **Provides APIs** - REST endpoints for monitoring symbol status

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    NADO WATCHDOG SYSTEM                      │
├─────────────────────────────────────────────────────────────┤
│  nado_watchdog.py    - Core state management                 │
│  nado_persistence.py - State persistence (disk)              │
│  nado_discovery.py   - Daily discovery of new symbols        │
│  nado_websocket.py   - Dynamic WebSocket connection          │
│  nado_config.yaml    - Configuration & default symbols       │
└─────────────────────────────────────────────────────────────┘
```

## Symbol States

- **ACTIVE** - Receiving regular updates (healthy)
- **SUSPECT** - No updates for 2 minutes, entering retry loop
- **RETRYING** - In retry loop with exponential backoff
- **INACTIVE** - Failed all 10 retry attempts
- **CANDIDATE** - New symbol from discovery, pending test
- **TESTING** - Currently being tested (30 seconds)

## State Transitions

```
ACTIVE → SUSPECT (no updates for 2 min)
  ↑         ↓
  └──── RETRYING (exponential backoff: 10s, 20s, 40s... max 2h)
            ↓ (after 10 failed retries)
         INACTIVE
            ↓ (daily reactivation check)
         CANDIDATE → TESTING → ACTIVE (if updates received)
```

## Configuration

### File: `nado_config.yaml`

```yaml
default_active_symbols:
  - BTC-PERP
  - ETH-PERP
  - SOL-PERP
  # ... 43 symbols total

watchdog:
  suspect_threshold_seconds: 120      # 2 minutes
  retry_max_attempts: 10
  retry_base_delay_seconds: 10
  retry_max_delay_seconds: 7200       # 2 hours
  reactivation_check_interval_hours: 24
  state_persistence_path: "/app/data/nado_watchdog_state.json"

discovery:
  interval_hours: 24
  test_duration_seconds: 30
  nado_api_url: "https://gateway.prod.nado.xyz"

websocket:
  endpoint: "wss://gateway.prod.nado.xyz/v1/subscribe"
  subscription_delay_ms: 50
```

## API Endpoints

### GET /nado/symbols
Returns status of all Nado symbols grouped by state.

```json
{
  "timestamp": "2026-04-22T14:30:00Z",
  "summary": {
    "total": 55,
    "active": 43,
    "inactive": 12
  },
  "symbols": {
    "active": [{"symbol": "BTC-PERP", "product_id": 2, "update_count": 15234}],
    "inactive": [{"symbol": "ADA-PERP", "product_id": 60}]
  }
}
```

### GET /nado/symbol/{symbol}
Returns detailed information about a specific symbol.

```json
{
  "symbol": "BTC-PERP",
  "product_id": 2,
  "state": "active",
  "update_count": 15234,
  "seconds_since_update": 2,
  "book": {
    "bid_levels": 45,
    "ask_levels": 38,
    "connected": true
  }
}
```

### POST /nado/force-discovery
Manually triggers symbol discovery and testing.

```json
{
  "success": true,
  "result": {
    "discovered": 2,
    "promoted": 1,
    "new_symbols": ["NEWSYM-PERP"]
  }
}
```

### GET /nado/health
Returns watchdog system health status.

```json
{
  "available": true,
  "statistics": {"total": 55, "active": 43},
  "websocket": {"connected": true, "subscribed_symbols": 43}
}
```

## Startup Flow

1. Load configuration from `nado_config.yaml`
2. Initialize watchdog with settings
3. Try to load persisted state from disk
4. If no state exists, use default 43 symbols
5. Start WebSocket manager with dynamic subscriptions
6. Start auto-persistence (saves on state changes)
7. Schedule initial discovery (runs after 30 seconds)
8. Start scheduled discovery (runs every 24 hours)

## Persistence

State is persisted to `/app/data/nado_watchdog_state.json`:

```json
{
  "version": "1.0",
  "last_saved": "2026-04-22T14:30:00Z",
  "symbols": {
    "BTC-PERP": {
      "product_id": 2,
      "state": "active",
      "last_update": "2026-04-22T14:29:58Z",
      "update_count": 15234
    }
  }
}
```

**Persistence strategy:**
- Only saves when state changes (not on timer)
- Immediate save on significant transitions (active→inactive, recovery, etc.)
- Background auto-save every 60 seconds if there are changes
- Atomic write (temp file + rename)

## Retry Schedule

Exponential backoff with max 2 hours:

| Attempt | Delay | Cumulative |
|---------|-------|------------|
| 1 | 10s | 10s |
| 2 | 20s | 30s |
| 3 | 40s | 70s |
| 4 | 80s | 150s (2.5min) |
| 5 | 160s | 310s (5min) |
| 6 | 320s | 630s (10min) |
| 7 | 640s | 1270s (21min) |
| 8 | 1280s | 2550s (42min) |
| 9 | 2560s | 5110s (85min) |
| 10 | 7200s (2h) | 12310s (3.4h) |

## Logs

```
INFO: Nado watchdog initialized with 43 active symbols
INFO: BTC-PERP: state=active, updates=5234
WARNING: LINK-PERP: No updates for 120s, marking SUSPECT (attempt 1/10)
INFO: LINK-PERP: Received data on retry 2, marking ACTIVE
WARNING: ADA-PERP: Inactive after 10 retries, marking INACTIVE
INFO: Nado discovery: Found 2 new symbols
INFO: Nado NEWSYM1-PERP: Testing for 30s...
INFO: Nado NEWSYM1-PERP: Promoted to ACTIVE after 45 updates
```

## Testing

Run the test script to verify functionality:

```bash
cd /Users/dhaussmann/Projects/tradeautonom/deploy/monitor
python3 test_nado_incremental.py
```

## Integration with OMS

The watchdog integrates seamlessly with the existing OMS:

- Uses same `_books` cache for order book data
- Compatible with existing `/book/nado/{symbol}` endpoint
- No breaking changes to existing functionality
- Falls back to legacy implementation if watchdog modules unavailable

## Files

- `nado_watchdog.py` - Core state machine
- `nado_persistence.py` - Disk persistence
- `nado_discovery.py` - Symbol discovery
- `nado_websocket.py` - WebSocket manager
- `nado_config.yaml` - Configuration
- `test_nado_incremental.py` - Test script
- `test_nado_shared_ws.py` - WebSocket test script

## Deployment

The watchdog starts automatically when OMS starts. No manual intervention needed.

State survives restarts via persistence file.
