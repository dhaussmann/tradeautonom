"""Dynamic position sizing based on capital, per-pair limits, and orderbook liquidity.

Uses binary search to find the maximum tradeable quantity that keeps
slippage within the configured budget on both sides of the trade.

Opt-in via fn_opt_dynamic_sizing feature flag.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal

from app.safety import estimate_fill_price

logger = logging.getLogger("tradeautonom.position_sizer")


@dataclass
class SizingResult:
    """Output of the dynamic position sizer."""
    recommended_qty: Decimal
    reason: str
    capital_limit: Decimal      # max from capital constraint
    per_pair_limit: Decimal     # max from per-pair ratio constraint
    liquidity_limit: Decimal    # max from orderbook slippage constraint
    capped_by: str              # which constraint was binding: "capital", "per_pair", "liquidity"


def compute_position_size(
    collateral_usd: float,
    leverage: float,
    max_utilization: float,
    max_per_pair_ratio: float,
    mark_price: float,
    long_book: dict | None = None,
    short_book: dict | None = None,
    max_slippage_bps: float = 10.0,
    min_qty: float = 0.001,
) -> SizingResult:
    """Compute optimal position size respecting capital and liquidity constraints.

    Args:
        collateral_usd: Total available collateral in USD.
        leverage: Applied leverage multiplier.
        max_utilization: Max fraction of collateral to use (0-1).
        max_per_pair_ratio: Max fraction of collateral per single pair (0-1).
        mark_price: Current mark/mid price of the asset.
        long_book: Orderbook dict for the long (buy) side. Optional.
        short_book: Orderbook dict for the short (sell) side. Optional.
        max_slippage_bps: Max acceptable slippage in basis points.
        min_qty: Minimum order quantity (exchange-specific).

    Returns:
        SizingResult with the recommended quantity and constraint details.
    """
    if mark_price <= 0 or collateral_usd <= 0:
        return SizingResult(
            recommended_qty=Decimal(str(min_qty)),
            reason="Invalid price or collateral",
            capital_limit=Decimal("0"),
            per_pair_limit=Decimal("0"),
            liquidity_limit=Decimal("0"),
            capped_by="error",
        )

    # Capital constraint: max notional / mark_price
    max_notional = collateral_usd * leverage * max_utilization
    capital_qty = max_notional / mark_price

    # Per-pair constraint
    per_pair_notional = collateral_usd * leverage * max_per_pair_ratio
    per_pair_qty = per_pair_notional / mark_price

    capital_limit = Decimal(str(round(capital_qty, 8)))
    per_pair_limit = Decimal(str(round(per_pair_qty, 8)))

    # Liquidity constraint via binary search
    liquidity_qty = min(capital_qty, per_pair_qty)  # start from the tighter capital constraint

    if long_book and short_book:
        liquidity_qty = _binary_search_max_qty(
            long_book=long_book,
            short_book=short_book,
            upper_bound=float(min(capital_limit, per_pair_limit)),
            max_slippage_bps=max_slippage_bps,
            min_qty=min_qty,
            mark_price=mark_price,
        )

    liquidity_limit = Decimal(str(round(liquidity_qty, 8)))

    # Final: minimum of all three constraints
    final_qty = min(capital_limit, per_pair_limit, liquidity_limit)
    final_qty = max(final_qty, Decimal(str(min_qty)))

    # Determine binding constraint
    if final_qty == liquidity_limit and liquidity_limit < min(capital_limit, per_pair_limit):
        capped_by = "liquidity"
    elif final_qty == per_pair_limit and per_pair_limit <= capital_limit:
        capped_by = "per_pair"
    else:
        capped_by = "capital"

    reason = (
        f"capital={capital_limit:.4f} per_pair={per_pair_limit:.4f} "
        f"liquidity={liquidity_limit:.4f} → {final_qty:.4f} (capped by {capped_by})"
    )

    logger.info("PositionSizer: %s", reason)

    return SizingResult(
        recommended_qty=final_qty,
        reason=reason,
        capital_limit=capital_limit,
        per_pair_limit=per_pair_limit,
        liquidity_limit=liquidity_limit,
        capped_by=capped_by,
    )


def _binary_search_max_qty(
    long_book: dict,
    short_book: dict,
    upper_bound: float,
    max_slippage_bps: float,
    min_qty: float,
    mark_price: float,
    iterations: int = 10,
) -> float:
    """Binary search for the largest quantity where slippage stays within budget.

    Checks both sides: buy slippage on long_book and sell slippage on short_book.
    """
    lo = min_qty
    hi = upper_bound

    if hi <= lo:
        return lo

    for _ in range(iterations):
        mid = (lo + hi) / 2.0
        qty = Decimal(str(round(mid, 8)))

        slip_ok = _check_both_sides_slippage(
            long_book, short_book, qty, mark_price, max_slippage_bps
        )

        if slip_ok:
            lo = mid  # can go bigger
        else:
            hi = mid  # too much slippage, go smaller

    return lo


def _check_both_sides_slippage(
    long_book: dict,
    short_book: dict,
    quantity: Decimal,
    mark_price: float,
    max_slippage_bps: float,
) -> bool:
    """Check if slippage on both sides is within the budget."""
    # Buy side (long): fill price should not be too far above mark
    buy_fill = estimate_fill_price(long_book, "buy", quantity)
    if buy_fill <= 0:
        return False
    buy_slip_bps = (buy_fill - mark_price) / mark_price * 10000

    # Sell side (short): fill price should not be too far below mark
    sell_fill = estimate_fill_price(short_book, "sell", quantity)
    if sell_fill <= 0:
        return False
    sell_slip_bps = (mark_price - sell_fill) / mark_price * 10000

    # Both must be within budget
    return buy_slip_bps <= max_slippage_bps and sell_slip_bps <= max_slippage_bps
