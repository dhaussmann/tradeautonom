"""FastAPI server — exposes endpoints for trade triggers and arbitrage."""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse

from app.arbitrage import ArbitrageEngine
from app.config import Settings
from app.engine import FundingArbEngine, EngineConfig
from app.bot_registry import BotRegistry
from app.crypto import encrypt_secrets, decrypt_secrets, create_auth_file, verify_password
from app.executor import TradeExecutor
from app.extended_client import ExtendedClient
from app.grvt_client import GrvtClient
from app.nado_client import NadoClient
from app.variational_client import VariationalClient
from app.job_manager import JobManager, ArbJob
from app.ws_feeds import OrderbookFeedManager
from app.journal_collector import JournalCollector
from app.schemas import (
    ArbAutoRequest,
    ArbCheckResponse,
    ArbConfigRequest,
    ArbExecutionResponse,
    ArbLegInfo,
    ArbStatusResponse,
    ArbTriggerRequest,
    DepthInfo,
    JobCreateRequest,
    JobConfigUpdateRequest,
    JobStatusResponse,
    SlippageInfo,
    SpreadInfo,
    SpreadRequest,
    SpreadResponse,
    TradeLogEntryResponse,
    TradeRequest,
    TradeResponse,
)

logger = logging.getLogger("tradeautonom.server")


def _spread_info(snapshot) -> SpreadInfo:
    """Convert an arbitrage SpreadSnapshot to a SpreadInfo response model."""
    return SpreadInfo(
        instrument_a=snapshot.instrument_a,
        instrument_b=snapshot.instrument_b,
        mid_price_a=snapshot.mid_price_a,
        mid_price_b=snapshot.mid_price_b,
        best_bid_a=getattr(snapshot, 'best_bid_a', 0.0),
        best_ask_a=getattr(snapshot, 'best_ask_a', 0.0),
        best_bid_b=getattr(snapshot, 'best_bid_b', 0.0),
        best_ask_b=getattr(snapshot, 'best_ask_b', 0.0),
        spread=snapshot.spread,
        spread_abs=snapshot.spread_abs,
        a_is_cheaper=snapshot.a_is_cheaper,
        exec_spread=getattr(snapshot, 'exec_spread', 0.0),
        slippage_cost=getattr(snapshot, 'slippage_cost', 0.0),
        break_even_spread=getattr(snapshot, 'break_even_spread', 0.0),
        spread_pct=getattr(snapshot, 'spread_pct', 0.0),
        data_source=getattr(snapshot, 'data_source', 'rest'),
    )

# Module-level singletons (populated in lifespan)
_settings: Settings | None = None
_client: GrvtClient | None = None
_extended_client: ExtendedClient | None = None
_executor: TradeExecutor | None = None
_arb_engine: ArbitrageEngine | None = None
_exchange_clients: dict = {}
_feed_manager: OrderbookFeedManager | None = None
_job_manager: JobManager | None = None
_fn_engine: FundingArbEngine | None = None
_bot_registry: BotRegistry | None = None
_journal_collector: JournalCollector | None = None
_vault_unlocked: bool = False  # True after user enters correct password

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_AUTH_FILE = _DATA_DIR / "auth.json"

_auto_trade_task: asyncio.Task | None = None
_history_ingest_task: asyncio.Task | None = None


async def _init_exchange_clients():
    """Initialize exchange clients and engines after vault unlock."""
    global _settings, _client, _extended_client, _executor, _arb_engine
    global _exchange_clients, _feed_manager, _job_manager, _fn_engine, _bot_registry
    global _vault_unlocked, _auto_trade_task

    _client = GrvtClient(_settings)
    _extended_client = ExtendedClient(
        base_url=_settings.extended_api_base_url,
        api_key=_settings.extended_api_key,
        public_key=_settings.extended_public_key,
        private_key=_settings.extended_private_key,
        vault=_settings.extended_vault,
    )
    _exchange_clients = {"grvt": _client, "extended": _extended_client}
    # NADO client (optional — needs private key or linked signer key)
    if _settings.nado_private_key or _settings.nado_linked_signer_key:
        _nado_client = NadoClient(
            private_key=_settings.nado_private_key,
            subaccount_name=_settings.nado_subaccount_name,
            env=_settings.nado_env,
            linked_signer_key=_settings.nado_linked_signer_key,
            wallet_address=_settings.nado_wallet_address,
        )
        _exchange_clients["nado"] = _nado_client
        logger.info("NADO client registered (env=%s)", _settings.nado_env)
    # Variational client (optional — only if JWT token is set)
    if _settings.variational_jwt_token:
        _variational_client = VariationalClient(
            jwt_token=_settings.variational_jwt_token,
            proxy_worker_url=_settings.variational_proxy_url,
        )
        _exchange_clients["variational"] = _variational_client
        logger.info("Variational client registered")
    _executor = TradeExecutor(_client, _settings)
    # Legacy single-engine (kept for backward compat with /arb/* endpoints)
    _arb_engine = ArbitrageEngine(_exchange_clients, _executor, _settings)
    _arb_engine.sync_position_from_exchange()
    # Start WebSocket orderbook feeds
    if _settings.arb_ws_enabled:
        _feed_manager = OrderbookFeedManager(
            grvt_env=_settings.grvt_env,
            stale_ms=_settings.arb_ws_stale_ms,
            nado_env=_settings.nado_env,
        )
        _feed_manager.start({
            _settings.arb_leg_a_exchange: _settings.arb_xau_instrument,
            _settings.arb_leg_b_exchange: _settings.arb_paxg_instrument,
        })
        _arb_engine.set_feed_manager(_feed_manager)
        logger.info("WebSocket feeds started for %s:%s + %s:%s",
                    _settings.arb_leg_a_exchange, _settings.arb_xau_instrument,
                    _settings.arb_leg_b_exchange, _settings.arb_paxg_instrument)
    # Job manager (multi-pair)
    _job_manager = JobManager(_exchange_clients, _executor, _settings, _feed_manager)
    # Restore persisted jobs from disk (survives container restarts)
    _job_manager.load_jobs()

    # Background auto-trade loop: runs tick_all() every arb_tick_interval_s seconds
    async def _auto_trade_loop():
        while True:
            await asyncio.sleep(_settings.arb_tick_interval_s)
            try:
                _job_manager.tick_all()
            except Exception as exc:
                logger.error("Auto-trade loop error: %s", exc, exc_info=True)

    _auto_trade_task = asyncio.create_task(_auto_trade_loop())

    # ── Funding-Arb engine (legacy single-bot — kept for backward compat) ──
    try:
        fn_config = EngineConfig.from_settings(_settings, job_id="default")
        _fn_engine = FundingArbEngine(config=fn_config, clients=_exchange_clients)
        await _fn_engine.start()
        logger.info("FundingArbEngine started: long=%s short=%s maker=%s",
                    fn_config.long_exchange, fn_config.short_exchange, fn_config.maker_exchange)
    except Exception as exc:
        logger.warning("FundingArbEngine failed to start (non-fatal): %s", exc)
        _fn_engine = None

    # ── Bot Registry (multi-bot v2) ────────────────────────────────
    try:
        _bot_registry = BotRegistry(clients=_exchange_clients, settings=_settings)
        await _bot_registry.start_all()
        logger.info("BotRegistry started: %s", _bot_registry.bot_ids)
    except Exception as exc:
        logger.warning("BotRegistry failed to start (non-fatal): %s", exc)
        _bot_registry = None

    # ── Journal Collector (order/fill/funding/points history) ──────
    global _journal_collector
    if _settings.history_ingest_url and _settings.history_ingest_token:
        journal_ingest_url = _settings.history_ingest_url.replace("/api/history/ingest", "/api/journal/ingest")
        _journal_collector = JournalCollector(
            exchange_clients=_exchange_clients,
            bot_registry=_bot_registry,
            ingest_url=journal_ingest_url,
            ingest_token=_settings.history_ingest_token,
            interval_s=_settings.history_ingest_interval_s,
        )
        _journal_collector.start()
        logger.info("JournalCollector started (url=%s)", journal_ingest_url)

    _vault_unlocked = True
    logger.info("Vault unlocked — exchanges=%s", list(_exchange_clients.keys()))


