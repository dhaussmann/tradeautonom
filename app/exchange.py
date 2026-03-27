"""Exchange client protocol — common interface for GRVT, Extended, etc.

Design: method signatures work for both REST (fetch on demand) and
future WebSocket implementations (return cached state).
"""

from typing import Protocol, runtime_checkable


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

    def get_min_order_size(self, symbol: str) -> "Decimal":
        """Return minimum order size for the symbol (0 if no minimum / unknown)."""
        ...

    def create_aggressive_limit_order(
        self, symbol: str, side: str, amount: "Decimal", offset_ticks: int = 2,
        best_price: float | None = None,
    ) -> dict:
        """Place an aggressive limit order: best price ± offset ticks."""
        ...

    def check_order_fill(self, order_id) -> dict:
        """Check if an order has been filled. Returns {filled: bool, status: str}."""
        ...
