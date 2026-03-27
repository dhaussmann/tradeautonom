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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from decimal import Decimal

from app.config import Settings
from app.exchange import ExchangeClient
from app.executor import TradeExecutor, TradeResult
from app.grvt_client import GrvtClient
from app.safety import estimate_fill_price, run_pre_trade_checks
from app.ws_feeds import OrderbookFeedManager

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
    # Slippage cost on both legs (qty * slippage_pct/100 * price, both sides).
    # break_even_spread = exec_spread + slippage_cost — entry is only profitable
    # if the EXIT spread will be at least this much wider than the entry spread.
    slippage_cost: float = 0.0
    break_even_spread: float = 0.0
    data_source: str = "rest"  # "websocket" or "rest"


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
        clients: dict[str, ExchangeClient],
        executor: TradeExecutor,
        settings: Settings,
    ) -> None:
        self.clients = clients
        self.client = clients.get("grvt")  # backward-compat: GRVT for trading/positions
        self.executor = executor
        self.settings = settings
        # Instruments — spread is always PAXG - XAU
        self.xau_instrument = settings.arb_xau_instrument
        self.paxg_instrument = settings.arb_paxg_instrument
        # Kept for backward-compat with server/WebUI (instrument_a=XAU, instrument_b=PAXG)
        self.instrument_a = self.xau_instrument
        self.instrument_b = self.paxg_instrument
        # Per-leg exchange selection
        self.leg_a_exchange = settings.arb_leg_a_exchange
        self.leg_b_exchange = settings.arb_leg_b_exchange
        # Thresholds
        self.spread_entry_low = settings.arb_spread_entry_low
        self.spread_exit_high = settings.arb_spread_exit_high
        self.max_exec_spread = settings.arb_max_exec_spread
        self.quantity = Decimal(str(settings.arb_quantity))
        self.liquidity_multiplier = settings.arb_liquidity_multiplier
        self.chunk_size = Decimal(str(settings.arb_chunk_size))
        self.chunk_delay_ms = settings.arb_chunk_delay_ms
        self.simulation_mode = settings.arb_simulation_mode
        # Aggressive limit order settings
        self.order_type = settings.arb_order_type           # "aggressive_limit" or "market"
        self.limit_offset_ticks = settings.arb_limit_offset_ticks
        self.min_profit = settings.arb_min_profit           # min USD profit above break-even
        self.fill_timeout_ms = settings.arb_fill_timeout_ms
        # WebSocket feed manager (set externally via set_feed_manager)
        self._feed_manager: OrderbookFeedManager | None = None
        self._ws_enabled = settings.arb_ws_enabled
        self._ws_stale_ms = settings.arb_ws_stale_ms
        # Position state
        self._has_position = False
        self._long_sym: str | None = None   # always PAXG when open
        self._short_sym: str | None = None  # always XAU when open
        self._entry_spread_actual: float | None = None

    def set_feed_manager(self, mgr: OrderbookFeedManager) -> None:
        """Attach a WebSocket feed manager for real-time orderbook data."""
        self._feed_manager = mgr

    def _get_client(self, exchange_name: str) -> ExchangeClient:
        """Resolve an exchange client by name."""
        client = self.clients.get(exchange_name)
        if client is None:
            raise ValueError(f"Unknown exchange: {exchange_name!r}. Available: {list(self.clients.keys())}")
        return client

    def _client_a(self) -> ExchangeClient:
        """Client for instrument_a (leg A)."""
        return self._get_client(self.leg_a_exchange)

    def _client_b(self) -> ExchangeClient:
        """Client for instrument_b (leg B)."""
        return self._get_client(self.leg_b_exchange)

    def _get_orderbook(self, exchange: str, instrument: str, limit: int = 10) -> dict:
        """Get orderbook from WS feed if available and fresh, else fall back to REST."""
        if self._ws_enabled and self._feed_manager is not None:
            if not self._feed_manager.is_stale(exchange, instrument, self._ws_stale_ms):
                book = self._feed_manager.get_book(exchange, instrument)
                if book is not None:
                    return book
                logger.debug("WS book empty for %s:%s — falling back to REST", exchange, instrument)
            else:
                logger.debug("WS data stale for %s:%s — falling back to REST", exchange, instrument)
        return self._get_client(exchange).fetch_order_book(instrument, limit=limit)

    # ------------------------------------------------------------------
    # Position state sync from exchange
    # ------------------------------------------------------------------

    def sync_position_from_exchange(self) -> None:
        """Read open positions from both exchanges and restore internal state.

        Queries each instrument from its configured exchange so that
        cross-exchange arb pairs (e.g. Extended + GRVT) are handled correctly.
        """
        positions: list[dict] = []

        # Query leg A instrument from its exchange
        try:
            client_a = self._client_a()
            if hasattr(client_a, "fetch_positions"):
                pos_a = client_a.fetch_positions([self.xau_instrument])
                positions.extend(pos_a)
        except Exception as exc:
            logger.warning("Could not sync leg A positions (%s@%s): %s", self.xau_instrument, self.leg_a_exchange, exc)

        # Query leg B instrument from its exchange (skip if same exchange to avoid double-counting)
        if self.leg_b_exchange != self.leg_a_exchange:
            try:
                client_b = self._client_b()
                if hasattr(client_b, "fetch_positions"):
                    pos_b = client_b.fetch_positions([self.paxg_instrument])
                    positions.extend(pos_b)
            except Exception as exc:
                logger.warning("Could not sync leg B positions (%s@%s): %s", self.paxg_instrument, self.leg_b_exchange, exc)
        else:
            # Same exchange — query both instruments in one call
            try:
                client_b = self._client_b()
                if hasattr(client_b, "fetch_positions"):
                    pos_b = client_b.fetch_positions([self.paxg_instrument])
                    positions.extend(pos_b)
            except Exception as exc:
                logger.warning("Could not sync leg B positions: %s", exc)

        long_sym = None
        short_sym = None
        entry_a = 0.0
        entry_b = 0.0

        for pos in positions:
            instrument = pos.get("instrument", "")
            size = float(pos.get("size", 0))
            if instrument == self.xau_instrument:
                if size > 0:
                    long_sym = instrument
                    entry_a = float(pos.get("entry_price", 0))
                elif size < 0:
                    short_sym = instrument
                    entry_a = float(pos.get("entry_price", 0))
            elif instrument == self.paxg_instrument:
                if size > 0:
                    long_sym = instrument
                    entry_b = float(pos.get("entry_price", 0))
                elif size < 0:
                    short_sym = instrument
                    entry_b = float(pos.get("entry_price", 0))

        if long_sym and short_sym:
            self._has_position = True
            self._long_sym = long_sym
            self._short_sym = short_sym
            paxg_entry = entry_b if long_sym == self.paxg_instrument else entry_a
            xau_entry = entry_a if short_sym == self.xau_instrument else entry_b
            self._entry_spread_actual = (paxg_entry - xau_entry) if paxg_entry and xau_entry else None
            logger.info(
                "Synced position from exchange: LONG %s / SHORT %s (entry spread ~%.4f)",
                long_sym, short_sym, self._entry_spread_actual or 0,
            )
        elif long_sym or short_sym:
            # One side open — delta not neutral, log warning but keep has_position=True
            # so the engine doesn't try to open a new entry on the already-open side
            self._has_position = True
            self._long_sym = long_sym
            self._short_sym = short_sym
            logger.warning(
                "Sync: only ONE side open — long=%s short=%s — NOT delta-neutral! "
                "has_position set True to block new entries. Manual close required.",
                long_sym, short_sym,
            )
        else:
            # No positions on either exchange — reset state
            was_open = self._has_position
            self._has_position = False
            self._long_sym = None
            self._short_sym = None
            self._entry_spread_actual = None
            if was_open:
                logger.warning(
                    "Sync: internal state claimed open position but exchange shows NONE — state reset to flat."
                )
            else:
                logger.info("Sync: no open arb positions on exchange — state is flat.")

    # ------------------------------------------------------------------
    # Spread calculation
    # ------------------------------------------------------------------

    def get_spread_snapshot(self) -> SpreadSnapshot:
        """Fetch order books and compute spread between instrument_a and instrument_b.

        spread      = mid_b - mid_a  (signed; positive = B is more expensive)
        spread_abs  = abs(spread)
        a_is_cheaper = mid_a < mid_b → buy A cheap / sell B expensive
        exec_spread = cost of the cheaper entry direction (buy cheap ask - sell exp bid)

        For same-asset cross-exchange pairs (e.g. SOL on Extended vs GRVT) the
        spread oscillates around 0 and can be positive or negative. The strategy
        enters when spread_abs >= spread_entry_low (a meaningful price gap exists)
        and exits when spread_abs <= spread_exit_high is no longer met.
        """
        book_a = self._get_orderbook(self.leg_a_exchange, self.xau_instrument)
        book_b = self._get_orderbook(self.leg_b_exchange, self.paxg_instrument)

        mid_a = _mid_price(book_a)
        mid_b = _mid_price(book_b)
        spread = mid_b - mid_a
        spread_abs = abs(spread)
        a_is_cheaper = mid_a < mid_b

        bid_a, ask_a = _best_bid_ask(book_a)
        bid_b, ask_b = _best_bid_ask(book_b)

        # Execution spread: cost of entering in the profitable direction.
        # If A is cheaper: Long A (buy at ask_a) + Short B (sell at bid_b)
        #   exec_spread = ask_a - bid_b  (negative = profitable gap, positive = costly)
        # If B is cheaper: Long B (buy at ask_b) + Short A (sell at bid_a)
        #   exec_spread = ask_b - bid_a
        if a_is_cheaper:
            exec_spread = ask_a - bid_b
        else:
            exec_spread = ask_b - bid_a

        # Slippage cost on all 4 legs (entry + exit):
        # slip_pct/100 * (ask_cheap + bid_exp + bid_cheap + ask_exp)
        slip_pct = self.settings.default_slippage_pct / 100
        slippage_cost = round(slip_pct * (ask_a + bid_a + ask_b + bid_b), 4)
        break_even_spread = round(abs(exec_spread) + slippage_cost, 4)

        # Determine data source — if either book came from WS, mark as websocket
        src_a = book_a.get("_source", "rest") if isinstance(book_a, dict) else "rest"
        src_b = book_b.get("_source", "rest") if isinstance(book_b, dict) else "rest"
        data_source = "websocket" if (src_a == "websocket" or src_b == "websocket") else "rest"

        snapshot = SpreadSnapshot(
            instrument_a=self.xau_instrument,
            instrument_b=self.paxg_instrument,
            mid_price_a=round(mid_a, 4),
            mid_price_b=round(mid_b, 4),
            spread=round(spread, 4),
            spread_abs=round(spread_abs, 4),
            a_is_cheaper=a_is_cheaper,
            best_bid_a=bid_a,
            best_ask_a=ask_a,
            best_bid_b=bid_b,
            best_ask_b=ask_b,
            exec_spread=round(exec_spread, 4),
            slippage_cost=slippage_cost,
            break_even_spread=break_even_spread,
            data_source=data_source,
        )
        cheaper = self.xau_instrument if a_is_cheaper else self.paxg_instrument
        logger.info(
            "Spread [%s]: %s-%s mid=%.4f abs=%.4f exec=%.4f slippage_cost=%.4f break_even=%.4f "
            "(%s bid/ask=%.4f/%.4f  %s bid/ask=%.4f/%.4f) cheaper=%s",
            data_source,
            self.paxg_instrument, self.xau_instrument,
            spread, spread_abs, exec_spread, slippage_cost, break_even_spread,
            self.xau_instrument, bid_a, ask_a,
            self.paxg_instrument, bid_b, ask_b,
            cheaper,
        )
        return snapshot

    # ------------------------------------------------------------------
    # Opportunity evaluation
    # ------------------------------------------------------------------

    def evaluate(self, snapshot: SpreadSnapshot | None = None) -> ArbCheckResult:
        """Determine whether we should ENTER, EXIT, or do NOTHING.

        Goal: open a delta-neutral position (Long cheap / Short expensive) when
        the price difference between the two exchanges is small enough.

          ENTRY: spread_abs <= spread_entry_low
                 (prices are close enough — acceptable entry cost)
          EXIT:  spread_abs >= spread_exit_high when holding a position
                 (spread has widened unfavourably — close to limit loss)
        """
        if snapshot is None:
            snapshot = self.get_spread_snapshot()

        spread_abs = snapshot.spread_abs

        if not self._has_position:
            if spread_abs <= self.spread_entry_low:
                return ArbCheckResult(
                    action="ENTRY",
                    snapshot=snapshot,
                    reason=(
                        f"Spread ${spread_abs:.4f} <= entry max ${self.spread_entry_low:.4f} "
                        f"({'A cheaper' if snapshot.a_is_cheaper else 'B cheaper'}: "
                        f"Long {self.xau_instrument if snapshot.a_is_cheaper else self.paxg_instrument} / "
                        f"Short {self.paxg_instrument if snapshot.a_is_cheaper else self.xau_instrument}) "
                        f"exec=${snapshot.exec_spread:.4f}"
                    ),
                )
            return ArbCheckResult(
                action="NONE",
                snapshot=snapshot,
                reason=(
                    f"Spread ${spread_abs:.4f} > entry max ${self.spread_entry_low:.4f} — spread too wide"
                ),
            )

        # Has position: exit when spread has widened beyond the exit threshold
        if self._has_position and spread_abs >= self.spread_exit_high:
            return ArbCheckResult(
                action="EXIT",
                snapshot=snapshot,
                reason=(
                    f"Spread ${spread_abs:.4f} >= exit threshold ${self.spread_exit_high:.4f} "
                    f"(spread widened — closing position)"
                ),
            )

        return ArbCheckResult(
            action="NONE",
            snapshot=snapshot,
            reason=(
                f"Spread ${spread_abs:.4f} — holding position "
                f"(exit if spread >= ${self.spread_exit_high:.4f})"
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
        """Open the spread: Long cheaper / Short expensive.

        Safety guards:
          1. Spread abs must be >= spread_entry_low.
          2. Exec spread must be <= max_exec_spread.
          3. Break-even guard: spread must exceed costs + min_profit.
          4. Both order books must have sufficient depth + slippage within bounds.
          5. Both legs executed in parallel (ThreadPoolExecutor).
          6. Fill confirmation: if one leg fails/unfills, unwind the other.
        """
        if snapshot is None:
            snapshot = self.get_spread_snapshot()

        # --- SPREAD GUARD: entry only when spread is within acceptable range ---
        if snapshot.spread_abs > self.spread_entry_low:
            return ArbExecutionResult(
                success=False, leg_a=None, leg_b=None,
                snapshot=snapshot,
                error=(
                    f"Entry blocked: spread_abs ${snapshot.spread_abs:.4f} "
                    f"> entry max ${self.spread_entry_low:.4f}"
                ),
            )

        # --- EXEC SPREAD SAFETY ---
        if abs(snapshot.exec_spread) > self.max_exec_spread:
            return ArbExecutionResult(
                success=False, leg_a=None, leg_b=None,
                snapshot=snapshot,
                error=(
                    f"Entry blocked: exec spread ${snapshot.exec_spread:.4f} "
                    f"> max ${self.max_exec_spread:.2f} (bid-ask too wide)"
                ),
            )

        # Direction: dynamic — Long the cheaper instrument, Short the more expensive one
        if snapshot.a_is_cheaper:
            long_sym = self.xau_instrument
            short_sym = self.paxg_instrument
            long_exchange = self.leg_a_exchange
            short_exchange = self.leg_b_exchange
            long_price = snapshot.mid_price_a
            short_price = snapshot.mid_price_b
        else:
            long_sym = self.paxg_instrument
            short_sym = self.xau_instrument
            long_exchange = self.leg_b_exchange
            short_exchange = self.leg_a_exchange
            long_price = snapshot.mid_price_b
            short_price = snapshot.mid_price_a

        sim_tag = "[SIM] " if self.simulation_mode else ""
        order_mode = self.order_type.upper()
        logger.info(
            "%sARB ENTRY [%s]: LONG %s@%s @ ~%.4f | SHORT %s@%s @ ~%.4f | qty=%s | spread_abs=%.4f",
            sim_tag, order_mode, long_sym, long_exchange, long_price,
            short_sym, short_exchange, short_price,
            self.quantity, snapshot.spread_abs,
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
            self._entry_spread_actual = snapshot.spread_abs
            logger.info(
                "[SIM] ARB ENTRY complete — LONG %s@%s @ %.4f / SHORT %s@%s @ %.4f, spread_abs=$%.4f",
                long_sym, long_exchange, long_price,
                short_sym, short_exchange, short_price,
                snapshot.spread_abs,
            )
            return ArbExecutionResult(
                success=True, leg_a=sim_leg, leg_b=sim_leg_b,
                snapshot=snapshot, error=None,
            )

        max_slip = slippage_pct if slippage_pct is not None else self.settings.default_slippage_pct
        depth_req = min_depth_usd if min_depth_usd is not None else self.settings.min_order_book_depth_usd

        logger.info(
            "=== ENTRY PRE-TRADE CHECKS === spread_abs=$%.4f break_even=$%.4f min_profit=$%.4f "
            "exec_cost=$%.4f slippage_cost=$%.4f max_slip=%.4f%% min_depth=$%.2f qty=%s order_type=%s",
            snapshot.spread_abs, snapshot.break_even_spread, self.min_profit,
            snapshot.exec_spread, snapshot.slippage_cost,
            max_slip, depth_req, self.quantity, self.order_type,
        )

        # --- FULL PRE-TRADE CHECK: fetch both books once, check all conditions
        #     before placing any order — avoids needing an unwind ---
        long_client = self._get_client(long_exchange)
        short_client = self._get_client(short_exchange)
        try:
            book_long = long_client.fetch_order_book(long_sym, limit=50)
            book_short = short_client.fetch_order_book(short_sym, limit=50)
            logger.info(
                "Order books fetched — LONG %s@%s best_ask=%s | SHORT %s@%s best_bid=%s",
                long_sym, long_exchange,
                book_long["asks"][0][0] if book_long.get("asks") else "?",
                short_sym, short_exchange,
                book_short["bids"][0][0] if book_short.get("bids") else "?",
            )
        except Exception as exc:
            return ArbExecutionResult(
                success=False, leg_a=None, leg_b=None,
                snapshot=snapshot,
                error=f"Failed to fetch order books: {exc}",
            )

        failures = []

        # --- Min order size ---
        min_long = long_client.get_min_order_size(long_sym)
        logger.info("Min order size check — LONG %s: qty=%s min=%s [%s]",
            long_sym, self.quantity, min_long, "PASS" if self.quantity >= min_long else "FAIL")
        if self.quantity < min_long:
            failures.append(
                f"LONG {long_sym} qty {self.quantity} < min order size {min_long} on {long_exchange}"
            )

        min_short = short_client.get_min_order_size(short_sym)
        logger.info("Min order size check — SHORT %s: qty=%s min=%s [%s]",
            short_sym, self.quantity, min_short, "PASS" if self.quantity >= min_short else "FAIL")
        if self.quantity < min_short:
            failures.append(
                f"SHORT {short_sym} qty {self.quantity} < min order size {min_short} on {short_exchange}"
            )

        # --- Depth + slippage checks ---
        passed_long, depth_long, slip_long = run_pre_trade_checks(
            order_book=book_long, side="buy", quantity=self.quantity,
            expected_price=long_price,
            max_slippage_pct=max_slip, min_depth_usd=depth_req,
        )
        logger.info(
            "LONG %s depth check: avail=$%.2f need=$%.2f [%s] | "
            "slippage: estimated=%.4f%% max=%.4f%% fill_price=%.4f [%s]",
            long_sym,
            depth_long.available_depth_usd, depth_long.required_depth_usd,
            "PASS" if depth_long.is_sufficient else "FAIL",
            slip_long.slippage_pct, slip_long.max_allowed_pct, slip_long.estimated_fill_price,
            "PASS" if slip_long.is_acceptable else "FAIL",
        )
        if not passed_long:
            if not depth_long.is_sufficient:
                failures.append(
                    f"LONG {long_sym} depth: ${depth_long.available_depth_usd:.2f} avail, "
                    f"need ${depth_long.required_depth_usd:.2f}"
                )
            if not slip_long.is_acceptable:
                failures.append(
                    f"LONG {long_sym} slippage: {slip_long.slippage_pct:.4f}% > max {slip_long.max_allowed_pct:.4f}% "
                    f"(fill ${slip_long.estimated_fill_price:.4f} vs expected ${long_price:.4f})"
                )

        passed_short, depth_short, slip_short = run_pre_trade_checks(
            order_book=book_short, side="sell", quantity=self.quantity,
            expected_price=short_price,
            max_slippage_pct=max_slip, min_depth_usd=depth_req,
        )
        logger.info(
            "SHORT %s depth check: avail=$%.2f need=$%.2f [%s] | "
            "slippage: estimated=%.4f%% max=%.4f%% fill_price=%.4f [%s]",
            short_sym,
            depth_short.available_depth_usd, depth_short.required_depth_usd,
            "PASS" if depth_short.is_sufficient else "FAIL",
            slip_short.slippage_pct, slip_short.max_allowed_pct, slip_short.estimated_fill_price,
            "PASS" if slip_short.is_acceptable else "FAIL",
        )
        if not passed_short:
            if not depth_short.is_sufficient:
                failures.append(
                    f"SHORT {short_sym} depth: ${depth_short.available_depth_usd:.2f} avail, "
                    f"need ${depth_short.required_depth_usd:.2f}"
                )
            if not slip_short.is_acceptable:
                failures.append(
                    f"SHORT {short_sym} slippage: {slip_short.slippage_pct:.4f}% > max {slip_short.max_allowed_pct:.4f}% "
                    f"(fill ${slip_short.estimated_fill_price:.4f} vs expected ${short_price:.4f})"
                )

        if failures:
            logger.warning("=== ENTRY BLOCKED — %d check(s) failed: %s ===", len(failures), "; ".join(failures))
            return ArbExecutionResult(
                success=False, leg_a=None, leg_b=None,
                snapshot=snapshot,
                error=f"Pre-trade checks failed — no orders placed: {'; '.join(failures)}",
            )

        logger.info(
            "=== ALL ENTRY CHECKS PASSED — LONG %s@%s / SHORT %s@%s "
            "qty=%s spread_abs=$%.4f exec_cost=$%.4f slip_cost=$%.4f order_type=%s ===",
            long_sym, long_exchange, short_sym, short_exchange,
            self.quantity, snapshot.spread_abs, snapshot.exec_spread, snapshot.slippage_cost,
            self.order_type,
        )

        # --- PARALLEL LEG EXECUTION ---
        leg_a, leg_b = self._execute_legs_parallel(
            long_sym=long_sym, short_sym=short_sym,
            long_exchange=long_exchange, short_exchange=short_exchange,
            long_price=long_price, short_price=short_price,
            min_depth_usd=0.0, slippage_pct=999.0,
        )

        if not leg_a.success and not leg_b.success:
            return ArbExecutionResult(
                success=False, leg_a=leg_a, leg_b=leg_b,
                snapshot=snapshot,
                error=f"Both legs failed: A={leg_a.error}; B={leg_b.error}",
            )

        if not leg_a.success:
            # Leg B filled but Leg A failed — unwind Leg B
            logger.error("Leg A failed, unwinding Leg B (buying back %s)", short_sym)
            self._unwind_leg(short_sym, "buy", self.quantity, short_price, short_exchange)
            return ArbExecutionResult(
                success=False, leg_a=leg_a, leg_b=leg_b,
                snapshot=snapshot,
                error=f"Leg A (LONG {long_sym}) failed: {leg_a.error}. Leg B unwound.",
            )

        if not leg_b.success:
            # Leg A filled but Leg B failed — unwind Leg A
            logger.error("Leg B failed, unwinding Leg A (selling %s)", long_sym)
            self._unwind_leg(long_sym, "sell", self.quantity, long_price, long_exchange)
            return ArbExecutionResult(
                success=False, leg_a=leg_a, leg_b=leg_b,
                snapshot=snapshot,
                error=f"Leg B (SHORT {short_sym}) failed: {leg_b.error}. Leg A unwound.",
            )

        # --- Success: set state optimistically, then verify with exchange ---
        self._has_position = True
        self._long_sym = long_sym
        self._short_sym = short_sym
        self._entry_spread_actual = snapshot.spread_abs
        logger.info(
            "ARB ENTRY orders sent — LONG %s@%s / SHORT %s@%s, spread_abs=$%.4f — verifying with exchange...",
            long_sym, long_exchange, short_sym, short_exchange, snapshot.spread_abs,
        )
        self.sync_position_from_exchange()
        if not self._has_position:
            logger.warning(
                "ARB ENTRY: orders reported OK but exchange shows NO position — "
                "fills may not have landed. State set to flat."
            )
            return ArbExecutionResult(
                success=False, leg_a=leg_a, leg_b=leg_b,
                snapshot=snapshot,
                error="Entry orders sent but exchange shows no position — fills not confirmed.",
            )
        logger.info(
            "ARB ENTRY complete — exchange confirms LONG %s / SHORT %s open.",
            self._long_sym, self._short_sym,
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
            pnl = (snapshot.spread_abs - (self._entry_spread_actual or 0)) * float(self.quantity)
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

        # Determine which exchange each symbol belongs to (based on configured leg assignments)
        sell_exchange = self.leg_a_exchange if sell_sym == self.xau_instrument else self.leg_b_exchange
        buy_exchange = self.leg_a_exchange if buy_sym == self.xau_instrument else self.leg_b_exchange

        max_slip = base_slippage
        depth_req = min_depth_usd if min_depth_usd is not None else self.settings.min_order_book_depth_usd

        entry_spread = self._entry_spread_actual or 0.0
        logger.info(
            "=== EXIT PRE-TRADE CHECKS === spread_abs=$%.4f entry_spread=$%.4f pnl_est=$%.4f "
            "exec_cost=$%.4f slippage_cost=$%.4f max_slip=%.4f%% min_depth=$%.2f qty=%s",
            snapshot.spread_abs, entry_spread,
            (snapshot.spread_abs - entry_spread) * float(self.quantity),
            snapshot.exec_spread, snapshot.slippage_cost,
            max_slip, depth_req, self.quantity,
        )

        # --- FULL PRE-EXIT CHECK: fetch both books, check both legs before any order ---
        try:
            book_sell = self._get_client(sell_exchange).fetch_order_book(sell_sym, limit=50)
            book_buy = self._get_client(buy_exchange).fetch_order_book(buy_sym, limit=50)
            logger.info(
                "Order books fetched — SELL %s@%s best_bid=%s | BUY %s@%s best_ask=%s",
                sell_sym, sell_exchange,
                book_sell["bids"][0][0] if book_sell.get("bids") else "?",
                buy_sym, buy_exchange,
                book_buy["asks"][0][0] if book_buy.get("asks") else "?",
            )
        except Exception as exc:
            return ArbExecutionResult(
                success=False, leg_a=None, leg_b=None,
                snapshot=snapshot,
                error=f"Failed to fetch order books for exit: {exc}",
            )

        failures = []

        # --- Min order size check ---
        min_sell = self._get_client(sell_exchange).get_min_order_size(sell_sym)
        logger.info("Min order size check — SELL %s: qty=%s min=%s [%s]",
            sell_sym, self.quantity, min_sell, "PASS" if self.quantity >= min_sell else "FAIL")
        if self.quantity < min_sell:
            failures.append(
                f"SELL {sell_sym} qty {self.quantity} < min order size {min_sell} on {sell_exchange}"
            )

        min_buy = self._get_client(buy_exchange).get_min_order_size(buy_sym)
        logger.info("Min order size check — BUY %s: qty=%s min=%s [%s]",
            buy_sym, self.quantity, min_buy, "PASS" if self.quantity >= min_buy else "FAIL")
        if self.quantity < min_buy:
            failures.append(
                f"BUY {buy_sym} qty {self.quantity} < min order size {min_buy} on {buy_exchange}"
            )

        passed_sell, depth_sell, slip_sell = run_pre_trade_checks(
            order_book=book_sell, side="sell", quantity=self.quantity,
            expected_price=sell_price,
            max_slippage_pct=max_slip, min_depth_usd=depth_req,
        )
        logger.info(
            "SELL %s depth check: avail=$%.2f need=$%.2f [%s] | "
            "slippage: estimated=%.4f%% max=%.4f%% fill_price=%.4f [%s]",
            sell_sym,
            depth_sell.available_depth_usd, depth_sell.required_depth_usd,
            "PASS" if depth_sell.is_sufficient else "FAIL",
            slip_sell.slippage_pct, slip_sell.max_allowed_pct, slip_sell.estimated_fill_price,
            "PASS" if slip_sell.is_acceptable else "FAIL",
        )
        if not passed_sell:
            if not depth_sell.is_sufficient:
                failures.append(
                    f"SELL {sell_sym} depth: ${depth_sell.available_depth_usd:.2f} avail, "
                    f"need ${depth_sell.required_depth_usd:.2f}"
                )
            if not slip_sell.is_acceptable:
                failures.append(
                    f"SELL {sell_sym} slippage: {slip_sell.slippage_pct:.4f}% > max {slip_sell.max_allowed_pct:.4f}% "
                    f"(fill ${slip_sell.estimated_fill_price:.4f} vs expected ${sell_price:.4f})"
                )

        passed_buy, depth_buy, slip_buy = run_pre_trade_checks(
            order_book=book_buy, side="buy", quantity=self.quantity,
            expected_price=buy_price,
            max_slippage_pct=max_slip, min_depth_usd=depth_req,
        )
        logger.info(
            "BUY %s depth check: avail=$%.2f need=$%.2f [%s] | "
            "slippage: estimated=%.4f%% max=%.4f%% fill_price=%.4f [%s]",
            buy_sym,
            depth_buy.available_depth_usd, depth_buy.required_depth_usd,
            "PASS" if depth_buy.is_sufficient else "FAIL",
            slip_buy.slippage_pct, slip_buy.max_allowed_pct, slip_buy.estimated_fill_price,
            "PASS" if slip_buy.is_acceptable else "FAIL",
        )
        if not passed_buy:
            if not depth_buy.is_sufficient:
                failures.append(
                    f"BUY {buy_sym} depth: ${depth_buy.available_depth_usd:.2f} avail, "
                    f"need ${depth_buy.required_depth_usd:.2f}"
                )
            if not slip_buy.is_acceptable:
                failures.append(
                    f"BUY {buy_sym} slippage: {slip_buy.slippage_pct:.4f}% > max {slip_buy.max_allowed_pct:.4f}% "
                    f"(fill ${slip_buy.estimated_fill_price:.4f} vs expected ${buy_price:.4f})"
                )

        if failures:
            logger.warning("=== EXIT BLOCKED — %d check(s) failed: %s ===", len(failures), "; ".join(failures))
            return ArbExecutionResult(
                success=False, leg_a=None, leg_b=None,
                snapshot=snapshot,
                error=f"Exit pre-trade checks failed — no orders placed: {'; '.join(failures)}",
            )

        logger.info(
            "=== ALL EXIT CHECKS PASSED — SELL %s@%s / BUY %s@%s "
            "qty=%s spread_abs=$%.4f entry=$%.4f est_pnl=$%.4f exec_cost=$%.4f slip_cost=$%.4f ===",
            sell_sym, sell_exchange, buy_sym, buy_exchange,
            self.quantity, snapshot.spread_abs, entry_spread,
            (snapshot.spread_abs - entry_spread) * float(self.quantity),
            snapshot.exec_spread, snapshot.slippage_cost,
        )

        # --- PARALLEL EXIT LEG EXECUTION ---
        # For exit, long_sym = sell (close long), short_sym = buy (close short)
        # We reuse _execute_legs_parallel with reversed sides
        leg_a, leg_b = self._execute_legs_parallel(
            long_sym=buy_sym, short_sym=sell_sym,
            long_exchange=buy_exchange, short_exchange=sell_exchange,
            long_price=buy_price, short_price=sell_price,
            min_depth_usd=0.0, slippage_pct=999.0,
        )

        if not leg_a.success and not leg_b.success:
            # Both failed — position still fully open, keep state
            logger.warning("Both exit legs failed — position kept open.")
            return ArbExecutionResult(
                success=False, leg_a=leg_a, leg_b=leg_b,
                snapshot=snapshot,
                error=f"Both exit legs failed: A={leg_a.error}; B={leg_b.error}. Position kept open.",
            )

        if not leg_a.success:
            # Leg A (BUY buy_sym) failed but Leg B (SELL sell_sym) filled.
            # Re-open the sell leg to restore the position (re-buy sell_sym).
            logger.error(
                "Exit Leg A (BUY %s) failed — Leg B already sold %s. Re-opening SELL to restore position.",
                buy_sym, sell_sym,
            )
            self._unwind_leg(sell_sym, "buy", self.quantity, sell_price, sell_exchange)
            # Position state unchanged — still has_position
            return ArbExecutionResult(
                success=False, leg_a=leg_a, leg_b=leg_b,
                snapshot=snapshot,
                error=f"Exit Leg A (BUY {buy_sym}) failed — Leg B unwound back. Position restored.",
            )

        if not leg_b.success:
            # Leg B (SELL sell_sym) failed but Leg A (BUY buy_sym) filled.
            # Re-open the buy leg to restore the position (re-sell buy_sym).
            logger.error(
                "Exit Leg B (SELL %s) failed — Leg A already bought back %s. Re-opening BUY to restore position.",
                sell_sym, buy_sym,
            )
            self._unwind_leg(buy_sym, "sell", self.quantity, buy_price, buy_exchange)
            # Position state unchanged — still has_position
            return ArbExecutionResult(
                success=False, leg_a=leg_a, leg_b=leg_b,
                snapshot=snapshot,
                error=f"Exit Leg B (SELL {sell_sym}) failed — Leg A unwound back. Position restored.",
            )

        # --- Success: verify with exchange before clearing state ---
        logger.info("ARB EXIT orders sent — verifying with exchange...")
        self.sync_position_from_exchange()
        if self._has_position:
            logger.warning(
                "ARB EXIT: orders reported OK but exchange still shows open position "
                "(long=%s short=%s) — state kept open.", self._long_sym, self._short_sym,
            )
            return ArbExecutionResult(
                success=False, leg_a=leg_a, leg_b=leg_b,
                snapshot=snapshot,
                error=f"Exit orders sent but exchange still shows position open — manual check required.",
            )
        logger.info("ARB EXIT complete — exchange confirms both positions closed.")
        return ArbExecutionResult(
            success=True, leg_a=leg_a, leg_b=leg_b,
            snapshot=snapshot, error=None,
        )

    # ------------------------------------------------------------------
    # Parallel execution helper
    # ------------------------------------------------------------------

    def _execute_single_leg(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        expected_price: float,
        exchange_name: str,
        min_depth_usd: float,
        slippage_pct: float,
    ) -> TradeResult:
        """Execute a single leg using the configured order type."""
        leg_client = self._get_client(exchange_name)

        if self.order_type == "aggressive_limit":
            return self.executor.execute_aggressive_limit_order(
                symbol=symbol, side=side, quantity=quantity,
                expected_price=expected_price,
                offset_ticks=self.limit_offset_ticks,
                slippage_pct=slippage_pct, min_depth_usd=min_depth_usd,
                client=leg_client,
            )
        else:
            # Fallback: market order (chunked)
            return self._execute_leg_chunked(
                symbol=symbol, side=side, total_qty=quantity,
                expected_price=expected_price, exchange_name=exchange_name,
                min_depth_usd=min_depth_usd, slippage_pct=slippage_pct,
            )

    def _execute_legs_parallel(
        self,
        long_sym: str,
        short_sym: str,
        long_exchange: str,
        short_exchange: str,
        long_price: float,
        short_price: float,
        min_depth_usd: float,
        slippage_pct: float,
    ) -> tuple[TradeResult, TradeResult]:
        """Execute both legs in parallel using ThreadPoolExecutor.

        Returns (leg_a_result, leg_b_result). Caller handles unwind logic.
        """
        t_start = time.time()

        with ThreadPoolExecutor(max_workers=2) as pool:
            future_a = pool.submit(
                self._execute_single_leg,
                symbol=long_sym, side="buy", quantity=self.quantity,
                expected_price=long_price, exchange_name=long_exchange,
                min_depth_usd=min_depth_usd, slippage_pct=slippage_pct,
            )
            future_b = pool.submit(
                self._execute_single_leg,
                symbol=short_sym, side="sell", quantity=self.quantity,
                expected_price=short_price, exchange_name=short_exchange,
                min_depth_usd=min_depth_usd, slippage_pct=slippage_pct,
            )

            # Wait for both to complete
            try:
                leg_a = future_a.result(timeout=30)
            except Exception as exc:
                leg_a = TradeResult(
                    success=False, order_response=None,
                    depth=None, slippage=None,
                    error=f"Leg A exception: {exc}",
                )

            try:
                leg_b = future_b.result(timeout=30)
            except Exception as exc:
                leg_b = TradeResult(
                    success=False, order_response=None,
                    depth=None, slippage=None,
                    error=f"Leg B exception: {exc}",
                )

        elapsed_ms = (time.time() - t_start) * 1000
        logger.info(
            "Parallel execution done in %.0fms — Leg A (%s %s): %s | Leg B (%s %s): %s",
            elapsed_ms,
            "BUY", long_sym, "OK" if leg_a.success else f"FAIL: {leg_a.error}",
            "SELL", short_sym, "OK" if leg_b.success else f"FAIL: {leg_b.error}",
        )

        return leg_a, leg_b

    def _unwind_leg(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        expected_price: float,
        exchange_name: str,
    ) -> TradeResult:
        """Emergency unwind: close a filled leg using market order (max slippage)."""
        logger.warning(
            "UNWIND: %s %s qty=%s @ ~%.4f on %s",
            side.upper(), symbol, quantity, expected_price, exchange_name,
        )
        leg_client = self._get_client(exchange_name)
        try:
            result = self.executor.execute_market_order(
                symbol=symbol, side=side, quantity=quantity,
                expected_price=expected_price,
                slippage_pct=self.settings.max_slippage_pct,
                min_depth_usd=0.0,
                client=leg_client,
            )
            if result.success:
                logger.info("UNWIND successful: %s %s", side.upper(), symbol)
            else:
                logger.error("UNWIND FAILED: %s %s — %s", side.upper(), symbol, result.error)
            return result
        except Exception as exc:
            logger.error("UNWIND EXCEPTION: %s %s — %s", side.upper(), symbol, exc)
            return TradeResult(
                success=False, order_response=None,
                depth=None, slippage=None,
                error=f"Unwind exception: {exc}",
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
        exchange_name: str | None = None,
        min_depth_usd: float | None = None,
        slippage_pct: float | None = None,
    ) -> TradeResult:
        """Execute a leg in chunks to reduce market impact.

        Splits total_qty into pieces of self.chunk_size, executes each as a
        separate market order, and waits self.chunk_delay_ms between them.
        Returns an aggregated TradeResult.
        """
        leg_client = self._get_client(exchange_name) if exchange_name else None
        chunk = self.chunk_size
        if chunk <= 0 or chunk >= total_qty:
            # No chunking needed — single order
            return self.executor.execute_market_order(
                symbol=symbol, side=side, quantity=total_qty,
                expected_price=expected_price,
                min_depth_usd=min_depth_usd, slippage_pct=slippage_pct,
                client=leg_client,
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
                client=leg_client,
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
        exchange_name: str | None = None,
    ) -> TradeResult:
        """Try to close a leg with constant slippage.

        Only retries on transient API/network errors (e.g. order rejected,
        timeout). Does NOT escalate slippage — if slippage is too high the
        position stays open to protect profits.
        """
        leg_client = self._get_client(exchange_name) if exchange_name else None
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
                client=leg_client,
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
