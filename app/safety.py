"""Pre-trade safety checks: order-book depth, slippage, and price validation."""

import logging
from dataclasses import dataclass
from decimal import Decimal

logger = logging.getLogger("tradeautonom.safety")


@dataclass
class DepthResult:
    """Result of an order-book depth check."""
    is_sufficient: bool
    available_depth_usd: float
    required_depth_usd: float
    best_price: float
    worst_fill_price: float
    levels_consumed: int


@dataclass
class SlippageResult:
    """Result of a slippage validation."""
    is_acceptable: bool
    expected_price: float
    estimated_fill_price: float
    slippage_pct: float
    max_allowed_pct: float


def check_order_book_depth(
    order_book: dict,
    side: str,
    quantity: Decimal,
    min_depth_usd: float,
) -> DepthResult:
    """Verify that enough liquidity exists on the relevant side.

    For a BUY we consume the asks (ascending); for a SELL the bids (descending).
    Returns a DepthResult with pass/fail and diagnostics.
    """
    levels = order_book.get("asks" if side == "buy" else "bids", [])
    if not levels:
        return DepthResult(
            is_sufficient=False,
            available_depth_usd=0.0,
            required_depth_usd=min_depth_usd,
            best_price=0.0,
            worst_fill_price=0.0,
            levels_consumed=0,
        )

    remaining = float(quantity)
    total_cost = 0.0
    levels_consumed = 0
    best_price = float(levels[0][0])
    worst_fill_price = best_price

    for price_str, size_str in levels:
        price = float(price_str)
        size = float(size_str)
        fill = min(remaining, size)
        total_cost += fill * price
        remaining -= fill
        levels_consumed += 1
        worst_fill_price = price
        if remaining <= 0:
            break

    filled_qty = float(quantity) - remaining
    available_depth_usd = total_cost

    is_sufficient = remaining <= 0 and available_depth_usd >= min_depth_usd

    result = DepthResult(
        is_sufficient=is_sufficient,
        available_depth_usd=round(available_depth_usd, 2),
        required_depth_usd=min_depth_usd,
        best_price=best_price,
        worst_fill_price=worst_fill_price,
        levels_consumed=levels_consumed,
    )
    logger.info("Depth check: %s", result)
    return result


def estimate_fill_price(
    order_book: dict,
    side: str,
    quantity: Decimal,
) -> float:
    """Walk the book and return the volume-weighted average fill price."""
    levels = order_book.get("asks" if side == "buy" else "bids", [])
    remaining = float(quantity)
    total_cost = 0.0

    for price_str, size_str in levels:
        price = float(price_str)
        size = float(size_str)
        fill = min(remaining, size)
        total_cost += fill * price
        remaining -= fill
        if remaining <= 0:
            break

    filled_qty = float(quantity) - remaining
    if filled_qty == 0:
        return 0.0
    return total_cost / filled_qty


def check_slippage(
    order_book: dict,
    side: str,
    quantity: Decimal,
    expected_price: float,
    max_slippage_pct: float,
) -> SlippageResult:
    """Compare the estimated fill price against the expected price.

    Slippage is measured as:
      BUY:  (fill_price - expected_price) / expected_price * 100
      SELL: (expected_price - fill_price) / expected_price * 100
    A positive value means adverse slippage.
    """
    fill_price = estimate_fill_price(order_book, side, quantity)

    if expected_price == 0 or fill_price == 0:
        return SlippageResult(
            is_acceptable=False,
            expected_price=expected_price,
            estimated_fill_price=fill_price,
            slippage_pct=100.0,
            max_allowed_pct=max_slippage_pct,
        )

    if side == "buy":
        slippage_pct = (fill_price - expected_price) / expected_price * 100
    else:
        slippage_pct = (expected_price - fill_price) / expected_price * 100

    result = SlippageResult(
        is_acceptable=slippage_pct <= max_slippage_pct,
        expected_price=expected_price,
        estimated_fill_price=round(fill_price, 6),
        slippage_pct=round(slippage_pct, 4),
        max_allowed_pct=max_slippage_pct,
    )
    logger.info("Slippage check: %s", result)
    return result


