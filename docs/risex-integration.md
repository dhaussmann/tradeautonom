# RISEx (rise.trade) Integration

Complete integration of the RISEx perpetual DEX into the TradeAutonom trading system. RISEx is a fully onchain orderbook exchange running on RISE Chain (chain ID 4153).

---

## 1. Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  Frontend (SettingsView.vue)                                 │
│  └─ RISEx section: wallet address + signer key input        │
│     → saved to D1 via Cloudflare Worker (MANAGED_KEYS)      │
├─────────────────────────────────────────────────────────────┤
│  Cloudflare Worker (secrets.ts)                              │
│  └─ MANAGED_KEYS includes risex_account_address,            │
│     risex_signer_key → injected to container via            │
│     /internal/apply-keys on auth/status                      │
├─────────────────────────────────────────────────────────────┤
│  Backend Container (Python)                                  │
│  ├─ config.py: risex_account_address, risex_signer_key,     │
│  │             risex_env (pydantic-settings)                 │
│  ├─ server.py: registers RisexClient in _exchange_clients    │
│  │             on init + reinit (key re-injection)           │
│  ├─ risex_client.py: full ExchangeClient + AsyncExchange-   │
│  │                    Client implementation (1370 lines)     │
│  └─ data_layer.py: WS orderbook feed + REST position poll   │
└─────────────────────────────────────────────────────────────┘
         │                    │
         ▼                    ▼
   REST API               WebSocket
   api.rise.trade         ws.rise.trade/ws
```

---

## 2. Files Changed

| File | Change | Lines |
|------|--------|-------|
| `app/risex_client.py` | **NEW** — Full exchange client | ~1370 lines |
| `app/config.py` | Added 3 settings | 60–63 |
| `app/server.py` | Import + registration in init/reinit | 28, 273–281, 2796–2802 |
| `app/data_layer.py` | WS orderbook handler + position routing | ~120 lines added |
| `deploy/cloudflare/src/lib/secrets.ts` | Added 2 managed keys | 20–21 |
| `frontend/src/views/SettingsView.vue` | RISEx settings section + help text | ~25 lines added |

---

## 3. Authentication Model

RISEx uses **EIP-712 permit signing** for all state-changing operations. This is fundamentally different from API key auth (Extended, GRVT) or JWT auth (Variational).

### Flow:
1. User registers a **signer key** (private key) via the RISEx web UI at `app.rise.trade`
2. The signer key is stored in the bot's config as `risex_signer_key`
3. For every trade/cancel/leverage call, the bot:
   - Fetches current **nonce state** from `/v1/nonce-state/{account}`
   - Encodes the action data into a keccak256 hash
   - Signs an **EIP-712 VerifyWitness** message with the signer key
   - Sends the permit (base64 signature + nonce + deadline) alongside the API request

### Nonce Management (Bitmap-based):
- Each account has a `nonce_anchor` (epoch) and a `current_bitmap_index` (0–207)
- Each permit consumes one bitmap index
- When `bitmap_index > 207`, the anchor is bumped and index resets to 0
- This prevents replay attacks without requiring sequential nonces

### EIP-712 Types:
```
VerifyWitness {
    account: address      // user's wallet address
    target: address       // router contract address
    hash: bytes32         // keccak256 of encoded action data
    nonceAnchor: uint48   // bitmap nonce epoch
    nonceBitmap: uint8    // bit index within epoch
    deadline: uint32      // permit expiry (unix seconds)
}
```

### Signature Format:
- Signed via `eth_account.Account.sign_message()` using `encode_typed_data()`
- V value fixed (0/1 → 27/28) via `_fix_signature_v()`
- Output is **base64-encoded** (not hex) — this is specific to RISEx

---

## 4. Order Encoding

Orders use a compressed **88-bit integer** format before being hashed.

### Bit Layout:
```
Bit [87:70]  marketId       (16 bits)
Bit [69:38]  sizeSteps      (32 bits)
Bit [37:14]  priceTicks     (24 bits)
Bit [13:6]   orderFlags     (8 bits: side, post_only, reduce_only, stp, type, tif)
Bit [5:1]    headerVersion  (5 bits, always 1)
Bit [0]      reserved       (1 bit, always 0)
```

### Price/Size Representation:
- **price_ticks** = `price / step_price` (integer) — e.g. BTC step_price=0.1, so $75000 → 750000 ticks
- **size_steps** = `size / step_size` (integer) — e.g. BTC step_size=0.000001, so 0.01 BTC → 10000 steps

### Action Hashing:
```python
hash = keccak256(abi.encode(
    ACTION_PLACE_ORDER_HASH,   # keccak256("RISE_PERPS_PLACE_ORDER_V1")
    headerFlags,               # uint8
    orderData,                 # uint256 (88-bit compressed)
    builderID,                 # uint16
    clientOrderID,             # uint64
    ttlUnits,                  # uint16
))
```

Cancel uses a different encoding:
```python
hash = keccak256(abi.encode(
    ACTION_CANCEL_ORDER_HASH,
    uint256(marketID),
    uint256(restingOrderID)    # from /v1/orders/open response
))
```

---

## 5. REST API Endpoints Used

### Public (no auth):
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/system/config` | GET | Chain ID, contract addresses |
| `/v1/auth/eip712-domain` | GET | EIP-712 domain for signing |
| `/v1/markets` | GET | All markets + config + live funding rates |
| `/v1/orderbook?market_id=N&limit=N` | GET | Orderbook (decimal strings) |
| `/v1/positions?account=0x...` | GET | Open positions |
| `/v1/orders?account=0x...` | GET | Order history (for fill checking) |
| `/v1/orders/open?account=0x...` | GET | Open orders (needed for cancel) |
| `/v1/nonce-state/{account}` | GET | Bitmap nonce state |
| `/v1/auth/session-key-status` | GET | Signer registration check |

