from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # GRVT credentials
    grvt_api_key: str = ""
    grvt_private_key: str = ""
    grvt_trading_account_id: str = ""
    grvt_env: str = "testnet"

    # App server
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    # Trading safety
    default_slippage_pct: float = 0.5
    max_slippage_pct: float = 2.0
    min_order_book_depth_usd: float = 1.0

    # Arbitrage — Cross-exchange spread-convergence strategy
    # Spread = abs(mid_b - mid_a)  (always >= 0)
    # Entry when spread_abs >= entry_low  → spread large enough, Long cheap / Short expensive
    # Exit  when spread_abs <= exit_high  → spread converged, take profit
    arb_spread_entry_low: float = 0.08   # enter when spread_abs >= this (min spread to enter)
    arb_spread_exit_high: float = 0.02   # exit when spread_abs <= this (spread converged — take profit)
    arb_max_exec_spread: float = 0.5    # safety: max bid-ask execution cost
    arb_quantity: float = 0.1
    arb_xau_instrument: str = "SOL-USD"       # instrument for leg A (e.g. Extended)
    arb_paxg_instrument: str = "SOL_USDT_Perp"  # instrument for leg B (e.g. GRVT)
    arb_leg_a_exchange: str = "extended"   # exchange for instrument_a (leg A)
    arb_leg_b_exchange: str = "grvt"       # exchange for instrument_b (leg B)
    arb_liquidity_multiplier: float = 2.0
    extended_api_base_url: str = "https://api.starknet.extended.exchange/api/v1"
    extended_api_key: str = ""
    extended_public_key: str = ""
    extended_private_key: str = ""
    extended_vault: int = 0
    arb_chunk_size: float = 1.0         # split large orders into chunks of this size
    arb_chunk_delay_ms: int = 500       # ms to wait between chunks (book replenishment)
    arb_simulation_mode: bool = False   # True = paper-trade (no real orders)
    arb_order_type: str = "aggressive_limit"  # "aggressive_limit" or "market"
    arb_limit_offset_ticks: int = 5     # ticks beyond best price for aggressive limit
    arb_min_profit: float = 0.005       # min profit margin in USD above break-even
    arb_fill_timeout_ms: int = 3000     # max ms to wait for fill confirmation
    arb_ws_enabled: bool = True          # use WebSocket feeds for orderbook data
    arb_ws_stale_ms: int = 5000          # max age (ms) before falling back to REST
    arb_auto_trade: bool = False         # enable auto-trading for the default job on startup
    arb_tick_interval_s: float = 2.0    # seconds between auto-trade ticks

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
