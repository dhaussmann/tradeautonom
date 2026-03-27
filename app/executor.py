"""Trade executor: validates safety checks and places orders on any exchange."""

import logging
from dataclasses import dataclass
from decimal import Decimal

from app.config import Settings
from app.exchange import ExchangeClient
from app.safety import DepthResult, SlippageResult, run_pre_trade_checks

logger = logging.getLogger("tradeautonom.executor")


@dataclass
class TradeResult:
    success: bool
    order_response: dict | None
    depth: DepthResult | None
    slippage: SlippageResult | None
    error: str | None


class TradeExecutor:
    """Validates pre-trade conditions and executes market orders on any exchange."""

    def __init__(self, client: ExchangeClient, settings: Settings) -> None:
        self.client = client
        self.settings = settings

    def execute_market_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        expected_price: float,
        slippage_pct: float | None = None,
        min_depth_usd: float | None = None,
        client: ExchangeClient | None = None,
    ) -> TradeResult:
        """Run safety checks and place a market order if everything passes.

        Args:
            symbol: Instrument name, e.g. "BTC_USDT_Perp".
            side: "buy" or "sell".
            quantity: Order size in base asset units.
            expected_price: The price you expect to fill at.
            slippage_pct: Max acceptable slippage %. Falls back to config default.
            min_depth_usd: Min required book depth in USD. Falls back to config default.

        Returns:
            TradeResult with outcome details.
        """
        active_client = client or self.client

        side = side.lower()
        if side not in ("buy", "sell"):
            return TradeResult(
                success=False, order_response=None, depth=None, slippage=None,
                error=f"Invalid side: {side}. Must be 'buy' or 'sell'.",
            )

        max_slip = slippage_pct if slippage_pct is not None else self.settings.default_slippage_pct
        depth_req = min_depth_usd if min_depth_usd is not None else self.settings.min_order_book_depth_usd

        # Cap slippage at configured maximum
        if max_slip > self.settings.max_slippage_pct:
            logger.warning(
                "Requested slippage %.2f%% exceeds max %.2f%%, capping.",
                max_slip, self.settings.max_slippage_pct,
            )
            max_slip = self.settings.max_slippage_pct

        # Fetch order book from the correct exchange
        try:
            order_book = active_client.fetch_order_book(symbol, limit=50)
        except Exception as exc:
            msg = f"Failed to fetch order book for {symbol}: {exc}"
            logger.error(msg)
            return TradeResult(success=False, order_response=None, depth=None, slippage=None, error=msg)

        # Run pre-trade checks
        passed, depth, slippage = run_pre_trade_checks(
            order_book=order_book,
            side=side,
            quantity=quantity,
            expected_price=expected_price,
            max_slippage_pct=max_slip,
            min_depth_usd=depth_req,
        )

        if not passed:
            reasons = []
            if not depth.is_sufficient:
                reasons.append(
                    f"Depth insufficient: {depth.available_depth_usd:.2f} USD "
                    f"available, {depth.required_depth_usd:.2f} required"
                )
            if not slippage.is_acceptable:
                reasons.append(
                    f"Slippage {slippage.slippage_pct:.4f}% exceeds max {slippage.max_allowed_pct:.2f}%"
                )
            error_msg = "Pre-trade checks failed: " + "; ".join(reasons)
            logger.warning(error_msg)
            return TradeResult(
                success=False, order_response=None,
                depth=depth, slippage=slippage, error=error_msg,
            )

        # Place the order on the correct exchange, passing slippage so exchanges
        # that emulate market orders (e.g. Extended IOC limit) use the same tolerance
        try:
            resp = active_client.create_market_order(
                symbol=symbol,
                side=side,
                amount=quantity,
                slippage_pct=max_slip,
            )
        except Exception as exc:
            msg = f"Order placement failed: {exc}"
            logger.error(msg)
            return TradeResult(success=False, order_response=None, depth=depth, slippage=slippage, error=msg)

        logger.info("Order executed successfully: %s", resp)
        return TradeResult(
            success=True,
            order_response=resp,
            depth=depth,
            slippage=slippage,
            error=None,
        )

    def execute_aggressive_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        expected_price: float,
        offset_ticks: int = 2,
        slippage_pct: float | None = None,
        min_depth_usd: float | None = None,
        client: ExchangeClient | None = None,
    ) -> TradeResult:
        """Run safety checks and place an aggressive limit order.

        Same pre-trade validation as market orders, but uses
        create_aggressive_limit_order for tighter price control.
        """
        active_client = client or self.client

        side = side.lower()
        if side not in ("buy", "sell"):
            return TradeResult(
                success=False, order_response=None, depth=None, slippage=None,
                error=f"Invalid side: {side}. Must be 'buy' or 'sell'.",
            )

        max_slip = slippage_pct if slippage_pct is not None else self.settings.default_slippage_pct
        depth_req = min_depth_usd if min_depth_usd is not None else self.settings.min_order_book_depth_usd

        if max_slip > self.settings.max_slippage_pct:
            max_slip = self.settings.max_slippage_pct

        try:
            order_book = active_client.fetch_order_book(symbol, limit=50)
        except Exception as exc:
            msg = f"Failed to fetch order book for {symbol}: {exc}"
            logger.error(msg)
            return TradeResult(success=False, order_response=None, depth=None, slippage=None, error=msg)

        passed, depth, slippage = run_pre_trade_checks(
            order_book=order_book, side=side, quantity=quantity,
            expected_price=expected_price,
            max_slippage_pct=max_slip, min_depth_usd=depth_req,
        )

        if not passed:
            reasons = []
            if not depth.is_sufficient:
                reasons.append(f"Depth insufficient: {depth.available_depth_usd:.2f} USD")
            if not slippage.is_acceptable:
                reasons.append(f"Slippage {slippage.slippage_pct:.4f}% exceeds max {slippage.max_allowed_pct:.2f}%")
            error_msg = "Pre-trade checks failed: " + "; ".join(reasons)
            logger.warning(error_msg)
            return TradeResult(success=False, order_response=None, depth=depth, slippage=slippage, error=error_msg)

        # Use best price from already-fetched book to avoid a redundant order book request
        levels = order_book.get("asks" if side == "buy" else "bids", [])
        best_price = float(levels[0][0]) if levels else None

        try:
            resp = active_client.create_aggressive_limit_order(
                symbol=symbol, side=side, amount=quantity,
                offset_ticks=offset_ticks, best_price=best_price,
            )
        except Exception as exc:
            msg = f"Aggressive limit order failed: {exc}"
            logger.error(msg)
            return TradeResult(success=False, order_response=None, depth=depth, slippage=slippage, error=msg)

        # Check fill confirmation if the client supports it
        state = resp.get("state", {}) if isinstance(resp, dict) else {}
        status = state.get("status", "")
        traded = state.get("traded_size", ["0"])
        traded_qty = float(traded[0]) if traded else 0.0

        if status and status not in ("FILLED", "CLOSED") and traded_qty == 0.0:
            # Order is PENDING with 0 fill — IOC likely expired unfilled
            # Try to confirm via check_order_fill if available
            cid = resp.get("metadata", {}).get("client_order_id") if isinstance(resp, dict) else None
            if cid and hasattr(active_client, "check_order_fill"):
                import time as _time
                _time.sleep(0.5)  # brief wait for exchange to process IOC
                fill_check = active_client.check_order_fill(int(cid))
                if not fill_check.get("filled"):
                    msg = (
                        f"Order {side.upper()} {symbol} placed but not filled "
                        f"(status={fill_check.get('status')} traded={traded_qty}) — "
                        f"IOC order expired or rejected"
                    )
                    logger.error(msg)
                    return TradeResult(success=False, order_response=resp, depth=depth, slippage=slippage, error=msg)
                logger.info("Fill confirmed via check_order_fill: %s", fill_check)

        logger.info("Aggressive limit order executed: symbol=%s side=%s status=%s traded=%s",
                    symbol, side, status or "unknown", traded_qty)
        return TradeResult(success=True, order_response=resp, depth=depth, slippage=slippage, error=None)
