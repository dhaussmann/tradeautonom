"""Spread-range strategy for PAXG / XAU.

Assumption: PAXG >= XAU (always).  Spread = PAXG_mid - XAU_mid >= 0.
The spread oscillates between ~0 and a maximum and is mean-reverting.

Strategy (one direction only):
  - ENTRY when spread <= spread_entry_low:
      Long PAXG + Short XAU  (bet on spread widening)
  - EXIT when spread >= spread_exit_high:
      Sell PAXG + Buy XAU  (take profit on widened spread)

Both legs are executed as market orders with full safety checks.
"""

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from app.config import Settings
from app.executor import TradeExecutor, TradeResult
from app.grvt_client import GrvtClient
from app.safety import check_dual_liquidity, estimate_fill_price

logger = logging.getLogger("tradeautonom.arbitrage")


@dataclass
class SpreadSnapshot:
    """Point-in-time spread data between two instruments."""
    instrument_a: str
    instrument_b: str
    mid_price_a: float
    mid_price_b: float
    spread: float
    spread_abs: float
    a_is_cheaper: bool
    # Execution prices — what you actually pay/receive
    best_bid_a: float = 0.0
    best_ask_a: float = 0.0
    best_bid_b: float = 0.0
    best_ask_b: float = 0.0
    # Execution spread: ask(cheap) - bid(expensive).
    # This is the REAL cost of opening the arb position.
    exec_spread: float = 0.0


@dataclass
class ArbCheckResult:
    """Result of an arbitrage opportunity evaluation."""
    action: str  # "ENTRY", "EXIT", or "NONE"
    snapshot: SpreadSnapshot
    reason: str


@dataclass
class ArbExecutionResult:
    """Combined result of executing both legs of an arb trade."""
    success: bool
    leg_a: TradeResult | None
    leg_b: TradeResult | None
    snapshot: SpreadSnapshot
    error: str | None


# Max retries for transient API/network failures during exit
_EXIT_MAX_RETRIES = 3