async def _shutdown_exchange_clients():
    """Shut down exchange clients and engines (for lock or app shutdown)."""
    global _client, _extended_client, _executor, _arb_engine
    global _exchange_clients, _feed_manager, _job_manager, _fn_engine, _bot_registry
    global _vault_unlocked, _auto_trade_task, _journal_collector

    _vault_unlocked = False
    if _journal_collector is not None:
        await _journal_collector.stop()
        _journal_collector = None
    if _auto_trade_task is not None:
        _auto_trade_task.cancel()
        _auto_trade_task = None
    if _bot_registry is not None:
        await _bot_registry.stop_all()
        _bot_registry = None
    if _fn_engine is not None:
        await _fn_engine.stop()
        _fn_engine = None
    if _feed_manager is not None:
        _feed_manager.stop()
        _feed_manager = None
    _job_manager = None
    _arb_engine = None
    _executor = None
    _exchange_clients = {}
    _client = None
    _extended_client = None
    logger.info("Exchange clients shut down (vault locked)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _settings
    _settings = Settings()
    # Check if auth is set up AND we have plaintext secrets (legacy migration)
    auth_file = _DATA_DIR / "auth.json"
    secrets_file_plain = _DATA_DIR / "secrets.json"
    if not auth_file.exists() and secrets_file_plain.exists():
        # Legacy mode: load plaintext secrets and init immediately
        _apply_secrets_to_settings(_load_secrets_plaintext())
        await _init_exchange_clients()
        logger.info("App started in LEGACY mode (no auth) — GRVT env=%s", _settings.grvt_env)
    elif not auth_file.exists():
        logger.info("App started in SETUP mode — waiting for password setup via /auth/setup")
    else:
        # Try auto-unlock from persisted vault session (survives uvicorn reload)
        session_pw = _load_vault_session()
        if session_pw:
            try:
                auth_data = json.loads(auth_file.read_text())
                if verify_password(session_pw, auth_data):
                    secrets = _load_secrets_encrypted(session_pw)
                    _apply_secrets_to_settings(secrets)
                    _current_password = session_pw
                    await _init_exchange_clients()
                    logger.info("App started — AUTO-UNLOCKED from vault session")
                else:
                    logger.info("Vault session password mismatch — starting LOCKED")
            except Exception as exc:
                logger.warning("Auto-unlock failed (%s) — starting LOCKED", exc)
        else:
            logger.info("App started in LOCKED mode — waiting for unlock via /auth/unlock")

    # Start history ingest background task if configured
    global _history_ingest_task
    if _settings and _settings.history_ingest_url:
        _history_ingest_task = asyncio.create_task(_history_ingest_loop())
        logger.info("History ingest task started (interval=%ds, url=%s)",
                    _settings.history_ingest_interval_s, _settings.history_ingest_url)

    yield

    if _history_ingest_task and not _history_ingest_task.done():
        _history_ingest_task.cancel()
    await _shutdown_exchange_clients()
    logger.info("App shutting down.")


app = FastAPI(
    title="TradeAutonom — GRVT Trade Executor",
    description=(
        "Execute market orders on GRVT with order-book depth checks, "
        "slippage validation, and XAU/PAXG arbitrage spread trading."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ------------------------------------------------------------------
# Health
# ------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "grvt_env": _settings.grvt_env if _settings else "not initialised"}


# ------------------------------------------------------------------
# Single market order
# ------------------------------------------------------------------

@app.post("/trade", response_model=TradeResponse)
async def execute_trade(req: TradeRequest):
    """Execute a single market order with full safety checks."""
    result = _executor.execute_market_order(
        symbol=req.symbol,
        side=req.side,
        quantity=Decimal(str(req.quantity)),
        expected_price=req.expected_price,
        slippage_pct=req.slippage_pct,
        min_depth_usd=req.min_depth_usd,
    )

    depth_info = None
    if result.depth:
        depth_info = DepthInfo(
            is_sufficient=result.depth.is_sufficient,
            available_depth_usd=result.depth.available_depth_usd,
            required_depth_usd=result.depth.required_depth_usd,
            best_price=result.depth.best_price,
            worst_fill_price=result.depth.worst_fill_price,
            levels_consumed=result.depth.levels_consumed,
        )

    slippage_info = None
    if result.slippage:
        slippage_info = SlippageInfo(
            is_acceptable=result.slippage.is_acceptable,
            expected_price=result.slippage.expected_price,
            estimated_fill_price=result.slippage.estimated_fill_price,
            slippage_pct=result.slippage.slippage_pct,
            max_allowed_pct=result.slippage.max_allowed_pct,
        )

    return TradeResponse(
        success=result.success,
        order_response=result.order_response,
        depth=depth_info,
        slippage=slippage_info,
        error=result.error,
    )


# ------------------------------------------------------------------
# Arbitrage — spread query
# ------------------------------------------------------------------

@app.post("/arb/spread", response_model=SpreadResponse)
async def get_spread(req: SpreadRequest = SpreadRequest()):
    """Query the current spread between the two arb instruments."""
    if req.instrument_a:
        _arb_engine.instrument_a = req.instrument_a
    if req.instrument_b:
        _arb_engine.instrument_b = req.instrument_b

    try:
        snapshot = _arb_engine.get_spread_snapshot()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=f"Orderbook not available: {exc}")
    check = _arb_engine.evaluate(snapshot)

    return SpreadResponse(
        snapshot=_spread_info(snapshot),
        recommended_action=check.action,
        reason=check.reason,
    )


# ------------------------------------------------------------------
# Arbitrage — evaluate only (no execution)
# ------------------------------------------------------------------

@app.get("/arb/check", response_model=ArbCheckResponse)
async def arb_check():
    """Evaluate arb opportunity without executing."""
    try:
        snapshot = _arb_engine.get_spread_snapshot()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=f"Orderbook not available: {exc}")
    check = _arb_engine.evaluate(snapshot)
    return ArbCheckResponse(
        action=check.action,
        snapshot=_spread_info(snapshot),
        reason=check.reason,
    )


# ------------------------------------------------------------------
# Arbitrage — trigger entry or exit
# ------------------------------------------------------------------

@app.post("/arb/trigger", response_model=ArbExecutionResponse)
async def arb_trigger(req: ArbTriggerRequest):
    """Trigger an arb ENTRY or EXIT with safety checks on both legs."""
    # Apply overrides if provided
    if req.spread_entry_low is not None:
        _arb_engine.spread_entry_low = req.spread_entry_low
    if req.spread_exit_high is not None:
        _arb_engine.spread_exit_high = req.spread_exit_high
    if req.quantity is not None:
        _arb_engine.quantity = Decimal(str(req.quantity))

    try:
        result = _arb_engine.execute_signal(
            req.action,
            min_depth_usd=req.min_depth_usd,
            slippage_pct=req.slippage_pct,
        )
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=f"Orderbook not available: {exc}")

    leg_a_info = None
    if result.leg_a:
        leg_a_info = ArbLegInfo(success=result.leg_a.success, error=result.leg_a.error)
    leg_b_info = None
    if result.leg_b:
        leg_b_info = ArbLegInfo(success=result.leg_b.success, error=result.leg_b.error)

    return ArbExecutionResponse(
        success=result.success,
        leg_a=leg_a_info,
        leg_b=leg_b_info,
        snapshot=_spread_info(result.snapshot),
        error=result.error,
    )


# ------------------------------------------------------------------
# Arbitrage — auto (check + execute in one call)
# ------------------------------------------------------------------

@app.post("/arb/auto", response_model=ArbExecutionResponse)
async def arb_auto(req: ArbAutoRequest = ArbAutoRequest()):
    """Check spread conditions and automatically execute if an arb signal is present.

    This combines /arb/check + /arb/trigger into a single call:
    - If spread <= entry threshold and no position → execute ENTRY
    - If spread >= exit threshold and has position → execute EXIT
    - Otherwise → return with error explaining why no action was taken
    """
    # Apply overrides
    if req.spread_entry_low is not None:
        _arb_engine.spread_entry_low = req.spread_entry_low
    if req.spread_exit_high is not None:
        _arb_engine.spread_exit_high = req.spread_exit_high
    if req.quantity is not None:
        _arb_engine.quantity = Decimal(str(req.quantity))

    # Evaluate
    try:
        snapshot = _arb_engine.get_spread_snapshot()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=f"Orderbook not available: {exc}")
    check = _arb_engine.evaluate(snapshot)

    if check.action == "NONE":
        return ArbExecutionResponse(
            success=False,
            leg_a=None,
            leg_b=None,
            snapshot=_spread_info(snapshot),
            error=f"No action: {check.reason}",
        )

    # Execute the signal
    if check.action == "ENTRY":
        result = _arb_engine.execute_entry(
            snapshot,
            min_depth_usd=req.min_depth_usd,
            slippage_pct=req.slippage_pct,
        )
    else:
        result = _arb_engine.execute_exit(
            snapshot,
            min_depth_usd=req.min_depth_usd,
            slippage_pct=req.slippage_pct,
        )

    leg_a_info = None
    if result.leg_a:
        leg_a_info = ArbLegInfo(success=result.leg_a.success, error=result.leg_a.error)
    leg_b_info = None
    if result.leg_b:
        leg_b_info = ArbLegInfo(success=result.leg_b.success, error=result.leg_b.error)

    return ArbExecutionResponse(
        success=result.success,
        leg_a=leg_a_info,
        leg_b=leg_b_info,
        snapshot=_spread_info(result.snapshot),
        error=result.error,
    )


# ------------------------------------------------------------------
# Arbitrage — status & config (for WebUI)
# ------------------------------------------------------------------

@app.get("/arb/status", response_model=ArbStatusResponse)
async def arb_status():
    """Return current arb engine configuration and position state."""
    pi = _arb_engine.position_info
    return ArbStatusResponse(
        has_position=pi["has_position"],
        spread_entry_low=_arb_engine.spread_entry_low,
        spread_exit_high=_arb_engine.spread_exit_high,
        max_exec_spread=_arb_engine.max_exec_spread,
        simulation_mode=_arb_engine.simulation_mode,
        quantity=float(_arb_engine.quantity),
        min_depth_usd=_settings.min_order_book_depth_usd,
        slippage_pct=_settings.default_slippage_pct,
        instrument_a=_arb_engine.instrument_a,
        instrument_b=_arb_engine.instrument_b,
        liquidity_multiplier=_arb_engine.liquidity_multiplier,
        min_top_book_mult=_arb_engine.min_top_book_mult,
        chunk_size=float(_arb_engine.chunk_size),
        chunk_delay_ms=_arb_engine.chunk_delay_ms,
        leg_a_exchange=_arb_engine.leg_a_exchange,
        leg_b_exchange=_arb_engine.leg_b_exchange,
        order_type=_arb_engine.order_type,
        limit_offset_ticks=_arb_engine.limit_offset_ticks,
        vwap_buffer_ticks=_arb_engine.vwap_buffer_ticks,
        min_profit=_arb_engine.min_profit,
        fill_timeout_ms=_arb_engine.fill_timeout_ms,
        ws_enabled=_arb_engine._ws_enabled,
        ws_stale_ms=_arb_engine._ws_stale_ms,
        signal_confirmations=_arb_engine.signal_confirmations,
        strategy=_arb_engine.strategy,
        max_spread_pct=_arb_engine.max_spread_pct,
        funding_rate_bias=_arb_engine.funding_rate_bias,
        long_sym=pi["long_sym"],
        short_sym=pi["short_sym"],
        entry_spread_actual=pi["entry_spread"],
    )


@app.post("/arb/config")
async def arb_config(req: ArbConfigRequest):
    """Update arb engine parameters at runtime."""
    if req.spread_entry_low is not None:
        _arb_engine.spread_entry_low = req.spread_entry_low
    if req.spread_exit_high is not None:
        _arb_engine.spread_exit_high = req.spread_exit_high
    if req.max_exec_spread is not None:
        _arb_engine.max_exec_spread = req.max_exec_spread
    if req.quantity is not None:
        _arb_engine.quantity = Decimal(str(req.quantity))
    if req.min_depth_usd is not None:
        _settings.min_order_book_depth_usd = req.min_depth_usd
    if req.slippage_pct is not None:
        _settings.default_slippage_pct = req.slippage_pct
    if req.liquidity_multiplier is not None:
        _arb_engine.liquidity_multiplier = req.liquidity_multiplier
    if hasattr(req, 'min_top_book_mult') and req.min_top_book_mult is not None:
        _arb_engine.min_top_book_mult = req.min_top_book_mult
    if req.chunk_size is not None:
        _arb_engine.chunk_size = Decimal(str(req.chunk_size))
    if req.chunk_delay_ms is not None:
        _arb_engine.chunk_delay_ms = req.chunk_delay_ms
    instruments_changed = False
    if req.instrument_a is not None:
        if req.instrument_a != _arb_engine.instrument_a:
            instruments_changed = True
        _arb_engine.instrument_a = req.instrument_a
        _arb_engine.xau_instrument = req.instrument_a
    if req.instrument_b is not None:
        if req.instrument_b != _arb_engine.instrument_b:
            instruments_changed = True
        _arb_engine.instrument_b = req.instrument_b
        _arb_engine.paxg_instrument = req.instrument_b
    if req.leg_a_exchange is not None:
        _arb_engine.leg_a_exchange = req.leg_a_exchange
    if req.leg_b_exchange is not None:
        _arb_engine.leg_b_exchange = req.leg_b_exchange
    if req.simulation_mode is not None:
        _arb_engine.simulation_mode = req.simulation_mode
    if req.order_type is not None:
        _arb_engine.order_type = req.order_type
    if req.limit_offset_ticks is not None:
        _arb_engine.limit_offset_ticks = req.limit_offset_ticks
    if req.vwap_buffer_ticks is not None:
        _arb_engine.vwap_buffer_ticks = req.vwap_buffer_ticks
    if req.min_profit is not None:
        _arb_engine.min_profit = req.min_profit
    if req.fill_timeout_ms is not None:
        _arb_engine.fill_timeout_ms = req.fill_timeout_ms
    if req.ws_enabled is not None:
        _arb_engine._ws_enabled = req.ws_enabled
    if req.ws_stale_ms is not None:
        _arb_engine._ws_stale_ms = req.ws_stale_ms
    if req.signal_confirmations is not None:
        _arb_engine.signal_confirmations = req.signal_confirmations
    # Reset position state when instruments change — old position no longer applies
    if instruments_changed:
        _arb_engine._has_position = False
        _arb_engine._long_sym = None
        _arb_engine._short_sym = None
        _arb_engine._entry_spread_actual = None
        logger.info("Instruments changed — position state reset")
        # Restart WS feeds for new instruments
        if _feed_manager is not None:
            _feed_manager.stop()
            _feed_manager.start({
                _arb_engine.leg_a_exchange: _arb_engine.xau_instrument,
                _arb_engine.leg_b_exchange: _arb_engine.paxg_instrument,
            })
            logger.info("WS feeds restarted for new instruments")
    return {"status": "ok", "message": "Configuration updated"}


