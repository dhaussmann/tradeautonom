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
