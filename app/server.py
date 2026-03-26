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
from app.grvt_client import GrvtClient
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
    )

# Module-level singletons (populated in lifespan)
_settings: Settings | None = None
_client: GrvtClient | None = None
_executor: TradeExecutor | None = None
_arb_engine: ArbitrageEngine | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _settings, _client, _executor, _arb_engine
    _settings = Settings()
    _client = GrvtClient(_settings)
    _executor = TradeExecutor(_client, _settings)
    _arb_engine = ArbitrageEngine(_client, _executor, _settings)
    _arb_engine.sync_position_from_exchange()
    logger.info("App started — GRVT env=%s", _settings.grvt_env)
    yield
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
    if req.simulation_mode is not None:
        _arb_engine.simulation_mode = req.simulation_mode
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
    """Return the current sub-account summary."""
    try:
        return _client.get_account_summary()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/account/positions")
async def account_positions(symbols: str = ""):
    """Return open positions. Pass comma-separated symbols or leave empty for all."""
    sym_list = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else []
    try:
        return _client.fetch_positions(sym_list)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


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
# WebUI
# ------------------------------------------------------------------

_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


@app.get("/ui")
async def serve_ui():
    """Serve the WebUI dashboard."""
    return FileResponse(_STATIC_DIR / "index.html")
