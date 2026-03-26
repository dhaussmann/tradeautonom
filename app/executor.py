"""Trade executor: validates safety checks and places orders on GRVT."""

import logging
from dataclasses import dataclass
from decimal import Decimal

from app.config import Settings
from app.grvt_client import GrvtClient
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
    """Validates pre-trade conditions and executes market orders on GRVT."""

    def __init__(self, client: GrvtClient, settings: Settings) -> None:
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

        # Fetch order book
        try:
            order_book = self.client.fetch_order_book(symbol, limit=50)
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

        # Place the order
        try:
            resp = self.client.create_market_order(
                symbol=symbol,
                side=side,
                amount=quantity,
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