class ArbitrageEngine:
    """Spread-range trader: Long PAXG / Short XAU when spread is low,
    close when spread widens."""

    def __init__(
        self,
        client: GrvtClient,
        executor: TradeExecutor,
        settings: Settings,
    ) -> None:
        self.client = client
        self.executor = executor
        self.settings = settings
        # Instruments — spread is always PAXG - XAU
        self.xau_instrument = settings.arb_xau_instrument
        self.paxg_instrument = settings.arb_paxg_instrument
        # Kept for backward-compat with server/WebUI (instrument_a=XAU, instrument_b=PAXG)
        self.instrument_a = self.xau_instrument
        self.instrument_b = self.paxg_instrument
        # Thresholds
        self.spread_entry_low = settings.arb_spread_entry_low
        self.spread_exit_high = settings.arb_spread_exit_high
        self.max_exec_spread = settings.arb_max_exec_spread
        self.quantity = Decimal(str(settings.arb_quantity))
        self.liquidity_multiplier = settings.arb_liquidity_multiplier
        self.chunk_size = Decimal(str(settings.arb_chunk_size))
        self.chunk_delay_ms = settings.arb_chunk_delay_ms
        self.simulation_mode = settings.arb_simulation_mode
        # Position state
        self._has_position = False
        self._long_sym: str | None = None   # always PAXG when open
        self._short_sym: str | None = None  # always XAU when open
        self._entry_spread_actual: float | None = None

    # ------------------------------------------------------------------
    # Position state sync from exchange
    # ------------------------------------------------------------------

    def sync_position_from_exchange(self) -> None:
        """Read open positions from the exchange and restore internal state.

        This must be called on startup so the engine knows about positions
        that survived a server restart.
        """
        try:
            positions = self.client.fetch_positions(
                [self.xau_instrument, self.paxg_instrument]
            )
        except Exception as exc:
            logger.warning("Could not sync positions from exchange: %s", exc)
            return

        long_sym = None
        short_sym = None

        for pos in positions:
            instrument = pos.get("instrument", "")
            size = float(pos.get("size", 0))
            if instrument not in (self.xau_instrument, self.paxg_instrument):
                continue
            if size > 0:
                long_sym = instrument
            elif size < 0:
                short_sym = instrument

        if long_sym and short_sym:
            self._has_position = True
            self._long_sym = long_sym
            self._short_sym = short_sym
            paxg_entry = next(
                (float(p.get("entry_price", 0)) for p in positions if p.get("instrument") == self.paxg_instrument),
                0,
            )
            xau_entry = next(
                (float(p.get("entry_price", 0)) for p in positions if p.get("instrument") == self.xau_instrument),
                0,
            )
            self._entry_spread_actual = (paxg_entry - xau_entry) if paxg_entry and xau_entry else None
            logger.info(
                "Synced position from exchange: LONG %s / SHORT %s (entry spread ~%.2f)",
                long_sym, short_sym, self._entry_spread_actual or 0,
            )
        elif long_sym or short_sym:
            logger.warning(
                "Found only ONE side open on exchange: long=%s short=%s — NOT delta-neutral!",
                long_sym, short_sym,
            )
        else:
            logger.info("No open arb positions found on exchange.")

    # ------------------------------------------------------------------
    # Spread calculation
    # ------------------------------------------------------------------

    def get_spread_snapshot(self) -> SpreadSnapshot:
        """Fetch order books and compute directional spread (PAXG - XAU).

        spread      = PAXG_mid - XAU_mid  (should be >= 0)
        exec_spread = PAXG_ask - XAU_bid  (cost of opening Long PAXG / Short XAU)
        """
        book_xau = self.client.fetch_order_book(self.xau_instrument, limit=10)
        book_paxg = self.client.fetch_order_book(self.paxg_instrument, limit=10)

        mid_xau = _mid_price(book_xau)
        mid_paxg = _mid_price(book_paxg)
        # Directional spread: PAXG - XAU (always >= 0 under our assumption)
        spread = mid_paxg - mid_xau
        spread_abs = abs(spread)

        bid_xau, ask_xau = _best_bid_ask(book_xau)
        bid_paxg, ask_paxg = _best_bid_ask(book_paxg)

        # Execution spread for our strategy direction:
        # Entry = Long PAXG (buy at ask) + Short XAU (sell at bid)
        # Cost  = ask_paxg - bid_xau
        exec_spread = ask_paxg - bid_xau

        snapshot = SpreadSnapshot(
            instrument_a=self.xau_instrument,
            instrument_b=self.paxg_instrument,
            mid_price_a=round(mid_xau, 4),
            mid_price_b=round(mid_paxg, 4),
            spread=round(spread, 4),
            spread_abs=round(spread_abs, 4),
            a_is_cheaper=mid_xau < mid_paxg,
            best_bid_a=bid_xau,
            best_ask_a=ask_xau,
            best_bid_b=bid_paxg,
            best_ask_b=ask_paxg,
            exec_spread=round(exec_spread, 4),
        )
        logger.info(
            "Spread: PAXG-XAU mid=%.2f exec=%.2f (xau bid/ask=%.2f/%.2f paxg bid/ask=%.2f/%.2f)",
            spread, exec_spread, bid_xau, ask_xau, bid_paxg, ask_paxg,
        )
        return snapshot

    # ------------------------------------------------------------------
    # Opportunity evaluation
    # ------------------------------------------------------------------

    def evaluate(self, snapshot: SpreadSnapshot | None = None) -> ArbCheckResult:
        """Determine whether we should ENTER, EXIT, or do NOTHING.

        Uses the directional mid spread (PAXG - XAU) for the signal:
          ENTRY: spread <= spread_entry_low  (spread is narrow)
          EXIT:  spread >= spread_exit_high  (spread has widened)
        """
        if snapshot is None:
            snapshot = self.get_spread_snapshot()

        # spread = PAXG_mid - XAU_mid (directional)
        spread = snapshot.spread

        if not self._has_position and 0 <= spread <= self.spread_entry_low:
            return ArbCheckResult(
                action="ENTRY",
                snapshot=snapshot,
                reason=(
                    f"Spread ${spread:.2f} <= entry low ${self.spread_entry_low:.2f} "
                    f"(exec ${snapshot.exec_spread:.2f})"
                ),
            )

        if not self._has_position and spread < 0:
            return ArbCheckResult(
                action="NONE",
                snapshot=snapshot,
                reason=(
                    f"Spread ${spread:.2f} is NEGATIVE (PAXG < XAU) — anomaly, no entry"
                ),
            )

        if self._has_position and spread >= self.spread_exit_high:
            return ArbCheckResult(
                action="EXIT",
                snapshot=snapshot,
                reason=(
                    f"Spread ${spread:.2f} >= exit high ${self.spread_exit_high:.2f} "
                    f"(exec ${snapshot.exec_spread:.2f})"
                ),
            )

        return ArbCheckResult(
            action="NONE",
            snapshot=snapshot,
            reason=(
                f"Spread ${spread:.2f} — no action "
                f"(entry<=${self.spread_entry_low:.2f}, exit>=${self.spread_exit_high:.2f}, "
                f"pos={self._has_position})"
            ),
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute_entry(
        self,
        snapshot: SpreadSnapshot | None = None,
        min_depth_usd: float | None = None,
        slippage_pct: float | None = None,
    ) -> ArbExecutionResult:
        """Open the spread: always Long PAXG + Short XAU.

        Safety guards:
          1. Mid spread must be <= spread_entry_low.
          2. Exec spread must be <= max_exec_spread (bid-ask cost check).
          3. Both order books must have sufficient liquidity.
          4. After Leg A fills, re-check spread hasn't blown out.
          5. If Leg B fails, immediately unwind Leg A.
        """
        if snapshot is None:
            snapshot = self.get_spread_snapshot()

        # --- SPREAD GUARD (mid) ---
        if snapshot.spread > self.spread_entry_low:
            return ArbExecutionResult(
                success=False, leg_a=None, leg_b=None,
                snapshot=snapshot,
                error=(
                    f"Entry blocked: spread ${snapshot.spread:.2f} "
                    f"> entry low ${self.spread_entry_low:.2f}"
                ),
            )

        # --- EXEC SPREAD SAFETY ---
        if snapshot.exec_spread > self.max_exec_spread:
            return ArbExecutionResult(
                success=False, leg_a=None, leg_b=None,
                snapshot=snapshot,
                error=(
                    f"Entry blocked: exec spread ${snapshot.exec_spread:.2f} "
                    f"> max ${self.max_exec_spread:.2f} (bid-ask too wide)"
                ),
            )

        # Direction is FIXED: Long PAXG + Short XAU
        long_sym = self.paxg_instrument
        short_sym = self.xau_instrument
        long_price = snapshot.mid_price_b   # PAXG mid
        short_price = snapshot.mid_price_a  # XAU mid

        sim_tag = "[SIM] " if self.simulation_mode else ""
        logger.info(
            "%sARB ENTRY: LONG %s @ ~%.2f | SHORT %s @ ~%.2f | qty=%s | spread=%.2f",
            sim_tag, long_sym, long_price, short_sym, short_price, self.quantity, snapshot.spread,
        )

        # --- SIMULATION MODE: skip real orders, track virtual position ---
        if self.simulation_mode:
            sim_leg = TradeResult(
                success=True,
                order_response={"simulated": True, "price": long_price},
                depth=None, slippage=None, error=None,
            )
            sim_leg_b = TradeResult(
                success=True,
                order_response={"simulated": True, "price": short_price},
                depth=None, slippage=None, error=None,
            )
            self._has_position = True
            self._long_sym = long_sym
            self._short_sym = short_sym
            self._entry_spread_actual = snapshot.spread
            logger.info(
                "[SIM] ARB ENTRY complete — LONG %s @ %.2f / SHORT %s @ %.2f, spread=$%.2f",
                long_sym, long_price, short_sym, short_price, snapshot.spread,
            )
            return ArbExecutionResult(
                success=True, leg_a=sim_leg, leg_b=sim_leg_b,
                snapshot=snapshot, error=None,
            )

        # --- PRE-ENTRY DUAL LIQUIDITY CHECK ---
        try:
            book_long = self.client.fetch_order_book(long_sym, limit=50)
            book_short = self.client.fetch_order_book(short_sym, limit=50)
            liq_ok, long_liq, short_liq = check_dual_liquidity(
                book_long=book_long,
                book_short=book_short,
                quantity=float(self.quantity),
                multiplier=self.liquidity_multiplier,
                long_symbol=long_sym,
                short_symbol=short_sym,
            )
            if not liq_ok:
                reasons = []
                if not long_liq.is_sufficient:
                    reasons.append(
                        f"LONG {long_sym}: {long_liq.available_qty:.4f} avail, "
                        f"need {long_liq.required_qty:.4f}"
                    )
                if not short_liq.is_sufficient:
                    reasons.append(
                        f"SHORT {short_sym}: {short_liq.available_qty:.4f} avail, "
                        f"need {short_liq.required_qty:.4f}"
                    )
                return ArbExecutionResult(
                    success=False, leg_a=None, leg_b=None,
                    snapshot=snapshot,
                    error=f"Insufficient liquidity: {'; '.join(reasons)}",
                )
        except Exception as exc:
            return ArbExecutionResult(
                success=False, leg_a=None, leg_b=None,
                snapshot=snapshot,
                error=f"Liquidity check failed: {exc}",
            )

        # --- Leg 1: LONG PAXG (chunked) ---
        leg_a = self._execute_leg_chunked(
            symbol=long_sym,
            side="buy",
            total_qty=self.quantity,
            expected_price=long_price,
            min_depth_usd=min_depth_usd,
            slippage_pct=slippage_pct,
        )
        if not leg_a.success:
            return ArbExecutionResult(
                success=False, leg_a=leg_a, leg_b=None,
                snapshot=snapshot,
                error=f"Leg A (LONG {long_sym}) failed: {leg_a.error}",
            )

        # --- SPREAD RE-CHECK after Leg A ---
        try:
            fresh = self.get_spread_snapshot()
            # If spread has moved significantly against us, unwind
            if fresh.spread > self.spread_entry_low * 3:
                logger.warning(
                    "Spread blew out after Leg A: %.2f (was %.2f). Unwinding.",
                    fresh.spread, snapshot.spread,
                )
                unwind = self._close_leg_with_retry(
                    long_sym, "sell", self.quantity, long_price,
                    slippage_pct or self.settings.default_slippage_pct,
                    min_depth_usd,
                )
                unwind_note = "Unwind OK" if unwind.success else f"UNWIND FAILED: {unwind.error}"
                return ArbExecutionResult(
                    success=False, leg_a=leg_a, leg_b=None,
                    snapshot=fresh,
                    error=f"Spread moved to ${fresh.spread:.2f} after Leg A. {unwind_note}",
                )
        except Exception as exc:
            logger.warning("Spread re-check failed (continuing): %s", exc)

        # --- Leg 2: SHORT XAU (chunked) ---
        leg_b = self._execute_leg_chunked(
            symbol=short_sym,
            side="sell",
            total_qty=self.quantity,
            expected_price=short_price,
            min_depth_usd=min_depth_usd,
            slippage_pct=slippage_pct,
        )
        if not leg_b.success:
            logger.error("Leg B failed, unwinding Leg A (selling %s)", long_sym)
            unwind = self._close_leg_with_retry(
                long_sym, "sell", self.quantity, long_price,
                slippage_pct or self.settings.default_slippage_pct,
                min_depth_usd,
            )
            unwind_note = "Unwind OK" if unwind.success else f"UNWIND FAILED: {unwind.error}"
            return ArbExecutionResult(
                success=False, leg_a=leg_a, leg_b=leg_b,
                snapshot=snapshot,
                error=f"Leg B (SHORT {short_sym}) failed: {leg_b.error}. {unwind_note}",
            )

        # --- Success ---
        self._has_position = True
        self._long_sym = long_sym
        self._short_sym = short_sym
        self._entry_spread_actual = snapshot.spread
        logger.info(
            "ARB ENTRY complete — LONG %s / SHORT %s, spread=$%.2f",
            long_sym, short_sym, snapshot.spread,
        )
        return ArbExecutionResult(
            success=True, leg_a=leg_a, leg_b=leg_b,
            snapshot=snapshot, error=None,
        )

    def execute_exit(
        self,
        snapshot: SpreadSnapshot | None = None,
        min_depth_usd: float | None = None,
        slippage_pct: float | None = None,
    ) -> ArbExecutionResult:
        """Close the spread by reversing the entry legs.

        Uses the *stored* entry direction (_long_sym / _short_sym) so that
        positions are always closed correctly regardless of current spread.

        Safety: each leg is retried up to _EXIT_MAX_RETRIES times with
        escalating slippage tolerance to ensure positions are closed
        even during volatile spikes.
        """
        if snapshot is None:
            snapshot = self.get_spread_snapshot()

        if not self._long_sym or not self._short_sym:
            return ArbExecutionResult(
                success=False, leg_a=None, leg_b=None,
                snapshot=snapshot,
                error="No stored entry direction — cannot determine which side to close.",
            )

        # Close LONG position: SELL the long instrument
        sell_sym = self._long_sym
        # Close SHORT position: BUY back the short instrument
        buy_sym = self._short_sym

        sell_price = snapshot.mid_price_a if sell_sym == self.instrument_a else snapshot.mid_price_b
        buy_price = snapshot.mid_price_a if buy_sym == self.instrument_a else snapshot.mid_price_b

        base_slippage = slippage_pct or self.settings.default_slippage_pct

        sim_tag = "[SIM] " if self.simulation_mode else ""
        logger.info(
            "%sARB EXIT: SELL %s (close long) @ ~%.2f | BUY %s (close short) @ ~%.2f | qty=%s",
            sim_tag, sell_sym, sell_price, buy_sym, buy_price, self.quantity,
        )

        # --- SIMULATION MODE: skip real orders, clear virtual position ---
        if self.simulation_mode:
            sim_leg = TradeResult(
                success=True,
                order_response={"simulated": True, "price": sell_price},
                depth=None, slippage=None, error=None,
            )
            sim_leg_b = TradeResult(
                success=True,
                order_response={"simulated": True, "price": buy_price},
                depth=None, slippage=None, error=None,
            )
            pnl = (snapshot.spread - (self._entry_spread_actual or 0)) * float(self.quantity)
            self._has_position = False
            self._long_sym = None
            self._short_sym = None
            self._entry_spread_actual = None
            logger.info(
                "[SIM] ARB EXIT complete — spread=$%.2f, est. PnL=$%.4f",
                snapshot.spread, pnl,
            )
            return ArbExecutionResult(
                success=True, leg_a=sim_leg, leg_b=sim_leg_b,
                snapshot=snapshot, error=None,
            )

        # --- Leg A: close the LONG position (chunked) ---
        leg_a = self._execute_leg_chunked(
            symbol=sell_sym, side="sell", total_qty=self.quantity,
            expected_price=sell_price,
            min_depth_usd=min_depth_usd, slippage_pct=base_slippage,
        )
        if not leg_a.success:
            # Position stays open — do NOT force close with worse terms
            logger.warning(
                "Exit Leg A failed — keeping position open to protect profits."
            )
            return ArbExecutionResult(
                success=False, leg_a=leg_a, leg_b=None,
                snapshot=snapshot,
                error=f"Exit Leg A (SELL {sell_sym}) failed: {leg_a.error}. Position kept open.",
            )

        # --- Leg B: close the SHORT position (chunked) ---
        leg_b = self._execute_leg_chunked(
            symbol=buy_sym, side="buy", total_qty=self.quantity,
            expected_price=buy_price,
            min_depth_usd=min_depth_usd, slippage_pct=base_slippage,
        )
        if not leg_b.success:
            # Leg A already closed — re-open it to stay fully hedged
            logger.error(
                "Exit Leg B failed, re-opening Leg A (buying %s) to stay hedged",
                sell_sym,
            )
            reopen = self._close_leg_with_retry(
                sell_sym, "buy", self.quantity, sell_price,
                base_slippage, min_depth_usd,
            )
            reopen_note = "Re-hedge OK" if reopen.success else f"RE-HEDGE FAILED: {reopen.error}"
            return ArbExecutionResult(
                success=False, leg_a=leg_a, leg_b=leg_b,
                snapshot=snapshot,
                error=f"Exit Leg B (BUY {buy_sym}) failed: {leg_b.error}. {reopen_note}",
            )

        # --- Success: clear position state ---
        self._has_position = False
        self._long_sym = None
        self._short_sym = None
        self._entry_spread_actual = None
        logger.info("ARB EXIT complete — both positions closed.")
        return ArbExecutionResult(
            success=True, leg_a=leg_a, leg_b=leg_b,
            snapshot=snapshot, error=None,
        )

    # ------------------------------------------------------------------
    # Chunked execution helper
    # ------------------------------------------------------------------

    def _execute_leg_chunked(
        self,
        symbol: str,
        side: str,
        total_qty: Decimal,
        expected_price: float,
        min_depth_usd: float | None = None,
        slippage_pct: float | None = None,
    ) -> TradeResult:
        """Execute a leg in chunks to reduce market impact.

        Splits total_qty into pieces of self.chunk_size, executes each as a
        separate market order, and waits self.chunk_delay_ms between them.
        Returns an aggregated TradeResult.
        """
        chunk = self.chunk_size
        if chunk <= 0 or chunk >= total_qty:
            # No chunking needed — single order
            return self.executor.execute_market_order(
                symbol=symbol, side=side, quantity=total_qty,
                expected_price=expected_price,
                min_depth_usd=min_depth_usd, slippage_pct=slippage_pct,
            )

        remaining = total_qty
        filled_chunks: list[TradeResult] = []
        chunk_num = 0

        while remaining > 0:
            chunk_num += 1
            qty = min(chunk, remaining)
            logger.info(
                "Chunk %d: %s %s qty=%s (remaining=%s)",
                chunk_num, side.upper(), symbol, qty, remaining,
            )
            result = self.executor.execute_market_order(
                symbol=symbol, side=side, quantity=qty,
                expected_price=expected_price,
                min_depth_usd=min_depth_usd, slippage_pct=slippage_pct,
            )
            if not result.success:
                filled_total = total_qty - remaining
                logger.error(
                    "Chunk %d failed after filling %s of %s: %s",
                    chunk_num, filled_total, total_qty, result.error,
                )
                # Return failure with info about partial fill
                return TradeResult(
                    success=False,
                    order_response={"chunks_ok": chunk_num - 1, "filled": float(filled_total)},
                    depth=result.depth, slippage=result.slippage,
                    error=f"Chunk {chunk_num} failed ({filled_total}/{total_qty} filled): {result.error}",
                )
            filled_chunks.append(result)
            remaining -= qty
            if remaining > 0 and self.chunk_delay_ms > 0:
                time.sleep(self.chunk_delay_ms / 1000.0)

        logger.info(
            "All %d chunks filled for %s %s (total=%s)",
            chunk_num, side.upper(), symbol, total_qty,
        )
        return TradeResult(
            success=True,
            order_response={"chunks": chunk_num, "total_qty": float(total_qty)},
            depth=filled_chunks[-1].depth if filled_chunks else None,
            slippage=filled_chunks[-1].slippage if filled_chunks else None,
            error=None,
        )

    # ------------------------------------------------------------------
    # Retry helper for closing legs
    # ------------------------------------------------------------------

    def _close_leg_with_retry(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        expected_price: float,
        slippage_pct: float,
        min_depth_usd: float | None = None,
    ) -> TradeResult:
        """Try to close a leg with constant slippage.

        Only retries on transient API/network errors (e.g. order rejected,
        timeout). Does NOT escalate slippage — if slippage is too high the
        position stays open to protect profits.
        """
        last_result = None
        for attempt in range(_EXIT_MAX_RETRIES):
            logger.info(
                "Close leg attempt %d/%d: %s %s qty=%s slippage=%.2f%%",
                attempt + 1, _EXIT_MAX_RETRIES, side.upper(), symbol,
                quantity, slippage_pct,
            )
            result = self.executor.execute_market_order(
                symbol=symbol,
                side=side,
                quantity=quantity,
                expected_price=expected_price,
                slippage_pct=slippage_pct,
                min_depth_usd=min_depth_usd,
            )
            if result.success:
                return result
            last_result = result
            # Only retry if it was an API/network error, not a safety check failure
            is_safety_failure = last_result.error and ("Slippage" in last_result.error or "Depth" in last_result.error)
            if is_safety_failure:
                logger.warning(
                    "Close leg aborted — safety check failed (slippage/depth too bad): %s",
                    last_result.error,
                )
                return last_result  # Don't retry, protect profits
            logger.warning(
                "Close leg attempt %d failed (transient): %s", attempt + 1, result.error,
            )
        return last_result

    def execute_signal(
        self,
        action: str,
        min_depth_usd: float | None = None,
        slippage_pct: float | None = None,
    ) -> ArbExecutionResult:
        """Convenience: run entry or exit based on a string action."""
        snapshot = self.get_spread_snapshot()
        if action.upper() == "ENTRY":
            return self.execute_entry(snapshot, min_depth_usd=min_depth_usd, slippage_pct=slippage_pct)
        elif action.upper() == "EXIT":
            return self.execute_exit(snapshot, min_depth_usd=min_depth_usd, slippage_pct=slippage_pct)
        else:
            return ArbExecutionResult(
                success=False, leg_a=None, leg_b=None,
                snapshot=snapshot,
                error=f"Unknown action: {action}",
            )

    @property
    def position_info(self) -> dict:
        """Return current position state for the dashboard."""
        return {
            "has_position": self._has_position,
            "long_sym": self._long_sym,
            "short_sym": self._short_sym,
            "entry_spread": self._entry_spread_actual,
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _mid_price(order_book: dict) -> float:
    """Return the mid price from an order book dict."""
    bids = order_book.get("bids", [])
    asks = order_book.get("asks", [])
    if not bids or not asks:
        raise ValueError("Order book is empty — cannot compute mid price")
    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    return (best_bid + best_ask) / 2.0


def _best_bid_ask(order_book: dict) -> tuple[float, float]:
    """Return (best_bid, best_ask) from an order book."""
    bids = order_book.get("bids", [])
    asks = order_book.get("asks", [])
    if not bids or not asks:
        raise ValueError("Order book is empty — cannot get bid/ask")
    return float(bids[0][0]), float(asks[0][0])