@app.post("/arb/force-state")
async def arb_force_state(req: dict):
    """Manually set the engine's position state (e.g. after a restart).

    Body: {has_position: bool, long_sym: str|null, short_sym: str|null}
    """
    _arb_engine._has_position = req.get("has_position", False)
    _arb_engine._long_sym = req.get("long_sym")
    _arb_engine._short_sym = req.get("short_sym")
    _arb_engine._entry_spread_actual = req.get("entry_spread_actual")
    return {"status": "ok", "state": _arb_engine.position_info}


# ------------------------------------------------------------------
# Account info
# ------------------------------------------------------------------

@app.get("/account/summary")
async def account_summary():
    """Return the current sub-account summary (GRVT only, for backward compat)."""
    try:
        return _client.get_account_summary()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/account/all")
async def account_all():
    """Return equity, balance and positions from all exchanges."""
    result = []
    for exchange_name, client in _exchange_clients.items():
        entry = {"exchange": exchange_name, "equity": "0", "available": "0", "unrealized_pnl": "0", "positions": [], "error": None}
        try:
            summary = client.get_account_summary()
            entry["equity"] = summary.get("total_equity", "0")
            entry["available"] = summary.get("available_balance", "0")
            entry["unrealized_pnl"] = summary.get("unrealized_pnl", "0")
            positions = summary.get("positions", [])
            for p in positions:
                p["exchange"] = exchange_name
            entry["positions"] = positions
        except Exception as exc:
            entry["error"] = str(exc)
        result.append(entry)
    return result


@app.get("/account/positions")
async def account_positions(symbols: str = ""):
    """Return open positions from all exchanges. Pass comma-separated symbols or leave empty for all."""
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else []
    all_positions = []
    errors = []
    for exchange_name, client in _exchange_clients.items():
        try:
            positions = client.fetch_positions(sym_list)
            for p in positions:
                p["exchange"] = exchange_name
            all_positions.extend(positions)
        except Exception as exc:
            errors.append(f"{exchange_name}: {exc}")
    if errors and not all_positions:
        logger.warning("account/positions: all exchanges failed: %s", "; ".join(errors))
    return all_positions


# ------------------------------------------------------------------
# Portfolio stream (consolidated positions + funding, real-time SSE)
# ------------------------------------------------------------------

# Cache funding rates per (exchange, symbol) with a TTL
_funding_cache: dict[tuple[str, str], dict] = {}
_funding_cache_ts: dict[tuple[str, str], float] = {}
_FUNDING_CACHE_TTL = 60  # seconds

# Cache cumulative funding payments per (exchange, symbol) with a longer TTL
_funding_payments_cache: dict[tuple[str, str], float] = {}
_funding_payments_cache_ts: dict[tuple[str, str], float] = {}
_FUNDING_PAYMENTS_CACHE_TTL = 120  # seconds


async def _fetch_funding_for_position(client, exchange_name: str, symbol: str) -> dict:
    """Fetch funding rate for a position, with caching."""
    key = (exchange_name, symbol)
    now = time.time()
    if key in _funding_cache and (now - _funding_cache_ts.get(key, 0)) < _FUNDING_CACHE_TTL:
        return _funding_cache[key]
    try:
        if hasattr(client, "async_fetch_funding_rate"):
            rate_data = await client.async_fetch_funding_rate(symbol)
        else:
            rate_data = {"funding_rate": 0.0}
        _funding_cache[key] = rate_data
        _funding_cache_ts[key] = now
        return rate_data
    except Exception as exc:
        logger.debug("Funding rate fetch error for %s:%s: %s", exchange_name, symbol, exc)
        return _funding_cache.get(key, {"funding_rate": 0.0})


async def _fetch_cumulative_funding(client, exchange_name: str, symbol: str, start_time: int = 0) -> float:
    """Fetch cumulative funding payments for a position, with caching.

    Args:
        start_time: Unix ms timestamp — only sum funding after this time (e.g. position createdTime).
    """
    key = (exchange_name, symbol)
    now = time.time()
    if key in _funding_payments_cache and (now - _funding_payments_cache_ts.get(key, 0)) < _FUNDING_PAYMENTS_CACHE_TTL:
        return _funding_payments_cache[key]
    try:
        if hasattr(client, "async_fetch_funding_payments"):
            total = await client.async_fetch_funding_payments(symbol, start_time=start_time)
        elif hasattr(client, "fetch_funding_payments"):
            total = await asyncio.to_thread(client.fetch_funding_payments, symbol, start_time)
        else:
            total = 0.0
        _funding_payments_cache[key] = total
        _funding_payments_cache_ts[key] = now
        return total
    except Exception as exc:
        logger.debug("Cumulative funding fetch error for %s:%s: %s", exchange_name, symbol, exc)
        return _funding_payments_cache.get(key, 0.0)


async def _history_ingest_loop():
    """Background task: periodically push portfolio snapshots to Cloudflare D1."""
    import httpx

    assert _settings is not None
    url = _settings.history_ingest_url
    token = _settings.history_ingest_token
    interval = _settings.history_ingest_interval_s

    async with httpx.AsyncClient(timeout=30) as http:
        while True:
            try:
                await asyncio.sleep(interval)
                if not _vault_unlocked or not _exchange_clients:
                    continue

                # Build the same structure as /portfolio/stream
                exchanges: dict = {}
                for exchange_name, client in _exchange_clients.items():
                    if exchange_name == "variational":
                        continue  # skip — reduce Cloudflare 403 trigger rate
                    entry: dict = {
                        "exchange": exchange_name,
                        "equity": 0.0,
                        "unrealized_pnl": 0.0,
                        "positions": [],
                        "error": None,
                    }
                    try:
                        summary = await asyncio.to_thread(client.get_account_summary)
                        entry["equity"] = float(summary.get("total_equity", 0))
                        entry["unrealized_pnl"] = float(summary.get("unrealized_pnl", 0))

                        for p in summary.get("positions", []):
                            symbol = p.get("instrument", p.get("symbol", ""))
                            size_val = float(p.get("size", 0))
                            side = p.get("side", "LONG" if size_val > 0 else "SHORT")
                            unrealized = float(p.get("unrealized_pnl", 0))
                            realized = float(p.get("realized_pnl", 0))

                            cum_funding = float(p.get("cumulative_realized_funding_payment", 0))
                            if not cum_funding:
                                cum_funding = float(p.get("cumulative_funding", 0))
                            if not cum_funding:
                                created_time = int(p.get("created_time", 0))
                                cum_funding = await _fetch_cumulative_funding(
                                    client, exchange_name, symbol, start_time=created_time
                                )

                            funding_data = await _fetch_funding_for_position(client, exchange_name, symbol)

                            entry["positions"].append({
                                "instrument": symbol,
                                "token": _extract_token(symbol),
                                "side": side.upper(),
                                "size": abs(size_val),
                                "entry_price": float(p.get("entry_price", 0)),
                                "mark_price": float(p.get("mark_price", 0)),
                                "unrealized_pnl": unrealized,
                                "realized_pnl": realized,
                                "cumulative_funding": cum_funding,
                                "funding_rate": float(funding_data.get("funding_rate", 0)),
                                "leverage": float(p.get("leverage", 0)),
                            })
                    except Exception as exc:
                        entry["error"] = str(exc)
                    exchanges[exchange_name] = entry

                payload = {"exchanges": exchanges, "timestamp": time.time(), "user_id": os.environ.get("USER_ID", "")}
                resp = await http.post(
                    url,
                    json=payload,
                    headers={"Authorization": f"Bearer {token}"},
                )
                if resp.status_code == 200:
                    result = resp.json()
                    logger.debug(
                        "History ingest OK: eq=%s pos=%s trades=%s",
                        result.get("equity_rows"), result.get("position_rows"), result.get("trade_rows"),
                    )
                else:
                    logger.warning("History ingest failed: %s %s", resp.status_code, resp.text[:200])

            except asyncio.CancelledError:
                logger.info("History ingest task cancelled")
                return
            except Exception as exc:
                logger.warning("History ingest error: %s", exc)
                await asyncio.sleep(30)


def _extract_token(instrument: str) -> str:
    """Extract token name from instrument string (e.g. 'XRP-USD' -> 'XRP', 'P-SUI-USDC-3600' -> 'SUI')."""
    if instrument.startswith("P-"):
        parts = instrument.split("-")
        return parts[1] if len(parts) >= 2 else instrument
    parts = instrument.replace("_", "-").split("-")
    return parts[0] if parts else instrument


@app.get("/portfolio/stream")
async def portfolio_stream(interval_ms: int = 3000):
    """SSE stream: consolidated positions + funding from all exchanges."""

    async def event_generator():
        while True:
            try:
                exchanges = {}
                for exchange_name, client in _exchange_clients.items():
                    if exchange_name == "variational":
                        continue  # skip — reduce Cloudflare 403 trigger rate
                    entry = {
                        "exchange": exchange_name,
                        "equity": 0.0,
                        "unrealized_pnl": 0.0,
                        "positions": [],
                        "error": None,
                    }
                    try:
                        # Fetch account summary
                        summary = await asyncio.to_thread(client.get_account_summary)
                        entry["equity"] = float(summary.get("total_equity", 0))
                        entry["unrealized_pnl"] = float(summary.get("unrealized_pnl", 0))

                        # Fetch positions
                        positions_raw = summary.get("positions", [])
                        positions = []
                        for p in positions_raw:
                            symbol = p.get("instrument", p.get("symbol", ""))
                            # Fetch funding rate (cached)
                            funding_data = await _fetch_funding_for_position(client, exchange_name, symbol)
                            funding_rate = float(funding_data.get("funding_rate", 0))

                            size_val = float(p.get("size", 0))
                            side = p.get("side", "LONG" if size_val > 0 else "SHORT")

                            unrealized = float(p.get("unrealized_pnl", 0))
                            realized = float(p.get("realized_pnl", 0))

                            # Cumulative funding: GRVT has it in position data, others need API call
                            cum_funding = float(p.get("cumulative_realized_funding_payment", 0))
                            if not cum_funding and not p.get("cumulative_funding", 0):
                                created_time = int(p.get("created_time", 0))
                                cum_funding = await _fetch_cumulative_funding(client, exchange_name, symbol, start_time=created_time)
                            elif p.get("cumulative_funding"):
                                cum_funding = float(p.get("cumulative_funding", 0))

                            positions.append({
                                "instrument": symbol,
                                "token": _extract_token(symbol),
                                "side": side.upper(),
                                "size": abs(size_val),
                                "entry_price": float(p.get("entry_price", 0)),
                                "mark_price": float(p.get("mark_price", 0)),
                                "unrealized_pnl": unrealized,
                                "realized_pnl": realized,
                                "total_pnl": unrealized + realized,
                                "cumulative_funding": cum_funding,
                                "leverage": float(p.get("leverage", 0)),
                                "funding_rate": funding_rate,
                            })
                        entry["positions"] = positions
                    except Exception as exc:
                        entry["error"] = str(exc)
                    exchanges[exchange_name] = entry

                payload = {
                    "exchanges": exchanges,
                    "timestamp": time.time(),
                }
                yield f"data: {json.dumps(payload)}\n\n"
            except Exception as exc:
                logger.warning("Portfolio stream error: %s", exc)
                yield f"data: {json.dumps({'error': str(exc)})}\n\n"

            await asyncio.sleep(interval_ms / 1000)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/portfolio/pairs")