### Authenticated (permit required):
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/v1/orders/place` | POST | Place order (maker or taker) |
| `/v1/orders/cancel` | POST | Cancel single order |
| `/v1/orders/cancel-all` | POST | Cancel all open orders |
| `/v1/account/leverage` | POST | Set leverage |

### Cancel Quirk:
Cancelling an order requires a `resting_order_id` (not the `order_id` returned by place). The client must first query `/v1/orders/open` to find the matching `resting_order_id`, then sign a cancel permit with that ID.

---

## 6. WebSocket Integration

### Orderbook Feed (DataLayer):
- **URL**: `wss://ws.rise.trade/ws`
- **Subscribe**: `{"method":"subscribe","params":{"channel":"orderbook","market_ids":[1]}}`
- **First message**: Full snapshot with `bids`/`asks` arrays (`{price, quantity, order_count}`)
- **Subsequent**: Incremental deltas with `"type":"update"` — upsert where qty>0, remove where qty=0
- **Heartbeat**: `{"op":"ping"}` every 15 seconds
- **Data format**: Plain decimal strings (e.g. `"75960.9"`, `"0.000263"`) — **NOT x18**

### Fills Feed (RisexClient):
- Same WS URL but requires **auth handshake** first
- Auth uses EIP-712 `Register` typed data with message `"sign in with RISEx"`
- Subscribe to `"fills"` channel with `account` and `market_ids`
- Delivers real-time fill notifications for order monitoring

### Position Feed:
- **No WS available** — uses REST polling fallback via `_run_pos_rest_fallback()` (every 2s)

---

## 7. Markets (Mainnet, as of April 2025)

| Market ID | Symbol | step_price | step_size | min_order_size | max_leverage |
|-----------|--------|------------|-----------|----------------|--------------|
| 1 | BTC/USDC | 0.1 | 0.000001 | 0.00015 | 25x |
| 2 | ETH/USDC | 0.01 | 0.001 | 0.005 | 25x |
| 3 | BNB/USDC | 0.01 | 0.001 | — | 25x |
| 4 | SOL/USDC | 0.001 | 0.01 | — | 25x |
| 5 | HYPE/USDC | — | — | — | — |
| 6 | XRP/USDC | — | — | — | — |
| 7 | TAO/USDC | — | — | — | — |
| 8 | ZEC/USDC | — | — | — | — |

