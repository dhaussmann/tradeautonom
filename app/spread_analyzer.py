"""Cross-venue execution spread analysis using orderbook depth.

Replaces BBO-only spread comparison with VWAP-based fill price simulation
to account for slippage on thin DEX orderbooks.

Opt-in via fn_opt_depth_spread feature flag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from app.safety import estimate_fill_price

logger = logging.getLogger("tradeautonom.spread_analyzer")


@dataclass
class SpreadAnalysis:
    """Result of a cross-venue execution spread analysis."""
    bbo_spread_pct: float       # BBO-only spread (asks[0] vs bids[0])
    exec_spread_pct: float      # VWAP execution spread (fill prices)
    slippage_bps: float         # additional slippage beyond BBO (basis points)
    is_acceptable: bool         # slippage within budget?
    long_fill_price: float      # estimated VWAP fill price for long (buy) side
    short_fill_price: float     # estimated VWAP fill price for short (sell) side
    long_bbo: float             # best ask on long side
    short_bbo: float            # best bid on short side


def analyze_cross_venue_spread(
    long_book: dict,
    short_book: dict,
    quantity: Decimal,
    max_slippage_bps: float = 10.0,
) -> SpreadAnalysis | None:
    """Analyze the real execution spread between two venues.

    Args:
        long_book: Orderbook dict for the long (buy) side.
                   Must have "asks" and "bids" as [[price, qty], ...].
        short_book: Orderbook dict for the short (sell) side.
        quantity: Trade size in base asset units.
        max_slippage_bps: Maximum acceptable additional slippage
                          beyond BBO spread, in basis points.

    Returns:
        SpreadAnalysis or None if books are too shallow.
    """
    long_asks = long_book.get("asks", [])
    short_bids = short_book.get("bids", [])

    if not long_asks or not short_bids:
        return None

    # BBO prices
    long_bbo = float(long_asks[0][0])
    short_bbo = float(short_bids[0][0])

    if short_bbo <= 0:
        return None

    # VWAP fill prices
    long_fill = estimate_fill_price(long_book, "buy", quantity)
    short_fill = estimate_fill_price(short_book, "sell", quantity)

    if long_fill <= 0 or short_fill <= 0:
        logger.debug(
            "spread_analyzer: insufficient depth — long_fill=%.4f short_fill=%.4f qty=%s",
            long_fill, short_fill, quantity,
        )
        return None

    # Spread calculations
    bbo_spread_pct = (long_bbo - short_bbo) / short_bbo * 100
    exec_spread_pct = (long_fill - short_fill) / short_fill * 100

    # Slippage = how much worse the execution spread is vs BBO
    slippage_bps = (exec_spread_pct - bbo_spread_pct) * 100

    is_acceptable = slippage_bps <= max_slippage_bps

    return SpreadAnalysis(
        bbo_spread_pct=bbo_spread_pct,
        exec_spread_pct=exec_spread_pct,
        slippage_bps=slippage_bps,
        is_acceptable=is_acceptable,
        long_fill_price=long_fill,
        short_fill_price=short_fill,
        long_bbo=long_bbo,
        short_bbo=short_bbo,
    )