@dataclass
class LiquidityResult:
    """Result of an order-book liquidity (quantity) check."""
    symbol: str
    side: str
    available_qty: float
    required_qty: float
    is_sufficient: bool
    worst_fill_price: float


def check_book_quantity(
    order_book: dict,
    side: str,
    required_qty: float,
) -> LiquidityResult:
    """Check how much quantity is available on one side of the book.

    For a BUY we consume asks; for a SELL we consume bids.
    Returns whether >= required_qty is available and at what worst price.
    """
    levels = order_book.get("asks" if side == "buy" else "bids", [])
    total_qty = 0.0
    worst_price = 0.0
    for price_str, size_str in levels:
        total_qty += float(size_str)
        worst_price = float(price_str)
        if total_qty >= required_qty:
            break
    return LiquidityResult(
        symbol="",  # filled by caller
        side=side,
        available_qty=round(total_qty, 6),
        required_qty=required_qty,
        is_sufficient=total_qty >= required_qty,
        worst_fill_price=worst_price,
    )


def check_dual_liquidity(
    book_long: dict,
    book_short: dict,
    quantity: float,
    multiplier: float,
    long_symbol: str,
    short_symbol: str,
) -> tuple[bool, LiquidityResult, LiquidityResult]:
    """Pre-entry check: verify BOTH order books have enough liquidity.

    Args:
        book_long: Order book of the instrument to go LONG (we buy asks).
        book_short: Order book of the instrument to go SHORT (we sell bids).
        quantity: The trade quantity for each leg.
        multiplier: Required available qty = quantity * multiplier (e.g. 2.0).
        long_symbol: Name for logging.
        short_symbol: Name for logging.

    Returns:
        (passed, long_result, short_result)
    """
    required = quantity * multiplier

    long_liq = check_book_quantity(book_long, "buy", required)
    long_liq.symbol = long_symbol

    short_liq = check_book_quantity(book_short, "sell", required)
    short_liq.symbol = short_symbol

    passed = long_liq.is_sufficient and short_liq.is_sufficient

    if not passed:
        reasons = []
        if not long_liq.is_sufficient:
            reasons.append(
                f"LONG {long_symbol}: {long_liq.available_qty:.4f} available, "
                f"need {required:.4f} ({multiplier:.1f}x {quantity})"
            )
        if not short_liq.is_sufficient:
            reasons.append(
                f"SHORT {short_symbol}: {short_liq.available_qty:.4f} available, "
                f"need {required:.4f} ({multiplier:.1f}x {quantity})"
            )
        logger.warning("Dual liquidity check FAILED: %s", "; ".join(reasons))
    else:
        logger.info(
            "Dual liquidity check PASSED: LONG %s %.4f avail (need %.4f), "
            "SHORT %s %.4f avail (need %.4f)",
            long_symbol, long_liq.available_qty, required,
            short_symbol, short_liq.available_qty, required,
        )

    return passed, long_liq, short_liq


def run_pre_trade_checks(
    order_book: dict,
    side: str,
    quantity: Decimal,
    expected_price: float,
    max_slippage_pct: float,
    min_depth_usd: float,
) -> tuple[bool, DepthResult, SlippageResult]:
    """Run all pre-trade safety checks. Returns (pass, depth, slippage)."""
    depth = check_order_book_depth(order_book, side, quantity, min_depth_usd)
    slippage = check_slippage(order_book, side, quantity, expected_price, max_slippage_pct)

    passed = depth.is_sufficient and slippage.is_acceptable
    if not passed:
        reasons = []
        if not depth.is_sufficient:
            reasons.append(
                f"Insufficient depth: {depth.available_depth_usd:.2f} USD "
                f"(need {depth.required_depth_usd:.2f})"
            )
        if not slippage.is_acceptable:
            reasons.append(
                f"Slippage too high: {slippage.slippage_pct:.4f}% "
                f"(max {slippage.max_allowed_pct:.2f}%)"
            )
        logger.warning("Pre-trade checks FAILED: %s", "; ".join(reasons))
    else:
        logger.info("Pre-trade checks PASSED")

    return passed, depth, slippage