**Note**: Some markets may be in `post_only` mode (no IOC/taker orders accepted). Check `m.post_only` flag from `/v1/markets` before attempting taker trades.

---

## 8. Client Methods Implemented

### ExchangeClient (sync):
- `name` → `"risex"`
- `fetch_order_book(symbol, limit)` → normalised `{bids, asks}`
- `fetch_markets()` → list of market dicts
- `get_min_order_size(symbol)` → `Decimal`
- `get_tick_size(symbol)` → `Decimal`
- `get_qty_step(symbol)` → `Decimal`
- `create_aggressive_limit_order(symbol, side, amount, ...)` → IOC order (sync)
- `check_order_fill(order_id)` → `{filled, status, traded_qty}`
- `fetch_positions(symbols)` → normalised positions
- `get_account_summary()` → `{balance}`

### AsyncExchangeClient (async):
- `async_fetch_order_book(symbol, limit)`
- `async_fetch_markets()`
- `async_get_min_order_size(symbol)`
- `async_get_tick_size(symbol)`
- `async_create_post_only_order(symbol, side, amount, price, reduce_only=False)`
- `async_create_ioc_order(symbol, side, amount, price, reduce_only=False)`
- `async_cancel_order(order_id)` → `bool`
- `async_cancel_all_orders(symbol=None)` → `bool`
- `async_check_order_fill(order_id)` → fill status dict
- `async_fetch_positions(symbols)`
- `async_fetch_funding_rate(symbol)` → funding rate dict
- `async_set_leverage(symbol, leverage)`
- `async_subscribe_fills(symbol, callback)` — WS with auth
- `async_subscribe_funding_rate(symbol, callback)` — polling (60s)

### Extra helpers:
- `async_get_balance()` → cross-margin balance
- `async_get_funding_payments(limit)` → funding payment history
- `async_get_trade_history(symbol, limit)` → fill/trade history
- `async_get_open_orders(symbol)` → open orders list
- `is_signer_registered()` → checks on-chain signer status
- `verify_signer()` → signer registration status dict

---

## 9. Configuration

### Environment Variables / `.env`:
```env
RISEX_ACCOUNT_ADDRESS=0x...    # Your RISEx wallet address
RISEX_SIGNER_KEY=0x...         # Pre-registered signer private key
RISEX_ENV=mainnet              # "mainnet" or "testnet"
```

### pydantic-settings keys (`app/config.py`):
```python
risex_account_address: str = ""
risex_signer_key: str = ""
risex_env: str = "mainnet"
```

### Cloudflare Worker managed keys (`secrets.ts`):
```typescript
"risex_account_address",
"risex_signer_key",
```

---

## 10. State Machine Compatibility

The RISEx client is **fully compatible** with the FundingArbEngine / state_machine.py:

| Required Method | Status | Notes |
|----------------|--------|-------|
| `async_create_post_only_order(reduce_only=)` | ✅ | GTC maker order |
| `async_create_ioc_order(reduce_only=)` | ✅ | IOC taker order |
| `async_cancel_order(order_id)` | ✅ | Looks up resting_order_id automatically |
| `async_check_order_fill(order_id)` | ✅ | Queries order history |
| `async_get_tick_size(symbol)` | ✅ | From cached step_price |
| `async_get_min_order_size(symbol)` | ✅ | From cached min_order_size |
| `async_fetch_positions(symbols)` | ✅ | REST query |
| `async_fetch_order_book(symbol, limit)` | ✅ | REST query |
| `async_cancel_all_orders()` | ✅ | Optional but implemented |
| `async_subscribe_fills(symbol, callback)` | ✅ | WS with auth |
| `async_subscribe_funding_rate(symbol, callback)` | ✅ | Polling (60s) |

---

## 11. Key Differences from Other Exchanges

