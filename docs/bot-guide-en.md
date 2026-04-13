# TradeAutonom — Bot Guide for End Users

## What is Funding Arbitrage?

Perpetual futures on decentralized exchanges (DEXs) use a **funding rate mechanism** to anchor the futures price to the spot price. When the futures price is above spot, long positions pay short positions (positive funding rate) — and vice versa.

**Funding arbitrage** exploits these payments by simultaneously opening a long position on one exchange and a short position on another. The positions are **delta-neutral** — price movements cancel each other out. The profit comes from the funding rate difference between the two exchanges.

### Example

| Exchange | Position | Funding Rate (p.a.) |
|----------|----------|---------------------|
| Extended | LONG ETH | -5% (receives 5%) |
| GRVT | SHORT ETH | +3% (pays 3%) |

**Net yield**: 5% - 3% = **2% p.a.** on deployed capital — with minimal price risk.

---

## How the Bot Works

### Lifecycle

```
IDLE → [Start] → ENTERING → HOLDING → [Stop/Timer] → EXITING → IDLE
```

1. **IDLE**: No active trade. Bot awaits a start command.
2. **ENTERING**: Bot opens the delta-neutral position (long + short simultaneously).
3. **HOLDING**: Position is open. Bot collects funding earnings. Dashboard shows PnL, funding, and positions in real time.
4. **EXITING**: Bot closes both positions simultaneously.
5. **IDLE**: Back to initial state.

### Starting the Bot

When starting, you configure:

| Parameter | Description | Example |
|-----------|-------------|---------|
| **Long Exchange** | Exchange for the long position | Extended |
| **Short Exchange** | Exchange for the short position | GRVT |
| **Instrument** | Which asset to trade | ETH-USD / ETH_USDT_Perp |
| **Quantity** | Position size (in tokens) | 20 ETH |
| **Leverage** | Leverage per exchange | 25x / 25x |
| **Duration** | How long the bot runs | 4h 0m |
| **TWAP Chunks** | How many sub-orders the position is split into | 10 |

### Timer

- The timer starts **after** a successful entry.
- When the timer expires, an automatic exit is triggered.
- You can adjust the timer at any time or set it to indefinite (0h 0m).
- The timer survives container restarts — on a restart, the remaining countdown resumes.

---

## Order Management

### Maker-Taker TWAP Strategy

The bot uses a **Maker-Taker strategy** with **TWAP** (Time-Weighted Average Price) to open and close positions as cost-efficiently as possible.

#### Why Maker-Taker?

- **Maker orders** (Post-Only Limit) pay lower or even negative fees.
- **Taker orders** (IOC Market) are more expensive but guarantee immediate execution.
- By combining both, one leg is filled cheaply via maker, and the other is instantly hedged via taker — keeping the position delta-neutral at all times.

#### Flow Per Chunk

```
1. Read orderbook → determine best price
2. Place Maker Post-Only order (e.g. SELL on Extended)
3. Wait for fill (with chase logic on price movement)
4. Once Maker filled → Taker IOC Hedge (e.g. BUY on GRVT)
5. Verify positions → on imbalance: Repair IOC
```

#### Chase Logic (Maker)

If the market price moves and the maker order isn't filled:

1. The order is automatically cancelled.
2. A fresh orderbook is fetched.
3. The order is re-placed at the new best price.
4. This repeats until the order is filled or the bot is stopped.

#### Taker Hedge

After each maker fill, an **IOC (Immediate-Or-Cancel)** order is immediately placed on the opposite exchange:

- Uses the current orderbook price plus a buffer (50 ticks).
- Executes immediately or is cancelled — no waiting.
- Guarantees the position remains delta-neutral.

#### TWAP (Split into Chunks)

The total quantity is split into multiple smaller orders:

- **Advantage**: Less market impact, better average prices.
- **Interval**: A configurable pause is inserted between chunks (default: 10s).
- Example: 20 ETH in 10 chunks = 2 ETH per chunk, every 10 seconds.

---

## Risk Management

### Spread Guard

Before each chunk, the bot checks the **cross-exchange spread** (price difference between the two exchanges):

- If the spread is too wide (configurable: e.g. max $1 or 0.05%), the bot waits until the spread normalizes.
- Prevents entries at unfavorable prices.

### Pre-Trade Checks

Before every order, the following are automatically verified:

| Check | Description |
|-------|-------------|
| **Circuit Breaker** | If cumulative losses exceed a threshold (e.g. $500), trading is halted. |
| **Min Order Size** | The order size must meet the exchange's minimum. |
| **Orderbook Sync** | The orderbook must be synchronized via WebSocket. |
| **Liquidity Check** | Sufficient liquidity in the top 10 orderbook levels. |

### Position Verification & Repair

After each chunk, the bot verifies actual positions on both exchanges:

1. **Immediate check** (0.5s after chunk): Query real position sizes.
2. **Delayed check** (3s after chunk): Re-check to capture late fills.
3. **Repair mechanism**: If an imbalance is detected (maker filled but taker not fully), a repair IOC order is automatically placed on the taker side.

### Circuit Breaker

- Monitors cumulative PnL across all trades.
- If losses exceed the configured threshold, trading is automatically halted.
- Manual reset available.

### Delta Monitoring

- Net delta (long quantity + short quantity) should always be near 0.
- Displayed on the dashboard.
- Larger deviations indicate a problem (e.g. failed taker hedge).

---

## Emergency Actions

### Stop (Graceful)

- Cancels the timer.
- Performs a full exit (Maker-Taker TWAP like entry, but in reverse direction).
- Positions are closed cleanly.

### Kill (Emergency)

When something goes wrong:

- **Immediately** cancels all running operations (TWAP loop, timer).
- Cancels all open orders on all exchanges.
- Resets state to IDLE.
- **Positions remain open** — must be closed manually on the exchanges.

### Reset

- Resets internal state to IDLE.
- For cases where positions have already been closed manually on the exchanges.
- No trading — state reset only.

---

## Dashboard Metrics

| Metric | Description |
|--------|-------------|
| **Total PnL** | Total net profit/loss of all closed positions |
| **Point Factor** | Points per $100K trading volume (points efficiency) |
| **Active Bots** | Number of running / total bots |
| **Most Traded** | Top 3 traded tokens by volume |
| **Paid Fees** | Total trading fees paid |
| **Paid Funding** | Total funding payments received/paid |
| **Avg Hold Time** | Average hold duration of closed positions |
| **Delta Neutral Factor** | How successfully positions were closed delta-neutral (>100% = both legs in profit) |

---

## Tips

1. **Check funding rates**: Only start the bot when there's a significant funding rate difference between exchanges.
2. **Start small**: Test with small quantities before opening larger positions.
3. **Use the timer**: Always set a timer to ensure positions are automatically closed.
4. **Watch the spread**: A high spread at entry can consume the entire funding yield.
5. **Leverage**: Higher leverage = less margin required, but higher liquidation risk on extreme price moves.