async def portfolio_pairs():
    """Build delta-neutral pairs from bot configs + token-name matching."""

    # Step 1: Collect all open positions across exchanges
    all_positions: list[dict] = []
    for exchange_name, client in _exchange_clients.items():
        if exchange_name == "variational":
            continue  # skip — reduce Cloudflare 403 trigger rate
        try:
            summary = await asyncio.to_thread(client.get_account_summary)
            for p in summary.get("positions", []):
                symbol = p.get("instrument", p.get("symbol", ""))
                size_val = float(p.get("size", 0))
                side = p.get("side", "LONG" if size_val > 0 else "SHORT")
                unrealized = float(p.get("unrealized_pnl", 0))
                realized = float(p.get("realized_pnl", 0))
                cum_funding = float(p.get("cumulative_realized_funding_payment", 0))
                if not cum_funding:
                    cum_funding = float(p.get("cumulative_funding", 0))
                if not cum_funding:
                    created_time = int(p.get("created_time", 0))
                    cum_funding = await _fetch_cumulative_funding(client, exchange_name, symbol, start_time=created_time)

                funding_data = await _fetch_funding_for_position(client, exchange_name, symbol)

                all_positions.append({
                    "exchange": exchange_name,
                    "instrument": symbol,
                    "token": _extract_token(symbol),
                    "side": side.upper(),
                    "size": abs(size_val),
                    "entry_price": float(p.get("entry_price", 0)),
                    "mark_price": float(p.get("mark_price", 0)),
                    "unrealized_pnl": unrealized,
                    "realized_pnl": realized,
                    "total_pnl": unrealized + realized,
                    "cumulative_funding": cum_funding,
                    "leverage": float(p.get("leverage", 0)),
                    "funding_rate": float(funding_data.get("funding_rate", 0)),
                })
        except Exception as exc:
            logger.warning("portfolio_pairs: error fetching %s: %s", exchange_name, exc)

    # Step 2: Build a bot-token lookup so we can tag pairs with bot source
    # Maps token (uppercase) -> bot_id for annotation purposes
    bot_token_map: dict[str, str] = {}
    if _bot_registry is not None:
        for bot_info in _bot_registry.list_bots():
            inst_a = bot_info.get("instrument_a", "")
            inst_b = bot_info.get("instrument_b", "")
            bot_id = bot_info.get("bot_id", "")
            # Extract token from both instruments and map to bot_id
            for inst in (inst_a, inst_b):
                token = _extract_token(inst).upper()
                if token:
                    bot_token_map[token] = bot_id

    # Step 3: Group all positions by token, then match long ↔ short
    token_groups: dict[str, list[dict]] = {}
    for p in all_positions:
        token_groups.setdefault(p["token"].upper(), []).append(p)

    pairs: list[dict] = []
    matched_keys: set[tuple[str, str]] = set()

    for token, group in sorted(token_groups.items()):
        longs = [p for p in group if p["side"] == "LONG"]
        shorts = [p for p in group if p["side"] == "SHORT"]

        # Determine source label
        source = f"bot:{bot_token_map[token]}" if token in bot_token_map else "token-match"

        # Pair longs with shorts (1:1, by order)
        paired_count = min(len(longs), len(shorts))
        for i in range(paired_count):
            pairs.append(_build_pair(
                token=token,
                source=source,
                long_pos=longs[i],
                short_pos=shorts[i],
            ))
            matched_keys.add((longs[i]["exchange"], longs[i]["instrument"]))
            matched_keys.add((shorts[i]["exchange"], shorts[i]["instrument"]))

        # Remaining unmatched longs/shorts
        for extra in longs[paired_count:] + shorts[paired_count:]:
            pairs.append(_build_pair(
                token=token,
                source="unmatched",
                long_pos=extra if extra["side"] == "LONG" else None,
                short_pos=extra if extra["side"] == "SHORT" else None,
            ))
            matched_keys.add((extra["exchange"], extra["instrument"]))

    return {"pairs": pairs, "timestamp": time.time()}


def _build_pair(token: str, source: str, long_pos: dict | None, short_pos: dict | None) -> dict:
    """Build a delta-neutral pair dict."""
    combined_unrealized = (long_pos or {}).get("unrealized_pnl", 0) + (short_pos or {}).get("unrealized_pnl", 0)
    combined_realized = (long_pos or {}).get("realized_pnl", 0) + (short_pos or {}).get("realized_pnl", 0)
    combined_funding = (long_pos or {}).get("cumulative_funding", 0) + (short_pos or {}).get("cumulative_funding", 0)
    return {
        "token": token,
        "source": source,
        "long": long_pos,
        "short": short_pos,
        "combined_pnl": {
            "unrealized": combined_unrealized,
            "realized": combined_realized,
            "total": combined_unrealized + combined_realized,
            "funding_net": combined_funding,
        },
    }


# ------------------------------------------------------------------
# Leverage
# ------------------------------------------------------------------

@app.get("/leverage")
async def get_leverage():
    """Get current leverage settings for all instruments."""
    try:
        return _client.get_all_leverage()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/leverage")
async def set_leverage(req: dict):
    """Set leverage for an instrument. Body: {instrument, leverage}"""
    instrument = req.get("instrument")
    leverage = req.get("leverage")
    if not instrument or not leverage:
        raise HTTPException(status_code=400, detail="instrument and leverage required")
    try:
        success = _client.set_leverage(instrument, int(leverage))
        return {"success": success, "instrument": instrument, "leverage": int(leverage)}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ------------------------------------------------------------------
# Exchange markets
# ------------------------------------------------------------------

@app.get("/exchanges/markets")
async def exchange_markets(exchange: str = "grvt"):
    """List available instruments on a given exchange."""
    client = _exchange_clients.get(exchange)
    if not client:
        raise HTTPException(status_code=400, detail=f"Unknown exchange: {exchange}. Available: {list(_exchange_clients.keys())}")
    try:
        return {"exchange": exchange, "markets": client.fetch_markets()}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/exchanges")
async def list_exchanges():
    """List available exchange names."""
    return {"exchanges": list(_exchange_clients.keys())}


# ------------------------------------------------------------------
# Orderbooks (for WebUI visualization)
# ------------------------------------------------------------------