| Aspect | RISEx | Nado | Extended | GRVT |
|--------|-------|------|----------|------|
| **Auth** | EIP-712 permit per call | EIP-712 order signing | API key + stark key | API key + cookie |
| **Nonce** | Bitmap (anchor+index) | Sequential | N/A | N/A |
| **Prices** | Decimal strings | x18 integers | Decimal strings | Decimal strings |
| **Order format** | 88-bit compressed | EIP-712 typed struct | REST JSON | REST JSON |
| **Cancel** | Needs resting_order_id lookup | By digest | By order ID | By order ID |
| **Signature** | Base64 | Hex (0x...) | N/A | N/A |
| **Position WS** | None (REST poll) | position_change stream | Account stream | v1.position stream |
| **Orderbook WS** | Snapshot + delta | Incremental x18 | Snapshot + delta | Full snapshots |
| **Chain** | RISE (4153) | Nado (57073) | StarkNet | — |

---

## 12. SDK Reference

The implementation was reverse-engineered from the **unofficial TypeScript SDK**:
- Repository: `github.com/SmoothBot/risex-ts`
- Key files ported:
  - `src/signing/encoder.ts` → order/cancel encoding
  - `src/signing/permit.ts` → VerifyWitness signing + base64
  - `src/signing/signer.ts` → signature V fix
  - `src/clients/ExchangeClient.ts` → REST methods
  - `src/clients/WebSocketClient.ts` → WS protocol
  - `src/utils/constants.ts` → action type hashes

Official docs: https://docs.risechain.com/docs/risex/api/

---

## 13. What's Open / Not Yet Done

| Item | Status | Notes |
|------|--------|-------|
| Backend client (`risex_client.py`) | ✅ Done | Full sync + async |
| Config (`config.py`) | ✅ Done | 3 settings |
| Server registration (`server.py`) | ✅ Done | init + reinit |
| Cloudflare secrets (`secrets.ts`) | ✅ Done | 2 managed keys |
| DataLayer WS orderbook | ✅ Done | Snapshot + delta |
| DataLayer position feed | ✅ Done | REST poll fallback |
| Frontend Settings UI | ✅ Done | 2 fields + help text |
| **Deploy to staging** | ❌ Not done | Code is local only |
| **Deploy to production** | ❌ Not done | Needs staging first |
| **Live import test** | ❌ Not done | Needs Docker (eth_account) |
| **End-to-end order test** | ❌ Not done | Needs signer key registered |
| **DNA bot support** | ❌ Not done | Not in dna_exchanges list |
| **CRC32 orderbook checksum** | ❌ Not done | WS sends checksums, not validated |
| **Frontend build** | ❌ Not done | `npm run build` needed |

---

## 14. Testing Done

| Test | Result |
|------|--------|
| Python syntax check (`ast.parse`) | ✅ Pass |
| `curl /v1/markets` | ✅ Response matches client parsing |
| `curl /v1/orderbook?market_id=1` | ✅ `data.bids[].price/quantity` format confirmed |
| `curl /v1/system/config` | ✅ Chain ID 4153, contract addresses |
| `curl /v1/auth/eip712-domain` | ✅ Domain name="RISEx", version="1" |
| WS orderbook subscription | ✅ Snapshot + delta messages confirmed |
| Full import test (Docker) | ❌ Not yet (needs eth_account) |
| Order placement test | ❌ Not yet (needs registered signer) |

---

## 15. Deployment Checklist

```bash
# 1. Verify no bots are running
curl -s http://192.168.133.100:8005/fn/bots | jq '.[] | .status'

# 2. Deploy code to staging (v3 shared mount)
./deploy/v3/manage.sh deploy-code

# 3. Test import on staging container
docker exec tradeautonom-v3 python -c "from app.risex_client import RisexClient; c = RisexClient(); print(c.fetch_markets())"

# 4. Build frontend
cd frontend && npm run build

# 5. Deploy Cloudflare Worker (picks up secrets.ts + frontend/dist)
./deploy/cloudflare/deploy.sh

# 6. Verify settings UI shows RISEx section
# 7. Enter keys via Settings → Save
# 8. Verify client registration in container logs
```
