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
    arb_min_top_book_mult: float = 2.0  # best bid/ask must hold qty × mult; 0 = disabled
    extended_api_base_url: str = "https://api.starknet.extended.exchange/api/v1"
    extended_api_key: str = ""
    extended_public_key: str = ""
    extended_private_key: str = ""
    extended_vault: int = 0
    # NADO credentials
    nado_private_key: str = ""
    nado_subaccount_name: str = "default"
    nado_env: str = "mainnet"
    nado_linked_signer_key: str = ""    # bot's trading key (linked signer — auto-generated)
    nado_wallet_address: str = ""       # main wallet address (from MetaMask connect)
    # Variational credentials (wallet address extracted from JWT automatically)
    variational_jwt_token: str = ""
    variational_proxy_url: str = "https://proxy.defitool.de/api"  # CF Worker proxy
    arb_chunk_size: float = 1.0         # split large orders into chunks of this size
    arb_chunk_delay_ms: int = 500       # ms to wait between chunks (book replenishment)
    arb_simulation_mode: bool = False   # True = paper-trade (no real orders)
    arb_order_type: str = "aggressive_limit"  # "aggressive_limit" or "market"
    arb_limit_offset_ticks: int = 5     # ticks beyond best price for aggressive limit (fallback)
    arb_vwap_buffer_ticks: int = 2      # extra ticks beyond VWAP worst-fill for latency protection
    arb_min_profit: float = 0.005       # min profit margin in USD above break-even
    arb_fill_timeout_ms: int = 3000     # max ms to wait for fill confirmation
    arb_strategy: str = "arbitrage"      # "arbitrage" or "delta_neutral"
    arb_max_spread_pct: float = 0.01     # delta-neutral: max spread in % of mid-price for entry/exit
    arb_funding_rate_bias: str | None = None  # stub for future funding-rate API integration
    arb_signal_confirmations: int = 3    # consecutive ticks confirming signal before executing
    arb_ws_enabled: bool = True          # use WebSocket feeds for orderbook data
    arb_ws_stale_ms: int = 5000          # max age (ms) before falling back to REST
    arb_auto_trade: bool = False         # enable auto-trading for the default job on startup
    arb_tick_interval_s: float = 2.0    # seconds between auto-trade ticks

    # ── Funding-Arb Maker-Taker Engine (new) ─────────────────────────
    # Direction: user-defined per job (which DEX holds long vs short)
    fn_enabled: bool = True                  # set False to skip legacy single-job engine startup
    fn_long_exchange: str = "extended"        # exchange holding the LONG position
    fn_short_exchange: str = "grvt"           # exchange holding the SHORT position
    fn_maker_exchange: str = "extended"       # exchange used as maker (post-only) side
    fn_instrument_a: str = "SOL-USD"          # instrument on long exchange
    fn_instrument_b: str = "SOL_USDT_Perp"   # instrument on short exchange
    fn_quantity: float = 0.1                  # total position size per entry

    # Maker order execution
    fn_maker_timeout_ms: int = 10000          # ms to wait for maker fill before repricing
    fn_maker_reprice_ticks: int = 3           # ticks to chase per reprice round
    fn_maker_max_chase_rounds: int = 5        # max reprice attempts before giving up
    fn_maker_offset_ticks: int = 0            # initial offset from best bid/ask for maker order

    # TWAP chunking
    fn_twap_num_chunks: int = 10              # split total qty into N chunks
    fn_twap_interval_s: float = 10.0          # seconds between TWAP chunks

    # Risk management
    fn_delta_max_usd: float = 50.0            # max absolute delta imbalance (USD) before rebalance
    fn_circuit_breaker_loss_usd: float = 500.0  # cumulative loss threshold to halt all trading
    fn_min_spread_pct: float = -0.5            # min spread % (safety floor — blocks extreme negative outliers)
    fn_max_spread_pct: float = 0.05           # max spread % of mid-price to allow entry
    fn_max_chunk_spread_usd: float = 1.0      # max absolute cross-exchange spread (USD) per chunk

    # Funding rate monitoring
    fn_funding_poll_interval_s: float = 60.0  # seconds between funding rate display refreshes

    # Run duration (total = h*60 + m minutes; 0+0 = run indefinitely)
    fn_duration_h: int = 0                    # hours component of run duration
    fn_duration_m: int = 0                    # minutes component of run duration
    fn_auto_entry: bool = True                # auto-enter position on start()

    # Leverage (per exchange)
    fn_leverage_long: int = 5                 # leverage for the long-side exchange
    fn_leverage_short: int = 5                # leverage for the short-side exchange

    # Simulation
    fn_simulation_mode: bool = False          # True = paper-trade (no real orders)

    # ── Opt-in Optimierungen (alle Default off) ───────────────────
    fn_opt_depth_spread: bool = False         # Opt 1: VWAP statt BBO für Spread Guard
    fn_opt_max_slippage_bps: float = 10.0     # Opt 1+4: Max Slippage Budget (basis points)
    fn_opt_ohi_monitoring: bool = False       # Opt 2: OHI im Dashboard anzeigen
    fn_opt_min_ohi: float = 0.4              # Opt 2: Min OHI für Entry (0=disabled)
    fn_opt_funding_history: bool = False      # Opt 3: V4 API historischer Funding Filter
    fn_opt_funding_api_url: str = "https://api.fundingrate.de"  # Opt 3: V4 API Base URL
    fn_opt_min_funding_consistency: float = 0.3  # Opt 3: Min Consistency Score (0-1)
    fn_opt_dynamic_sizing: bool = False       # Opt 4: Liquiditätsbasiertes Sizing
    fn_opt_max_utilization: float = 0.80      # Opt 4: Max Kapitalnutzung (0-1)
    fn_opt_max_per_pair_ratio: float = 0.25   # Opt 4: Max Anteil pro Pair (0-1)
    fn_opt_shared_monitor_url: str = ""       # Opt 5: OMS URL (leer = disabled)
    fn_opt_taker_drift_guard: bool = False    # Opt 6: Taker-Drift-Guard während Maker-Wait
    fn_opt_max_taker_drift_bps: float = 3.0  # Opt 6: Max erlaubter Taker-Drift (basis points)

    # ── DNA Bot (Delta-Neutral Arbitrage) ──────────────────────
    dna_oms_url: str = "http://192.168.133.100:8099"
    dna_position_size_usd: float = 1000.0
    dna_max_positions: int = 3
    dna_min_profit_bps: float = 0.0        # 0 = use OMS fee thresholds
    dna_exchanges: str = "extended,grvt,nado"
    dna_slippage_tolerance_pct: float = 0.5
    dna_size_tolerance_pct: float = 5.0
    dna_simulation: bool = False           # default to live trading
    dna_excluded_tokens: str = "SUI"       # comma-separated tokens to skip
    dna_auto_exclude_open_positions: bool = True  # auto-exclude tokens with open exchange positions

    # ── History ingest (push snapshots to Cloudflare D1) ───────
    history_ingest_url: str = ""              # e.g. https://tradeautonom.workers.dev/api/history/ingest
    history_ingest_token: str = ""            # Bearer token for auth
    history_ingest_interval_s: int = 300      # seconds between pushes (default 5 min)

    # ── Execution log (per-chunk AI training data) ─────────────
    execution_log_enabled: bool = True        # Enable per-chunk execution logging to D1

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}
