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
    min_order_book_depth_usd: float = 1000.0

    # Arbitrage — Spread-Range strategy (PAXG always >= XAU)
    # Spread = PAXG_mid - XAU_mid (always >= 0)
    # Entry when spread <= low threshold → Long PAXG + Short XAU
    # Exit  when spread >= high threshold → close both
    arb_spread_entry_low: float = 2.0   # enter when spread narrows to this
    arb_spread_exit_high: float = 8.0   # exit when spread widens to this
    arb_max_exec_spread: float = 5.0    # safety: max bid-ask cost to execute
    arb_quantity: float = 1.0
    arb_xau_instrument: str = "XAU_USDT_Perp"
    arb_paxg_instrument: str = "PAXG_USDT_Perp"
    arb_liquidity_multiplier: float = 2.0
    arb_chunk_size: float = 1.0         # split large orders into chunks of this size
    arb_chunk_delay_ms: int = 500       # ms to wait between chunks (book replenishment)
    arb_simulation_mode: bool = False   # True = paper-trade (no real orders)

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