@app.get("/arb/orderbooks")
async def arb_orderbooks(depth: int = 10):
    """Return orderbook data for both arb legs (WS cache or REST fallback)."""
    try:
        book_a = _arb_engine._get_orderbook(_arb_engine.leg_a_exchange, _arb_engine.xau_instrument, limit=depth)
        book_b = _arb_engine._get_orderbook(_arb_engine.leg_b_exchange, _arb_engine.paxg_instrument, limit=depth)
        return {
            "leg_a": {
                "exchange": _arb_engine.leg_a_exchange,
                "instrument": _arb_engine.xau_instrument,
                "bids": book_a.get("bids", [])[:depth],
                "asks": book_a.get("asks", [])[:depth],
                "source": book_a.get("_source", "rest"),
            },
            "leg_b": {
                "exchange": _arb_engine.leg_b_exchange,
                "instrument": _arb_engine.paxg_instrument,
                "bids": book_b.get("bids", [])[:depth],
                "asks": book_b.get("asks", [])[:depth],
                "source": book_b.get("_source", "rest"),
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ------------------------------------------------------------------
# WebSocket feed status
# ------------------------------------------------------------------

@app.get("/ws/status")
async def ws_status():
    """Return WebSocket feed connection status."""
    if _feed_manager is None:
        return {"enabled": False, "feeds": {}}
    return {"enabled": _arb_engine._ws_enabled, "feeds": _feed_manager.status()}


# ------------------------------------------------------------------
# Job management
# ------------------------------------------------------------------


def _job_status_response(job: ArbJob) -> JobStatusResponse:
    """Build a JobStatusResponse from an ArbJob."""
    eng = job.engine
    pi = eng.position_info
    return JobStatusResponse(
        job_id=job.job_id,
        name=job.name,
        status=job.status,
        auto_trade=job.auto_trade,
        created_at=job.created_at,
        entry_time=job.entry_time,
        instrument_a=eng.instrument_a,
        instrument_b=eng.instrument_b,
        leg_a_exchange=eng.leg_a_exchange,
        leg_b_exchange=eng.leg_b_exchange,
        spread_entry_low=eng.spread_entry_low,
        spread_exit_high=eng.spread_exit_high,
        max_exec_spread=eng.max_exec_spread,
        quantity=float(eng.quantity),
        simulation_mode=eng.simulation_mode,
        order_type=eng.order_type,
        limit_offset_ticks=eng.limit_offset_ticks,
        vwap_buffer_ticks=eng.vwap_buffer_ticks,
        min_profit=eng.min_profit,
        fill_timeout_ms=eng.fill_timeout_ms,
        chunk_size=float(eng.chunk_size),
        chunk_delay_ms=eng.chunk_delay_ms,
        liquidity_multiplier=eng.liquidity_multiplier,
        min_top_book_mult=eng.min_top_book_mult,
        signal_confirmations=eng.signal_confirmations,
        strategy=eng.strategy,
        max_spread_pct=eng.max_spread_pct,
        funding_rate_bias=eng.funding_rate_bias,
        hold_duration_h=job.schedule.hold_duration_h,
        min_exit_spread=job.schedule.min_exit_spread,
        has_position=pi["has_position"],
        long_sym=pi["long_sym"],
        short_sym=pi["short_sym"],
        entry_spread_actual=pi["entry_spread"],
        entry_fill_long=pi.get("entry_fill_long"),
        entry_fill_short=pi.get("entry_fill_short"),
        ws_enabled=eng._ws_enabled,
        ws_stale_ms=eng._ws_stale_ms,
    )


@app.get("/jobs")
async def list_jobs():
    """List all arb jobs."""
    return {"jobs": _job_manager.list_jobs()}


@app.post("/jobs")
async def create_job(req: JobCreateRequest):
    """Create a new arb job."""
    try:
        job = _job_manager.create_job(req.model_dump())
        return {"status": "ok", "job": _job_status_response(job).model_dump()}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/jobs/{job_id}")
async def get_job(job_id: str):
    """Get full status of a single job."""
    try:
        job = _job_manager.get_job(job_id)
        return _job_status_response(job)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.put("/jobs/{job_id}/config")
async def update_job_config(job_id: str, req: JobConfigUpdateRequest):
    """Update job configuration."""
    try:
        config = {k: v for k, v in req.model_dump().items() if v is not None}
        job = _job_manager.update_job_config(job_id, config)
        return {"status": "ok", "job": _job_status_response(job).model_dump()}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    """Delete an arb job."""
    try:
        _job_manager.delete_job(job_id)
        return {"status": "ok", "message": f"Job {job_id} deleted"}
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/jobs/{job_id}/trigger")
async def trigger_job(job_id: str, req: ArbTriggerRequest):
    """Manual entry/exit for a specific job."""
    try:
        job = _job_manager.get_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    engine = job.engine

    # Save original thresholds for restore after force execution
    orig_entry = engine.spread_entry_low
    orig_exit = engine.spread_exit_high

    if req.force:
        # Force mode: set thresholds so spread guards always pass
        engine.spread_entry_low = 0.0
        engine.spread_exit_high = 999999.0
    if req.spread_entry_low is not None:
        engine.spread_entry_low = req.spread_entry_low
    if req.spread_exit_high is not None:
        engine.spread_exit_high = req.spread_exit_high
    if req.quantity is not None:
        engine.quantity = Decimal(str(req.quantity))

    # Capture entry fill prices BEFORE execute_signal clears them (for EXIT PnL)
    _entry_fill_long = engine._entry_fill_long if req.action.upper() == "EXIT" else None
    _entry_fill_short = engine._entry_fill_short if req.action.upper() == "EXIT" else None

    try:
        result = engine.execute_signal(
            req.action,
            min_depth_usd=req.min_depth_usd,
            slippage_pct=req.slippage_pct,
        )
    except ValueError as exc:
        raise HTTPException(status_code=503, detail=f"Orderbook not available: {exc}")
    finally:
        if req.force:
            engine.spread_entry_low = orig_entry
            engine.spread_exit_high = orig_exit

    # Log the trade with PnL
    _job_manager._log_trade(
        job, req.action.upper(), result, result.snapshot,
        entry_fill_long=_entry_fill_long, entry_fill_short=_entry_fill_short,
    )

    if result.success and req.action.upper() == "ENTRY":
        job.entry_time = datetime.now(timezone.utc).isoformat()
        job.status = "holding"
    elif result.success and req.action.upper() == "EXIT":
        job.entry_time = None
        job.status = "monitoring" if job.auto_trade else "idle"

    leg_a_info = ArbLegInfo(success=result.leg_a.success, error=result.leg_a.error) if result.leg_a else None
    leg_b_info = ArbLegInfo(success=result.leg_b.success, error=result.leg_b.error) if result.leg_b else None

    return ArbExecutionResponse(
        success=result.success,
        leg_a=leg_a_info,
        leg_b=leg_b_info,
        snapshot=_spread_info(result.snapshot),
        error=result.error,
    )


@app.get("/jobs/{job_id}/check")
async def check_job(job_id: str):
    """Spread check for a specific job."""
    try:
        job = _job_manager.get_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    try:
        snapshot = job.engine.get_spread_snapshot()
    except ValueError as exc:
        return {"action": "NONE", "snapshot": None, "reason": f"Orderbook not available: {exc}"}
    job._last_spread = snapshot
    check = job.engine.evaluate(snapshot)
    return ArbCheckResponse(
        action=check.action,
        snapshot=_spread_info(snapshot),
        reason=check.reason,
    )


@app.get("/jobs/{job_id}/orderbooks")
async def job_orderbooks(job_id: str, depth: int = 10):
    """Orderbooks for a specific job's pair."""
    try:
        job = _job_manager.get_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    engine = job.engine
    try:
        book_a = engine._get_orderbook(engine.leg_a_exchange, engine.xau_instrument, limit=depth)
        book_b = engine._get_orderbook(engine.leg_b_exchange, engine.paxg_instrument, limit=depth)
        return {
            "leg_a": {
                "exchange": engine.leg_a_exchange,
                "instrument": engine.xau_instrument,
                "bids": book_a.get("bids", [])[:depth],
                "asks": book_a.get("asks", [])[:depth],
                "source": book_a.get("_source", "rest"),
            },
            "leg_b": {
                "exchange": engine.leg_b_exchange,
                "instrument": engine.paxg_instrument,
                "bids": book_b.get("bids", [])[:depth],
                "asks": book_b.get("asks", [])[:depth],
                "source": book_b.get("_source", "rest"),
            },
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/jobs/{job_id}/log")
async def job_trade_log(job_id: str, limit: int = 50):
    """Trade log entries for a specific job."""
    try:
        job = _job_manager.get_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    entries = job.trade_log[-limit:] if limit else job.trade_log
    return {"job_id": job_id, "entries": [asdict(e) for e in reversed(entries)]}


@app.delete("/jobs/{job_id}/log")
async def job_clear_trade_log(job_id: str):
    """Clear trade log for a specific job (in-memory and on disk)."""
    try:
        _job_manager.clear_trade_log(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"status": "ok", "message": f"Trade log cleared for job {job_id}"}


@app.get("/jobs/{job_id}/spread-stream")
async def job_spread_stream(job_id: str, interval_ms: int = 1000):
    """SSE real-time spread stream for a specific job."""
    try:
        _job_manager.get_job(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    async def event_generator():
        while True:
            try:
                job = _job_manager.get_job(job_id)
                snapshot = job.engine.get_spread_snapshot()
                job._last_spread = snapshot
                data = {
                    "spread": snapshot.spread,
                    "spread_abs": snapshot.spread_abs,
                    "spread_pct": snapshot.spread_pct,
                    "mid_a": snapshot.mid_price_a,
                    "mid_b": snapshot.mid_price_b,
                    "exec_spread": snapshot.exec_spread,
                    "data_source": snapshot.data_source,
                    "ts": time.time(),
                }
                yield f"data: {json.dumps(data)}\n\n"
            except Exception:
                yield f"data: {json.dumps({'error': 'failed'})}\n\n"
            await asyncio.sleep(interval_ms / 1000.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/jobs/tick")
async def tick_all_jobs():
    """Manually trigger a tick on all jobs (same as auto-trade timer)."""
    results = _job_manager.tick_all()
    return {"status": "ok", "results": results}


# ==================================================================
# Funding-Arb Engine endpoints (new)
# ==================================================================

def _require_fn_engine() -> FundingArbEngine:
    if _fn_engine is None:
        raise HTTPException(status_code=503, detail="Funding-arb engine not started")
    return _fn_engine


@app.get("/fn/status")
async def fn_status():
    """Full status of the funding-arb engine."""
    engine = _require_fn_engine()
    return engine.get_status()


@app.get("/fn/funding")
async def fn_funding():
    """Current funding rates and suggestion."""
    engine = _require_fn_engine()
    return engine._funding_monitor.get_rates() if engine._funding_monitor else {}


@app.get("/fn/suggestion")
async def fn_suggestion():
    """Current funding rate direction suggestion."""
    engine = _require_fn_engine()
    s = engine.get_funding_suggestion()
    return {
        "recommended_long": s.recommended_long_exchange,
        "recommended_short": s.recommended_short_exchange,
        "funding_rate_a": s.funding_rate_a,
        "funding_rate_b": s.funding_rate_b,
        "exchange_a": s.exchange_a,
        "exchange_b": s.exchange_b,
        "spread": s.funding_spread,
        "spread_annualised": s.funding_spread_annualised,
        "reason": s.reason,
    }


@app.post("/fn/start")
async def fn_start(req: dict | None = None):
    """Start the bot: set leverage → enter position → start countdown timer.

    Returns immediately — entry runs as a background task.
    Optional body: {duration_h, duration_m, leverage_long, leverage_short,
                    quantity, long_exchange, short_exchange, instrument_a, instrument_b}
    """
    engine = _require_fn_engine()
    # Guard: require orderbook feeds to be connected + synced before starting
    if engine._data_layer and not engine._data_layer.is_ready():
        raise HTTPException(
            status_code=409,
            detail="Orderbook feeds not ready. Wait for all WS connections to be established.",
        )
    body = req or {}

    async def _run_entry():
        try:
            await engine.run(
                duration_h=body.get("duration_h"),
                duration_m=body.get("duration_m"),
                leverage_long=body.get("leverage_long"),
                leverage_short=body.get("leverage_short"),
                quantity=Decimal(str(body["quantity"])) if body.get("quantity") else None,
                long_exchange=body.get("long_exchange"),
                short_exchange=body.get("short_exchange"),
                instrument_a=body.get("instrument_a"),
                instrument_b=body.get("instrument_b"),
            )
        except Exception as exc:
            logger.error("Background entry failed: %s", exc)
            engine.log_activity("ENGINE", f"Entry failed: {exc}", level="error")

    try:
        # Validate config before launching background task
        if engine._is_running:
            raise RuntimeError("Bot is already running")
        engine._execution_task = asyncio.create_task(_run_entry())
        return {"success": True, "message": "Bot starting — track progress in activity log"}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/fn/stop")
async def fn_stop():
    """Stop the bot: cancel countdown timer → exit position (Maker-Taker TWAP).

    Returns immediately — exit runs as a background task.
    """
    engine = _require_fn_engine()

    async def _run_exit():
        try:
            await engine.graceful_stop(reason="manual")
        except Exception as exc:
            logger.error("Background exit failed: %s", exc)
            engine.log_activity("ENGINE", f"Exit failed: {exc}", level="error")

    try:
        is_holding = (engine._state_machine and engine._state_machine.state.value == "HOLDING")
        if not engine._is_running and not is_holding:
            raise RuntimeError("Bot is not running and has no position to close")
        engine._execution_task = asyncio.create_task(_run_exit())
        return {"success": True, "message": "Bot stopping — track progress in activity log"}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/fn/kill")
async def fn_kill():
    """Hard kill: cancel open orders, abort execution, reset to IDLE.

    No exit trades — positions stay on exchanges as-is.
    """
    engine = _require_fn_engine()
    try:
        result = await engine.force_kill()
        return {"success": True, "message": "Bot killed — orders cancelled, state reset to IDLE", **result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/fn/timer")
async def fn_timer(req: dict):
    """Adjust the countdown timer on the running bot.

    Body: {"duration_h": int, "duration_m": int}
    Sets a new timer from now. Both 0 = run indefinitely.
    """
    engine = _require_fn_engine()
    try:
        result = await engine.adjust_timer(
            duration_h=req.get("duration_h"),
            duration_m=req.get("duration_m"),
        )
        return result
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/fn/entry")
async def fn_entry(
    long_exchange: str | None = None,
    short_exchange: str | None = None,
    quantity: float | None = None,
):
    """Manually trigger a delta-neutral entry (without leverage/timer)."""
    engine = _require_fn_engine()
    try:
        result = await engine.manual_entry(
            long_exchange=long_exchange,
            short_exchange=short_exchange,
            quantity=Decimal(str(quantity)) if quantity else None,
        )
        return {
            "success": result.success,
            "error": result.error,
            "total_maker_qty": result.total_maker_qty,
            "total_taker_qty": result.total_taker_qty,
            "num_chunks": len(result.chunks),
            "duration_s": result.end_ts - result.start_ts,
        }
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/fn/exit")
async def fn_exit(quantity: float | None = None):
    """Manually trigger a delta-neutral exit (without stopping the timer)."""
    engine = _require_fn_engine()
    try:
        result = await engine.manual_exit(
            quantity=Decimal(str(quantity)) if quantity else None,
        )
        return {
            "success": result.success,
            "error": result.error,
            "total_maker_qty": result.total_maker_qty,
            "total_taker_qty": result.total_taker_qty,
            "num_chunks": len(result.chunks),
            "duration_s": result.end_ts - result.start_ts,
        }
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/fn/position")
async def fn_position():
    """Current position info from the state machine."""
    engine = _require_fn_engine()
    return engine._state_machine.position_info if engine._state_machine else {}


@app.get("/fn/execution")
async def fn_execution():
    """Current execution status (chunk progress etc.)."""
    engine = _require_fn_engine()
    return engine._state_machine.execution_status if engine._state_machine else {}


@app.get("/fn/trades")
async def fn_trades(limit: int = 50):
    """Trade log for the funding-arb engine."""
    engine = _require_fn_engine()
    return engine.get_trade_log(limit)


@app.get("/fn/risk")
async def fn_risk():
    """Risk manager status and alerts."""
    engine = _require_fn_engine()
    return {
        "status": engine._risk_manager.get_status() if engine._risk_manager else {},
        "alerts": engine.get_risk_alerts(),
    }


@app.post("/fn/risk/reset-halt")
async def fn_risk_reset_halt():
    """Reset circuit breaker halt."""
    engine = _require_fn_engine()
    if engine._risk_manager:
        engine._risk_manager.reset_halt()
    return {"status": "ok"}


@app.post("/fn/risk/reset-pnl")
async def fn_risk_reset_pnl():
    """Reset cumulative PnL counter."""
    engine = _require_fn_engine()
    if engine._risk_manager:
        engine._risk_manager.reset_pnl()
    return {"status": "ok"}


@app.post("/fn/config")
async def fn_config_update(updates: dict):
    """Update engine config and restart data feeds if instruments/exchanges changed."""
    engine = _require_fn_engine()
    try:
        result = await engine.apply_config_and_restart_feeds(**updates)
        return {"status": "ok", "updated": list(updates.keys()), "feeds_restarted": result.get("feeds_restarted", False)}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/fn/reset")
async def fn_reset():
    """Force reset state machine to IDLE."""
    engine = _require_fn_engine()
    if engine._state_machine:
        engine._state_machine.reset()
    return {"status": "ok"}


@app.get("/fn/log")
async def fn_activity_log(since_seq: int = 0, limit: int = 100):
    """Real-time activity log (incremental fetch via since_seq)."""
    engine = _require_fn_engine()
    return {"entries": engine.get_activity_log(since_seq=since_seq, limit=limit)}


@app.get("/fn/data")
async def fn_data():
    """Data layer feed status (orderbooks, funding rates)."""
    engine = _require_fn_engine()
    return engine._data_layer.status() if engine._data_layer else {}


@app.get("/fn/stream")
async def fn_stream(interval_ms: int = 2000):
    """SSE stream for real-time funding-arb dashboard updates."""
    engine = _require_fn_engine()

    async def event_generator():
        while True:
            try:
                now = time.time()
                remaining_s = None
                if engine._expires_at:
                    remaining_s = max(0, engine._expires_at - now)
                data = {
                    "state": engine._state_machine.state.value if engine._state_machine else "?",
                    "is_running": engine._is_running,
                    "timer": {
                        "started_at": engine._started_at,
                        "expires_at": engine._expires_at,
                        "remaining_s": remaining_s,
                        "stop_reason": engine._stop_reason,
                    },
                    "leverage": {
                        "long": engine.config.leverage_long,
                        "short": engine.config.leverage_short,
                    },
                    "prices": engine.get_live_prices(),
                    "pnl": engine.get_unrealized_pnl(),
                    "position": engine._state_machine.position_info if engine._state_machine else {},
                    "execution": engine._state_machine.execution_status if engine._state_machine else {},
                    "funding": engine._funding_monitor.get_rates() if engine._funding_monitor else {},
                    "risk": engine._risk_manager.get_status() if engine._risk_manager else {},
                    "feeds_ready": engine._data_layer.is_ready() if engine._data_layer else False,
                    "data": engine._data_layer.status() if engine._data_layer else {},
                    "activity_log": engine.get_activity_log(limit=50),
                    "ts": now,
                }
                yield f"data: {json.dumps(data)}\n\n"
            except Exception:
                yield f"data: {json.dumps({'error': 'failed'})}\n\n"
            await asyncio.sleep(interval_ms / 1000.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ==================================================================
# Multi-Bot Registry endpoints (v2)
# ==================================================================

def _require_registry() -> BotRegistry:
    if _bot_registry is None:
        raise HTTPException(status_code=503, detail="Bot registry not started")
    return _bot_registry


def _require_bot(bot_id: str) -> FundingArbEngine:
    registry = _require_registry()
    try:
        return registry.get_bot(bot_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Bot '{bot_id}' not found")


@app.get("/fn/bots")
async def fn_bots_list():
    """List all bots with summary info."""
    registry = _require_registry()
    return {"bots": registry.list_bots()}


@app.post("/fn/bots")
async def fn_bots_create(req: dict):
    """Create a new bot. Body: {bot_id, ...config fields}."""
    registry = _require_registry()
    bot_id = req.pop("bot_id", None)
    if not bot_id:
        raise HTTPException(status_code=400, detail="bot_id is required")
    try:
        # Build config from defaults + overrides
        config = EngineConfig.from_settings(_settings, job_id=bot_id) if _settings else EngineConfig(job_id=bot_id)
        for key, val in req.items():
            if hasattr(config, key):
                if key == "quantity":
                    setattr(config, key, Decimal(str(val)))
                else:
                    setattr(config, key, val)
        await registry.create_bot(bot_id, config)
        return {"success": True, "bot_id": bot_id}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.delete("/fn/bots/{bot_id}")
async def fn_bots_delete(bot_id: str):
    """Delete a bot (must be IDLE)."""
    registry = _require_registry()
    try:
        await registry.delete_bot(bot_id)
        return {"success": True, "bot_id": bot_id}
    except (KeyError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/fn/bots/{bot_id}/status")
async def fn_bot_status(bot_id: str):
    """Full status of a specific bot."""
    engine = _require_bot(bot_id)
    return engine.get_status()


@app.get("/fn/bots/{bot_id}/funding")
async def fn_bot_funding(bot_id: str):
    """Current funding rates for a specific bot."""
    engine = _require_bot(bot_id)
    return engine._funding_monitor.get_rates() if engine._funding_monitor else {}


@app.post("/fn/bots/{bot_id}/start")
async def fn_bot_start(bot_id: str, req: dict | None = None):
    """Start a specific bot."""
    engine = _require_bot(bot_id)
    if engine._data_layer and not engine._data_layer.is_ready():
        raise HTTPException(status_code=409, detail="Orderbook feeds not ready.")
    body = req or {}

    async def _run_entry():
        try:
            await engine.run(
                duration_h=body.get("duration_h"),
                duration_m=body.get("duration_m"),
                leverage_long=body.get("leverage_long"),
                leverage_short=body.get("leverage_short"),
                quantity=Decimal(str(body["quantity"])) if body.get("quantity") else None,
                long_exchange=body.get("long_exchange"),
                short_exchange=body.get("short_exchange"),
                instrument_a=body.get("instrument_a"),
                instrument_b=body.get("instrument_b"),
            )
        except Exception as exc:
            logger.error("Bot %s entry failed: %s", bot_id, exc)
            engine.log_activity("ENGINE", f"Entry failed: {exc}", level="error")

    try:
        if engine._is_running:
            raise RuntimeError("Bot is already running")
        engine._execution_task = asyncio.create_task(_run_entry())
        return {"success": True, "message": f"Bot '{bot_id}' starting"}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/fn/bots/{bot_id}/stop")
async def fn_bot_stop(bot_id: str):
    """Graceful stop a specific bot (exit position)."""
    engine = _require_bot(bot_id)

    async def _run_exit():
        try:
            await engine.graceful_stop(reason="manual")
        except Exception as exc:
            logger.error("Bot %s exit failed: %s", bot_id, exc)
            engine.log_activity("ENGINE", f"Exit failed: {exc}", level="error")

    try:
        is_holding = (engine._state_machine and engine._state_machine.state.value == "HOLDING")
        if not engine._is_running and not is_holding:
            raise RuntimeError("Bot is not running and has no position to close")
        engine._execution_task = asyncio.create_task(_run_exit())
        return {"success": True, "message": f"Bot '{bot_id}' stopping"}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/fn/bots/{bot_id}/kill")
async def fn_bot_kill(bot_id: str):
    """Hard kill a specific bot."""
    engine = _require_bot(bot_id)
    try:
        result = await engine.force_kill()
        return {"success": True, "message": f"Bot '{bot_id}' killed", **result}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/fn/bots/{bot_id}/timer")
async def fn_bot_timer(bot_id: str, req: dict):
    """Adjust the countdown timer on a specific bot.

    Body: {"duration_h": int, "duration_m": int}
    Sets a new timer from now. Both 0 = run indefinitely.
    """
    engine = _require_bot(bot_id)
    try:
        result = await engine.adjust_timer(
            duration_h=req.get("duration_h"),
            duration_m=req.get("duration_m"),
        )
        return result
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/fn/bots/{bot_id}/config")
async def fn_bot_config_update(bot_id: str, updates: dict):
    """Update config for a specific bot."""
    engine = _require_bot(bot_id)
    try:
        result = await engine.apply_config_and_restart_feeds(**updates)
        # Persist updated config
        _require_registry().update_bot_config(bot_id, engine.config)
        return {"status": "ok", "updated": list(updates.keys()), "feeds_restarted": result.get("feeds_restarted", False)}
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/fn/bots/{bot_id}/pause")
async def fn_bot_pause(bot_id: str):
    """Pause a running TWAP execution (entry or exit)."""
    engine = _require_bot(bot_id)
    try:
        return engine.pause_execution()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/fn/bots/{bot_id}/resume")
async def fn_bot_resume(bot_id: str):
    """Resume a paused TWAP execution."""
    engine = _require_bot(bot_id)
    try:
        return engine.resume_execution()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/fn/bots/{bot_id}/reset")
async def fn_bot_reset(bot_id: str):
    """Force reset a bot's state machine to IDLE."""
    engine = _require_bot(bot_id)
    if engine._state_machine:
        engine._state_machine.reset()
    return {"status": "ok"}


@app.get("/fn/bots/{bot_id}/position")
async def fn_bot_position(bot_id: str):
    """Current position info for a specific bot."""
    engine = _require_bot(bot_id)
    return engine._state_machine.position_info if engine._state_machine else {}


@app.get("/fn/bots/{bot_id}/risk")
async def fn_bot_risk(bot_id: str):
    """Risk status for a specific bot."""
    engine = _require_bot(bot_id)
    return {
        "status": engine._risk_manager.get_status() if engine._risk_manager else {},
        "alerts": engine.get_risk_alerts(),
    }


@app.get("/fn/bots/{bot_id}/suggestion")
async def fn_bot_suggestion(bot_id: str):
    """Current funding rate direction suggestion for a specific bot."""
    engine = _require_bot(bot_id)
    s = engine.get_funding_suggestion()
    return {
        "recommended_long": s.recommended_long_exchange,
        "recommended_short": s.recommended_short_exchange,
        "funding_rate_a": s.funding_rate_a,
        "funding_rate_b": s.funding_rate_b,
        "exchange_a": s.exchange_a,
        "exchange_b": s.exchange_b,
        "spread": s.funding_spread,
        "spread_annualised": s.funding_spread_annualised,
        "reason": s.reason,
    }


@app.get("/fn/bots/{bot_id}/trades")
async def fn_bot_trades(bot_id: str, limit: int = 50):
    """Trade log for a specific bot."""
    engine = _require_bot(bot_id)
    return engine.get_trade_log(limit=limit)


@app.get("/fn/bots/{bot_id}/log")
async def fn_bot_log(bot_id: str, since_seq: int = 0, limit: int = 100):
    """Activity log for a specific bot."""
    engine = _require_bot(bot_id)
    return {"entries": engine.get_activity_log(since_seq=since_seq, limit=limit)}


@app.get("/fn/bots/{bot_id}/stream")
async def fn_bot_stream(bot_id: str, interval_ms: int = 2000):
    """SSE stream for a specific bot."""
    engine = _require_bot(bot_id)

    async def event_generator():
        while True:
            try:
                now = time.time()
                remaining_s = None
                if engine._expires_at:
                    remaining_s = max(0, engine._expires_at - now)
                data = {
                    "bot_id": bot_id,
                    "state": engine._state_machine.state.value if engine._state_machine else "?",
                    "is_running": engine._is_running,
                    "timer": {
                        "started_at": engine._started_at,
                        "expires_at": engine._expires_at,
                        "remaining_s": remaining_s,
                        "stop_reason": engine._stop_reason,
                    },
                    "leverage": {
                        "long": engine.config.leverage_long,
                        "short": engine.config.leverage_short,
                    },
                    "prices": engine.get_live_prices(),
                    "pnl": engine.get_unrealized_pnl(),
                    "position": engine._state_machine.position_info if engine._state_machine else {},
                    "execution": engine._state_machine.execution_status if engine._state_machine else {},
                    "funding": engine._funding_monitor.get_rates() if engine._funding_monitor else {},
                    "risk": engine._risk_manager.get_status() if engine._risk_manager else {},
                    "feeds_ready": engine._data_layer.is_ready() if engine._data_layer else False,
                    "data": engine._data_layer.status() if engine._data_layer else {},
                    "orderbooks": {
                        "long": engine._data_layer.get_orderbook_depth(engine.config.long_exchange, engine.config.instrument_a, depth=10) if engine._data_layer else {},
                        "short": engine._data_layer.get_orderbook_depth(engine.config.short_exchange, engine.config.instrument_b, depth=10) if engine._data_layer else {},
                    } if engine._data_layer and engine.config.instrument_a and engine.config.instrument_b else {},
                    "activity_log": engine.get_activity_log(limit=50),
                    "config": {
                        "long_exchange": engine.config.long_exchange,
                        "short_exchange": engine.config.short_exchange,
                        "maker_exchange": engine.config.maker_exchange,
                        "instrument_a": engine.config.instrument_a,
                        "instrument_b": engine.config.instrument_b,
                        "quantity": float(engine.config.quantity),
                        "twap_num_chunks": engine.config.twap_num_chunks,
                        "twap_interval_s": engine.config.twap_interval_s,
                        "simulation": engine.config.simulation,
                        "max_chunk_spread_usd": engine.config.max_chunk_spread_usd,
                        "max_spread_pct": engine.config.max_spread_pct,
                        "duration_h": engine.config.duration_h,
                        "duration_m": engine.config.duration_m,
                    },
                    "ts": now,
                }
                yield f"data: {json.dumps(data)}\n\n"
            except Exception:
                yield f"data: {json.dumps({'error': 'failed'})}\n\n"
            await asyncio.sleep(interval_ms / 1000.0)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.websocket("/fn/bots/{bot_id}/ws/orderbooks")
async def fn_bot_ws_orderbooks(ws: WebSocket, bot_id: str):
    """Real-time orderbook WebSocket stream for a specific bot.

    Pushes top-10 bid/ask levels for both exchanges whenever the
    DataLayer receives a new WS update from any exchange.
    Throttled to max ~100ms between sends.
    """
    try:
        engine = _require_bot(bot_id)
    except Exception:
        await ws.close(code=4004, reason=f"Bot '{bot_id}' not found")
        return

    dl = engine._data_layer
    if not dl:
        await ws.close(code=4003, reason="DataLayer not started")
        return

    await ws.accept()
    MIN_INTERVAL = 0.1  # 100ms throttle
    last_send = 0.0

    try:
        while True:
            # Wait for any orderbook change
            dl._ob_changed.clear()
            try:
                await asyncio.wait_for(dl._ob_changed.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                # Send a heartbeat even if no changes (keeps connection alive)
                pass

            # Throttle: skip if we sent too recently
            now = time.time()
            elapsed = now - last_send
            if elapsed < MIN_INTERVAL:
                await asyncio.sleep(MIN_INTERVAL - elapsed)

            try:
                data = {
                    "long": dl.get_orderbook_depth(engine.config.long_exchange, engine.config.instrument_a, depth=10),
                    "short": dl.get_orderbook_depth(engine.config.short_exchange, engine.config.instrument_b, depth=10),
                    "ts": time.time(),
                }
                await ws.send_json(data)
                last_send = time.time()
            except Exception:
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass


# ------------------------------------------------------------------
# Settings / API Keys (encrypted with user password)
# ------------------------------------------------------------------

_SECRETS_ENC_FILE = _DATA_DIR / "secrets.enc"
_SECRETS_FILE_LEGACY = _DATA_DIR / "secrets.json"
_VAULT_SESSION_FILE = _DATA_DIR / ".vault_session"
_current_password: str | None = None  # held in RAM while vault is unlocked

# ── Vault session persistence (auto-unlock after uvicorn reload) ──────

def _session_key() -> bytes:
    """Derive an AES-256 key from hostname + fixed pepper for session encryption."""
    import hashlib
    hostname = os.environ.get("HOSTNAME", "tradeautonom-default")
    pepper = "vault-session-pepper-2026"
    return hashlib.pbkdf2_hmac("sha256", f"{hostname}:{pepper}".encode(), b"session-salt", 100_000, dklen=32)


def _save_vault_session(password: str) -> None:
    """Encrypt and persist password to disk for auto-unlock after reload."""
    try:
        key = _session_key()
        nonce = os.urandom(12)
        from app.crypto import _aes_gcm_encrypt
        ciphertext, tag = _aes_gcm_encrypt(key, nonce, password.encode("utf-8"))
        _VAULT_SESSION_FILE.write_bytes(nonce + tag + ciphertext)
        _VAULT_SESSION_FILE.chmod(0o600)
        logger.info("Vault session saved for auto-unlock")
    except Exception as exc:
        logger.warning("Failed to save vault session: %s", exc)


def _load_vault_session() -> str | None:
    """Load and decrypt password from vault session file. Returns None on failure."""
    try:
        if not _VAULT_SESSION_FILE.exists():
            return None
        raw = _VAULT_SESSION_FILE.read_bytes()
        if len(raw) < 12 + 16 + 1:
            return None
        nonce = raw[:12]
        tag = raw[12:28]
        ciphertext = raw[28:]
        key = _session_key()
        from app.crypto import _aes_gcm_decrypt
        plaintext = _aes_gcm_decrypt(key, nonce, ciphertext, tag)
        return plaintext.decode("utf-8")
    except Exception as exc:
        logger.debug("Vault session load failed (expected on first run): %s", exc)
        return None


def _delete_vault_session() -> None:
    """Remove vault session file."""
    try:
        if _VAULT_SESSION_FILE.exists():
            _VAULT_SESSION_FILE.unlink()
            logger.info("Vault session deleted")
    except Exception as exc:
        logger.warning("Failed to delete vault session: %s", exc)

# Keys that can be managed via the settings UI
_MANAGED_KEYS = [
    "extended_api_key", "extended_public_key", "extended_private_key", "extended_vault",
    "grvt_api_key", "grvt_private_key", "grvt_trading_account_id",
    "variational_jwt_token",
    "nado_private_key", "nado_linked_signer_key", "nado_wallet_address", "nado_subaccount_name",
]


def _mask(value: str) -> str:
    """Return masked version of a secret: show last 4 chars only."""
    if not value or len(value) <= 4:
        return "****" if value else ""
    return "***" + value[-4:]


def _load_secrets_plaintext() -> dict:
    """Load saved secrets from legacy plaintext secrets.json."""
    try:
        if _SECRETS_FILE_LEGACY.exists():
            return json.loads(_SECRETS_FILE_LEGACY.read_text())
    except Exception as exc:
        logger.warning("Failed to load legacy secrets file: %s", exc)
    return {}


def _load_secrets_encrypted(password: str) -> dict:
    """Load and decrypt secrets from secrets.enc. Raises ValueError on bad password."""
    if not _SECRETS_ENC_FILE.exists():
        return {}
    data = _SECRETS_ENC_FILE.read_bytes()
    return decrypt_secrets(data, password)


def _save_secrets_encrypted(secrets: dict, password: str) -> None:
    """Encrypt and save secrets to secrets.enc."""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = encrypt_secrets(secrets, password)
    _SECRETS_ENC_FILE.write_bytes(data)
    _SECRETS_ENC_FILE.chmod(0o600)


def _apply_secrets_to_settings(secrets: dict) -> None:
    """Apply saved secrets to in-memory settings object."""
    if not _settings:
        return
    for key in _MANAGED_KEYS:
        if key in secrets and secrets[key]:
            value = secrets[key]
            if key == "extended_vault":
                value = int(value)
            setattr(_settings, key, value)


def _reinit_exchange_clients() -> None:
    """Reinitialize exchange clients with current settings values."""
    global _client, _extended_client, _exchange_clients, _executor
    if not _settings:
        return
    # Reinit GRVT client
    _client = GrvtClient(_settings)
    _exchange_clients["grvt"] = _client
    # Reinit Extended client
    _extended_client = ExtendedClient(
        base_url=_settings.extended_api_base_url,
        api_key=_settings.extended_api_key,
        public_key=_settings.extended_public_key,
        private_key=_settings.extended_private_key,
        vault=_settings.extended_vault,
    )
    _exchange_clients["extended"] = _extended_client
    # Reinit Variational client if configured
    if _settings.variational_jwt_token:
        _exchange_clients["variational"] = VariationalClient(
            jwt_token=_settings.variational_jwt_token,
            proxy_worker_url=_settings.variational_proxy_url,
        )
    # Reinit NADO client if configured
    if _settings.nado_private_key or _settings.nado_linked_signer_key:
        _exchange_clients["nado"] = NadoClient(
            private_key=_settings.nado_private_key,
            subaccount_name=_settings.nado_subaccount_name,
            env=_settings.nado_env,
            linked_signer_key=_settings.nado_linked_signer_key,
            wallet_address=_settings.nado_wallet_address,
        )
    # Reinit executor (uses GRVT client)
    _executor = TradeExecutor(_client, _settings)
    logger.info("Exchange clients reinitialized after key update")


# ------------------------------------------------------------------
# NADO linked signer endpoints (wallet-connect authorization flow)
# ------------------------------------------------------------------

def _get_or_create_nado_client() -> "NadoClient":
    """Return the existing NADO client or create + persist one for the link flow."""
    client = _exchange_clients.get("nado")
    if not client:
        client = NadoClient(env=_settings.nado_env if _settings else "mainnet")
        _exchange_clients["nado"] = client
    return client


@app.post("/nado/prepare-link")
async def nado_prepare_link(body: dict):
    """Step 1: Generate trading key + EIP-712 typed data for MetaMask signing."""
    wallet_address = body.get("wallet_address", "").strip()
    if not wallet_address:
        raise HTTPException(status_code=400, detail="wallet_address required")
    subaccount_name = body.get("subaccount_name", "default").strip() or "default"

    client = _get_or_create_nado_client()

    try:
        result = client.prepare_link_signer(wallet_address, subaccount_name)
        return result
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/nado/submit-link")
async def nado_submit_link(body: dict):
    """Step 2: Submit the MetaMask signature to NADO and save the trading key."""
    signature = body.get("signature", "").strip()
    if not signature:
        raise HTTPException(status_code=400, detail="signature required")

    client = _get_or_create_nado_client()

    if not client._pending_link:
        raise HTTPException(status_code=409, detail="No pending link — call /nado/prepare-link first")

    try:
        result = client.submit_link_signer(signature)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # Save the trading key + wallet address to settings and encrypted secrets
    if _settings and result.get("status") == "success":
        _settings.nado_linked_signer_key = result["trading_key"]
        _settings.nado_wallet_address = result["wallet_address"]
        _settings.nado_subaccount_name = result["subaccount_name"]
        # Reinit NADO client with the new linked signer key
        _reinit_exchange_clients()
        # Persist to encrypted secrets if vault is unlocked
        if _current_password:
            try:
                secrets = _load_secrets_encrypted(_current_password)
            except Exception:
                secrets = {}
            secrets["nado_linked_signer_key"] = result["trading_key"]
            secrets["nado_wallet_address"] = result["wallet_address"]
            secrets["nado_subaccount_name"] = result["subaccount_name"]
            _save_secrets_encrypted(secrets, _current_password)
            # Verify the save round-trips correctly
            verify = _load_secrets_encrypted(_current_password)
            if verify.get("nado_linked_signer_key") == result["trading_key"]:
                logger.info("NADO linked signer key saved and verified in encrypted secrets")
            else:
                logger.error("NADO linked signer key save VERIFICATION FAILED — key may be lost on restart")
        else:
            logger.warning("NADO linked signer key NOT saved — vault is locked (_current_password is None)")

    # Don't return the trading key to the frontend
    return {
        "status": result.get("status"),
        "trading_address": result.get("trading_address"),
        "wallet_address": result.get("wallet_address"),
        "subaccount_name": result.get("subaccount_name"),
    }


@app.get("/nado/link-status")
async def nado_link_status():
    """Return current NADO linked signer status."""
    has_key = bool(_settings and _settings.nado_linked_signer_key)
    wallet = _settings.nado_wallet_address if _settings else ""
    subaccount = _settings.nado_subaccount_name if _settings else "default"

    # Try to query NADO for the actual linked signer
    remote_signer = None
    client = _exchange_clients.get("nado")
    if wallet and client:
        try:
            info = client.get_linked_signer(wallet, subaccount)
            remote_signer = info.get("linked_signer")
        except Exception:
            pass

    return {
        "has_trading_key": has_key,
        "wallet_address": wallet,
        "subaccount_name": subaccount,
        "remote_linked_signer": remote_signer,
    }


# ------------------------------------------------------------------
# Auth endpoints
# ------------------------------------------------------------------

@app.get("/auth/status")
async def auth_status():
    """Return current auth/vault state for the lock screen UI."""
    has_auth = _AUTH_FILE.exists()
    return {
        "setup_required": not has_auth,
        "locked": has_auth and not _vault_unlocked,
        "unlocked": _vault_unlocked,
    }


@app.post("/auth/setup")
async def auth_setup(body: dict):
    """Set the initial password. Only works if no auth.json exists yet."""
    if _AUTH_FILE.exists():
        raise HTTPException(status_code=409, detail="Password already set. Use /auth/unlock.")
    password = body.get("password", "").strip()
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    # Create auth file
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    auth_data = create_auth_file(password)
    _AUTH_FILE.write_text(json.dumps(auth_data, indent=2))
    _AUTH_FILE.chmod(0o600)
    # Create empty encrypted secrets file
    _save_secrets_encrypted({}, password)
    # Migrate legacy plaintext secrets if they exist
    global _current_password
    _current_password = password
    if _SECRETS_FILE_LEGACY.exists():
        try:
            legacy = _load_secrets_plaintext()
            if legacy:
                _save_secrets_encrypted(legacy, password)
                _apply_secrets_to_settings(legacy)
                logger.info("Migrated %d legacy secrets to encrypted store", len(legacy))
            # Remove legacy file after successful migration
            _SECRETS_FILE_LEGACY.rename(_SECRETS_FILE_LEGACY.with_suffix(".json.bak"))
        except Exception as exc:
            logger.warning("Failed to migrate legacy secrets: %s", exc)
    # Auto-unlock after setup
    await _init_exchange_clients()
    _save_vault_session(password)
    logger.info("Auth setup complete — vault unlocked")
    return {"status": "ok", "unlocked": True}


@app.post("/auth/unlock")
async def auth_unlock(body: dict):
    """Unlock the vault with the user password."""
    global _current_password
    if not _AUTH_FILE.exists():
        raise HTTPException(status_code=409, detail="No password set. Use /auth/setup first.")
    if _vault_unlocked:
        return {"status": "ok", "already_unlocked": True}
    password = body.get("password", "").strip()
    if not password:
        raise HTTPException(status_code=400, detail="Password required")
    # Verify password
    try:
        auth_data = json.loads(_AUTH_FILE.read_text())
    except Exception:
        raise HTTPException(status_code=500, detail="Corrupt auth file")
    if not verify_password(password, auth_data):
        raise HTTPException(status_code=401, detail="Wrong password")
    # Decrypt secrets and apply to settings
    try:
        secrets = _load_secrets_encrypted(password)
        _apply_secrets_to_settings(secrets)
    except ValueError:
        raise HTTPException(status_code=401, detail="Decryption failed — wrong password")
    except Exception as exc:
        logger.warning("Failed to load encrypted secrets: %s", exc)
    _current_password = password
    _save_vault_session(password)
    await _init_exchange_clients()
    return {"status": "ok", "unlocked": True}


@app.post("/internal/apply-keys")
async def internal_apply_keys(body: dict):
    """Receive plaintext API keys from the Worker (D1-backed) and initialize exchange clients.

    This endpoint is called by the Cloudflare Worker via the orchestrator proxy
    when keys are available in D1. No vault password needed.
    """
    keys = body.get("keys", {})
    if not keys:
        raise HTTPException(status_code=400, detail="No keys provided")
    if _vault_unlocked:
        # Vault already open — re-apply keys and reinit clients (handles new keys added after unlock)
        _apply_secrets_to_settings(keys)
        _reinit_exchange_clients()
        logger.info("Keys re-injected (vault was unlocked) — exchanges=%s", list(_exchange_clients.keys()))
        return {"status": "ok", "already_unlocked": True, "reinjected": True, "exchanges": list(_exchange_clients.keys())}
    # Apply keys to in-memory settings
    _apply_secrets_to_settings(keys)
    # Initialize exchange clients
    await _init_exchange_clients()
    logger.info("Keys injected via /internal/apply-keys — exchanges=%s", list(_exchange_clients.keys()))
    return {"status": "ok", "unlocked": True, "exchanges": list(_exchange_clients.keys())}


@app.post("/auth/lock")
async def auth_lock():
    """Lock the vault — shut down exchange clients and wipe keys from RAM."""
    global _current_password
    if not _vault_unlocked:
        return {"status": "ok", "already_locked": True}
    await _shutdown_exchange_clients()
    _current_password = None
    _delete_vault_session()
    # Clear sensitive settings in RAM
    if _settings:
        for key in _MANAGED_KEYS:
            setattr(_settings, key, "")
    return {"status": "ok", "locked": True}


# ------------------------------------------------------------------
# Settings / Key management endpoints
# ------------------------------------------------------------------

@app.get("/settings/keys")
async def get_keys():
    """Return masked API keys for display in the settings UI."""
    if not _vault_unlocked:
        raise HTTPException(status_code=403, detail="Vault is locked")
    return {
        "extended_api_key": _mask(_settings.extended_api_key),
        "extended_public_key": _mask(_settings.extended_public_key),
        "extended_private_key": _mask(_settings.extended_private_key),
        "extended_vault": str(_settings.extended_vault) if _settings.extended_vault else "",
        "grvt_api_key": _mask(_settings.grvt_api_key),
        "grvt_private_key": _mask(_settings.grvt_private_key),
        "grvt_trading_account_id": _mask(_settings.grvt_trading_account_id),
        "variational_jwt_token": _mask(_settings.variational_jwt_token),
    }


@app.post("/settings/keys")
async def update_keys(updates: dict):
    """Update API keys. Only non-empty values that differ from masked are saved."""
    if not _vault_unlocked or not _current_password:
        raise HTTPException(status_code=403, detail="Vault is locked")
    # Load current encrypted secrets
    try:
        secrets = _load_secrets_encrypted(_current_password)
    except Exception:
        secrets = {}
    changed = []
    for key, value in updates.items():
        if key not in _MANAGED_KEYS:
            continue
        # Skip masked values (user didn't change this field)
        if not value or value.startswith("***"):
            continue
        secrets[key] = value
        setattr(_settings, key, value)
        changed.append(key)
    if not changed:
        return {"status": "ok", "changed": []}
    _save_secrets_encrypted(secrets, _current_password)
    _reinit_exchange_clients()
    logger.info("API keys updated: %s", changed)
    return {"status": "ok", "changed": changed}


# ------------------------------------------------------------------
# Debug: Variational position query test
# ------------------------------------------------------------------

@app.get("/debug/variational-positions")
async def debug_variational_positions(
    iterations: int = 50,
    interval: float = 5.0,
    symbol: str = "P-TAO-USDC-3600",
):
    """Test Variational position queries exactly like the bot does.

    Runs N iterations with the live VariationalClient, logs every HTTP
    status code and response time. Use this to reproduce 403 errors.

    curl http://localhost:8005/debug/variational-positions?iterations=50&interval=5&symbol=P-TAO-USDC-3600
    """
    from starlette.responses import StreamingResponse
    import asyncio, time

    client = _exchange_clients.get("variational")
    if client is None:
        raise HTTPException(status_code=400, detail="Variational client not loaded (vault locked?)")

    async def _stream():
        success = 0
        errors = 0
        errors_403 = 0

        yield f"[START] symbol={symbol} iterations={iterations} interval={interval}s\n"
        yield f"[INFO]  wallet={client._wallet_address[:6]}...{client._wallet_address[-4:]}\n"
        yield f"[INFO]  URL={client._base_url}/positions\n"
        yield "=" * 70 + "\n"

        for i in range(1, iterations + 1):
            t0 = time.time()
            try:
                # Exact same call path as the bot: _sync_get → curl_cffi GET
                resp = client._cffi_session.get(
                    f"{client._base_url}/positions",
                    headers=client._headers(),
                    cookies=client._cookies(),
                    timeout=15,
                )
                elapsed = (time.time() - t0) * 1000
                status = resp.status_code

                if status == 200:
                    success += 1
                    data = resp.json()
                    # Parse positions — same as async_fetch_positions
                    positions = await client.async_fetch_positions(symbols=[symbol])
                    if positions:
                        p = positions[0]
                        yield f"[{i:4d}] {status} OK  {elapsed:6.0f}ms | {p['symbol']} size={p['size']:.6f} side={p['side']} entry={p['entry_price']:.4f}\n"
                    else:
                        n = len(data) if isinstance(data, list) else "?"
                        yield f"[{i:4d}] {status} OK  {elapsed:6.0f}ms | No match for {symbol} (total positions: {n})\n"
                elif status == 403:
                    errors_403 += 1
                    errors += 1
                    body = resp.text[:200]
                    yield f"[{i:4d}] {status} 403 {elapsed:6.0f}ms | >>> FORBIDDEN <<< {body}\n"
                else:
                    errors += 1
                    body = resp.text[:200]
                    yield f"[{i:4d}] {status} ERR {elapsed:6.0f}ms | {body}\n"

            except Exception as exc:
                elapsed = (time.time() - t0) * 1000
                errors += 1
                yield f"[{i:4d}] EXC     {elapsed:6.0f}ms | {type(exc).__name__}: {exc}\n"

            if i % 20 == 0:
                total = success + errors
                yield f"  --- Summary: {success}/{total} OK, {errors_403} x 403, {errors - errors_403} other ---\n"

            if i < iterations:
                await asyncio.sleep(interval)

        yield "=" * 70 + "\n"
        yield f"DONE: {success} OK, {errors_403} x 403, {errors - errors_403} other errors / {iterations} total\n"

    return StreamingResponse(_stream(), media_type="text/plain")


# ------------------------------------------------------------------
# WebUI
# ------------------------------------------------------------------

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/ui")
async def serve_ui():
    """Serve the WebUI dashboard."""
    return FileResponse(_STATIC_DIR / "index.html")
