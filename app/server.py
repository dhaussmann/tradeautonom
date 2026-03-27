"""FastAPI server — exposes endpoints for trade triggers and arbitrage."""

import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from app.arbitrage import ArbitrageEngine
from app.config import Settings
from app.executor import TradeExecutor
from app.extended_client import ExtendedClient
from app.grvt_client import GrvtClient
from app.ws_feeds import OrderbookFeedManager
from app.schemas import (
    ArbAutoRequest,
    ArbCheckResponse,
    ArbConfigRequest,
    ArbExecutionResponse,
    ArbLegInfo,
    ArbStatusResponse,
    ArbTriggerRequest,
    DepthInfo,
    SlippageInfo,
    SpreadInfo,
    SpreadRequest,
    SpreadResponse,
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
        spread=snapshot.spread,
        spread_abs=snapshot.spread_abs,
        a_is_cheaper=snapshot.a_is_cheaper,
        exec_spread=getattr(snapshot, 'exec_spread', 0.0),
        slippage_cost=getattr(snapshot, 'slippage_cost', 0.0),
        break_even_spread=getattr(snapshot, 'break_even_spread', 0.0),
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _settings, _client, _extended_client, _executor, _arb_engine, _exchange_clients, _feed_manager
    _settings = Settings()
    _client = GrvtClient(_settings)
    _extended_client = ExtendedClient(
        base_url=_settings.extended_api_base_url,
        api_key=_settings.extended_api_key,
        public_key=_settings.extended_public_key,
        private_key=_settings.extended_private_key,
        vault=_settings.extended_vault,
    )
    _exchange_clients = {"grvt": _client, "extended": _extended_client}
    _executor = TradeExecutor(_client, _settings)
    _arb_engine = ArbitrageEngine(_exchange_clients, _executor, _settings)
    _arb_engine.sync_position_from_exchange()
    # Start WebSocket orderbook feeds
    if _settings.arb_ws_enabled:
        _feed_manager = OrderbookFeedManager(
            grvt_env=_settings.grvt_env,
            stale_ms=_settings.arb_ws_stale_ms,
        )
        _feed_manager.start({
            _settings.arb_leg_a_exchange: _settings.arb_xau_instrument,
            _settings.arb_leg_b_exchange: _settings.arb_paxg_instrument,
        })
        _arb_engine.set_feed_manager(_feed_manager)
        logger.info("WebSocket feeds started for %s:%s + %s:%s",
                    _settings.arb_leg_a_exchange, _settings.arb_xau_instrument,
                    _settings.arb_leg_b_exchange, _settings.arb_paxg_instrument)
    logger.info("App started — GRVT env=%s, exchanges=%s", _settings.grvt_env, list(_exchange_clients.keys()))
    yield
    if _feed_manager is not None:
        _feed_manager.stop()
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

    snapshot = _arb_engine.get_spread_snapshot()
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
    snapshot = _arb_engine.get_spread_snapshot()
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

    result = _arb_engine.execute_signal(
        req.action,
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
    snapshot = _arb_engine.get_spread_snapshot()
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
        chunk_size=float(_arb_engine.chunk_size),
        chunk_delay_ms=_arb_engine.chunk_delay_ms,
        leg_a_exchange=_arb_engine.leg_a_exchange,
        leg_b_exchange=_arb_engine.leg_b_exchange,
        order_type=_arb_engine.order_type,
        limit_offset_ticks=_arb_engine.limit_offset_ticks,
        min_profit=_arb_engine.min_profit,
        fill_timeout_ms=_arb_engine.fill_timeout_ms,
        ws_enabled=_arb_engine._ws_enabled,
        ws_stale_ms=_arb_engine._ws_stale_ms,
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
    if req.min_profit is not None:
        _arb_engine.min_profit = req.min_profit
    if req.fill_timeout_ms is not None:
        _arb_engine.fill_timeout_ms = req.fill_timeout_ms
    if req.ws_enabled is not None:
        _arb_engine._ws_enabled = req.ws_enabled
    if req.ws_stale_ms is not None:
        _arb_engine._ws_stale_ms = req.ws_stale_ms
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
        raise HTTPException(status_code=500, detail="; ".join(errors))
    return all_positions


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
# WebUI
# ------------------------------------------------------------------

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/ui")
async def serve_ui():
    """Serve the WebUI dashboard."""
    return FileResponse(_STATIC_DIR / "index.html")
