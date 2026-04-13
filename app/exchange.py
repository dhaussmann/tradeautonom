"""Exchange client protocol — common interface for GRVT, Extended, Nado.

Design: method signatures work for both REST (fetch on demand) and
future WebSocket implementations (return cached state).

Two protocols are provided:
  - ExchangeClient: legacy synchronous interface (kept for migration)
  - AsyncExchangeClient: new async interface for the funding-arb engine
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable, Coroutine, Protocol, runtime_checkable


# ──────────────────────────────────────────────────────────────────────
# Legacy synchronous protocol (kept during migration, will be removed)
# ──────────────────────────────────────────────────────────────────────

@runtime_checkable
class ExchangeClient(Protocol):
    """Minimal interface every exchange adapter must implement."""

    @property
    def name(self) -> str:
        """Short identifier, e.g. 'grvt' or 'extended'."""
        ...

    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        """Return normalised order book.

        Format:
            {
                "bids": [[price_str, qty_str], ...],   # best first
                "asks": [[price_str, qty_str], ...],   # best first
            }
        """
        ...

    def fetch_markets(self) -> list[dict]:
        """Return list of available markets / instruments.

        Each dict must contain at least:
            {"symbol": "BTC_USDT_Perp", "name": "BTC/USDT Perp", ...}
        """
        ...

    def get_min_order_size(self, symbol: str) -> Decimal:
        """Return minimum order size for the symbol (0 if no minimum / unknown)."""
        ...

    def create_aggressive_limit_order(
        self, symbol: str, side: str, amount: Decimal, offset_ticks: int = 2,
        best_price: float | None = None, limit_price: float | None = None,
    ) -> dict:
        """Place an aggressive limit order: best price ± offset ticks.

        If limit_price is provided (e.g. from VWAP), use it directly.
        """
        ...

    def check_order_fill(self, order_id) -> dict:
        """Check if an order has been filled. Returns {filled: bool, status: str}."""
        ...


# ──────────────────────────────────────────────────────────────────────
# New async protocol for the Funding-Arb Maker-Taker engine
# ──────────────────────────────────────────────────────────────────────

# Type alias for async fill / funding-rate / position callbacks
FillCallback = Callable[[dict], Coroutine[Any, Any, None]]
FundingRateCallback = Callable[[dict], Coroutine[Any, Any, None]]
PositionCallback = Callable[[dict], Coroutine[Any, Any, None]]


@runtime_checkable
class AsyncExchangeClient(Protocol):
    """Async interface for the new funding-arb engine.

    All network-bound methods are async.  Clients must implement this
    protocol to be used with the Maker-Taker state machine.
    """

    @property
    def name(self) -> str:
        """Short identifier, e.g. 'grvt', 'extended', 'nado'."""
        ...

    # ── Market data ───────────────────────────────────────────────────

    async def async_fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        """Return normalised order book (async).

        Format: {"bids": [[price, qty], ...], "asks": [[price, qty], ...]}
        """
        ...

    async def async_fetch_markets(self) -> list[dict]:
        """Return list of available markets / instruments (async)."""
        ...

    async def async_get_min_order_size(self, symbol: str) -> Decimal:
        """Return minimum order size for the symbol (async)."""
        ...

    async def async_get_tick_size(self, symbol: str) -> Decimal:
        """Return price tick size for the symbol (async)."""
        ...

    # ── Order management ──────────────────────────────────────────────

    async def async_create_post_only_order(
        self,
        symbol: str,
        side: str,
        amount: Decimal,
        price: Decimal,
    ) -> dict:
        """Place a GTT post-only limit order (maker only).

        Returns at minimum: {"id": order_id, ...}
        The order is rejected if it would cross the book (take liquidity).
        """
        ...

    async def async_create_ioc_order(
        self,
        symbol: str,
        side: str,
        amount: Decimal,
        price: Decimal,
    ) -> dict:
        """Place an IOC (Immediate-or-Cancel) limit order (taker).

        Returns at minimum: {"id": order_id, "traded_qty": float, ...}
        """
        ...

    async def async_cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID. Returns True if cancelled successfully."""
        ...

    async def async_check_order_fill(self, order_id: str) -> dict:
        """Check order fill status. Returns {filled: bool, status: str, traded_qty: float, ...}."""
        ...

    # ── Positions ─────────────────────────────────────────────────────

    async def async_fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        """Fetch open positions (async)."""
        ...

    # ── Funding rate ──────────────────────────────────────────────────

    async def async_fetch_funding_rate(self, symbol: str) -> dict:
        """Fetch the current/latest funding rate for a perpetual symbol.

        Returns: {"symbol": str, "funding_rate": float, "next_funding_time": str | None, ...}
        """
        ...

    # ── WebSocket subscriptions ───────────────────────────────────────

    async def async_subscribe_fills(
        self, symbol: str, callback: FillCallback,
    ) -> None:
        """Subscribe to real-time fill events via WebSocket.

        callback receives dicts with at minimum:
            {"order_id": str, "filled_qty": float, "remaining_qty": float,
             "price": float, "is_taker": bool}
        """
        ...

    async def async_subscribe_funding_rate(
        self, symbol: str, callback: FundingRateCallback,
    ) -> None:
        """Subscribe to real-time funding rate updates via WebSocket.

        callback receives dicts with at minimum:
            {"symbol": str, "funding_rate": float, "timestamp": str}
        """
        ...
