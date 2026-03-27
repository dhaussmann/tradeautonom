"""Pydantic request/response schemas for the API."""

from pydantic import BaseModel, Field


# ------------------------------------------------------------------
# Single market order trigger
# ------------------------------------------------------------------

class TradeRequest(BaseModel):
    """Incoming request to execute a single market order."""
    symbol: str = Field(..., description="Instrument name, e.g. BTC_USDT_Perp")
    side: str = Field(..., description="'buy' or 'sell'")
    quantity: float = Field(..., gt=0, description="Order size in base asset units")
    expected_price: float = Field(..., gt=0, description="Price you expect to fill at")
    slippage_pct: float | None = Field(None, ge=0, description="Max slippage % override")
    min_depth_usd: float | None = Field(None, ge=0, description="Min book depth in USD override")


class DepthInfo(BaseModel):
    is_sufficient: bool
    available_depth_usd: float
    required_depth_usd: float
    best_price: float
    worst_fill_price: float
    levels_consumed: int


class SlippageInfo(BaseModel):
    is_acceptable: bool
    expected_price: float
    estimated_fill_price: float
    slippage_pct: float
    max_allowed_pct: float


class TradeResponse(BaseModel):
    success: bool
    order_response: dict | None = None
    depth: DepthInfo | None = None
    slippage: SlippageInfo | None = None
    error: str | None = None


# ------------------------------------------------------------------
# Arbitrage
# ------------------------------------------------------------------

class ArbTriggerRequest(BaseModel):
    """Trigger an arb entry or exit."""
    action: str = Field(..., description="'ENTRY' or 'EXIT'")
    spread_entry_low: float | None = Field(None, description="Override: enter when spread <= this")
    spread_exit_high: float | None = Field(None, description="Override: exit when spread >= this")
    quantity: float | None = Field(None, gt=0, description="Override quantity for both legs")
    min_depth_usd: float | None = Field(None, ge=0, description="Min book depth in USD override")
    slippage_pct: float | None = Field(None, ge=0, description="Max slippage % override")


class ArbAutoRequest(BaseModel):
    """Auto-arb: check spread conditions and execute if they are met."""
    quantity: float | None = Field(None, gt=0, description="Override quantity for both legs")
    spread_entry_low: float | None = Field(None, description="Override: enter when spread <= this")
    spread_exit_high: float | None = Field(None, description="Override: exit when spread >= this")
    min_depth_usd: float | None = Field(None, ge=0, description="Min book depth in USD override")
    slippage_pct: float | None = Field(None, ge=0, description="Max slippage % override")


class ArbConfigRequest(BaseModel):
    """Update arb engine configuration at runtime."""
    spread_entry_low: float | None = Field(None, description="Enter when spread <= this")
    spread_exit_high: float | None = Field(None, description="Exit when spread >= this")
    max_exec_spread: float | None = Field(None, ge=0, description="Max bid-ask execution cost")
    quantity: float | None = Field(None, gt=0, description="Quantity for both legs")
    min_depth_usd: float | None = Field(None, ge=0, description="Min book depth in USD")
    slippage_pct: float | None = Field(None, ge=0, description="Max slippage %")
    liquidity_multiplier: float | None = Field(None, ge=1.0, description="Min liquidity as multiple of qty")
    chunk_size: float | None = Field(None, gt=0, description="Order chunk size for splitting large orders")
    chunk_delay_ms: int | None = Field(None, ge=0, description="Delay between chunks in ms")
    instrument_a: str | None = Field(None, description="Instrument for leg A")
    instrument_b: str | None = Field(None, description="Instrument for leg B")
    leg_a_exchange: str | None = Field(None, description="Exchange for leg A (grvt or extended)")
    leg_b_exchange: str | None = Field(None, description="Exchange for leg B (grvt or extended)")
    simulation_mode: bool | None = Field(None, description="Paper-trade mode (no real orders)")
    order_type: str | None = Field(None, description="Order type: 'aggressive_limit' or 'market'")
    limit_offset_ticks: int | None = Field(None, ge=0, description="Ticks beyond best price for aggressive limit")
    min_profit: float | None = Field(None, ge=0, description="Min profit margin in USD above break-even")
    fill_timeout_ms: int | None = Field(None, ge=0, description="Max ms to wait for fill confirmation")
    ws_enabled: bool | None = Field(None, description="Use WebSocket feeds for orderbook data")
    ws_stale_ms: int | None = Field(None, ge=0, description="Max age (ms) before WS data is stale")


class ArbStatusResponse(BaseModel):
    """Full status of the arb engine for the dashboard."""
    has_position: bool
    spread_entry_low: float
    spread_exit_high: float
    max_exec_spread: float
    simulation_mode: bool
    quantity: float
    min_depth_usd: float
    slippage_pct: float
    instrument_a: str
    instrument_b: str
    liquidity_multiplier: float
    chunk_size: float
    chunk_delay_ms: int
    leg_a_exchange: str
    leg_b_exchange: str
    order_type: str
    limit_offset_ticks: int
    min_profit: float
    fill_timeout_ms: int
    ws_enabled: bool
    ws_stale_ms: int
    long_sym: str | None = None
    short_sym: str | None = None
    entry_spread_actual: float | None = None


class SpreadInfo(BaseModel):
    instrument_a: str
    instrument_b: str
    mid_price_a: float
    mid_price_b: float
    spread: float
    spread_abs: float
    a_is_cheaper: bool
    exec_spread: float = 0.0
    slippage_cost: float = 0.0
    break_even_spread: float = 0.0
    data_source: str = "rest"


class ArbCheckResponse(BaseModel):
    action: str
    snapshot: SpreadInfo
    reason: str


class ArbLegInfo(BaseModel):
    success: bool
    error: str | None = None


class ArbExecutionResponse(BaseModel):
    success: bool
    leg_a: ArbLegInfo | None = None
    leg_b: ArbLegInfo | None = None
    snapshot: SpreadInfo
    error: str | None = None


# ------------------------------------------------------------------
# Spread query
# ------------------------------------------------------------------

class SpreadRequest(BaseModel):
    instrument_a: str | None = None
    instrument_b: str | None = None


class SpreadResponse(BaseModel):
    snapshot: SpreadInfo
    recommended_action: str
    reason: str
