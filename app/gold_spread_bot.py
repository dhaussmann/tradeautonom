"""Gold-spread convergence bot — PAXG vs XAUT on Variational.

Strategy
--------
Both PAXG (Paxos Gold) and XAUT (Tether Gold) represent one ounce of physical
gold; they are economically identical assets. On Variational the perp prices
typically diverge by a few USD because liquidity providers quote them
independently. Empirically PAXG > XAUT, and the spread mean-reverts on a
timescale of minutes to hours.

The bot opens a market-neutral pair when the spread is *wide* and unwinds
when it has *converged*:

    Entry (state IDLE/MONITORING → HOLDING):
        if (paxg_mid - xaut_mid) >= entry_spread for N consecutive ticks
            SELL  paxg_qty  on Variational  (P-PAXG-USDC-3600, side=sell)
            BUY   xaut_qty  on Variational  (P-XAUT-USDC-3600, side=buy)

    Exit (state HOLDING → IDLE):
        if (paxg_mid - xaut_mid) <= exit_spread for N consecutive ticks
            BUY   paxg_qty  on Variational  (close short)
            SELL  xaut_qty  on Variational  (close long)

Only Variational is involved, so there is no cross-exchange leg or
maker/taker handoff. Variational trading is RFQ + IOC only (taker), so when
Phase 2 execution lands the entry/exit orders are placed via two
``async_create_ioc_order`` calls fired in parallel via ``asyncio.gather``.

Phase 1 (this revision)
    Monitoring + signal detection + activity logging + UI integration.
    The execution methods are stubbed to log the would-be order without
    touching the exchange. Set ``simulation=False`` later in Phase 2 to
    enable real fills.

Data source
    The bot polls the OMS-v2 REST endpoint
    ``GET /gold-spread/latest`` (added in
    ``deploy/cf-containers/oms-v2/src/aggregator.ts``) which returns the
    most recent in-memory PAXG/XAUT mid+bid+ask snapshot. Falls back to
    polling the per-exchange book endpoints if the aggregate endpoint is
    not reachable.

State persistence
    JSON in ``data/gold_spread/state.json`` (config + position + last
    activity entries). Survives container restarts; the bot resumes in its
    previous state after ``start()``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, AsyncGenerator
import urllib.error
import urllib.request

logger = logging.getLogger("tradeautonom.gold_spread_bot")

# Persist state to data/gold_spread/
_STATE_DIR = Path("data/gold_spread")

# Variational symbols. The bot does not actually need these for OMS polling
# (it uses /gold-spread/latest which works regardless of suffix), but they
# are returned in /status for the UI and used by Phase-2 trading code as
# defaults. Variational rotates the funding-interval suffix periodically;
# the gold pair currently uses 14400 (4h). If you see stale 3600 symbols
# in your config, app/symbol_resolver.py auto-corrects them at bot start.
PAXG_SYMBOL = "P-PAXG-USDC-14400"
XAUT_SYMBOL = "P-XAUT-USDC-14400"
EXCHANGE = "variational"

# Number of in-memory live snapshots to keep for the SSE stream and chart
# bootstrap before the user-side history endpoint catches up.
_LIVE_HISTORY_MAX = 720  # ≈ 1 h at 5 s tick interval

# OMS HTTP request UA — Cloudflare's bot-fight-mode 403s the default urllib UA.
_OMS_HEADERS = {"User-Agent": "TradeAutonom/GoldSpreadBot/1.0"}


# ── Data classes ──────────────────────────────────────────────────


class State(str, Enum):
    """High-level lifecycle. Mirrors the simple state model used by the
    legacy ArbitrageEngine plus an explicit MONITORING substate so the UI
    can distinguish "stopped" from "watching for entry"."""

    IDLE = "IDLE"
    MONITORING = "MONITORING"
    ENTERING = "ENTERING"
    HOLDING = "HOLDING"
    EXITING = "EXITING"
    ERROR = "ERROR"


@dataclass
class GoldSpreadConfig:
    """Configuration for the Gold-Spread singleton."""

    # Instruments — frozen for the gold pair.
    paxg_symbol: str = PAXG_SYMBOL
    xaut_symbol: str = XAUT_SYMBOL
    exchange: str = EXCHANGE

    # Strategy thresholds (USD by default; set in_pct=True to read as percent).
    entry_spread: float = 15.0           # Open when spread ≥ entry_spread
    exit_spread: float = 5.0             # Close when spread ≤ exit_spread
    threshold_in_pct: bool = False       # If True thresholds are spread_pct values

    # Position sizing.
    quantity: float = 1.0                # base-asset qty per leg
    max_slippage_pct: float = 0.7        # IOC slippage cap for normal trades (0.7 = 0.7%)

    # Signal robustness.
    signal_confirmations: int = 3        # consecutive ticks before acting
    tick_interval_s: float = 5.0         # how often to poll OMS

    # Phase 2: Execution safety
    # Cap on per-leg notional so a misconfigured ``quantity`` can't size into
    # a tens-of-thousands USD position by accident. Computed at entry time as
    # ``quantity * max(short_entry_price, long_entry_price)`` and rejected if
    # it exceeds this value.
    max_position_value_usd: float = 10000.0
    # Wall-clock cap on the parallel ``asyncio.gather`` of the two IOC orders
    # plus their fill-verification step. If exceeded the bot enters a
    # defensive sweep that fetches positions and unwinds anything that
    # filled — better to take a small slippage hit than sit on an unhedged
    # leg indefinitely.
    execution_timeout_s: float = 30.0
    # Slippage cap used for the *unwind* leg after a one-sided fill. Wider
    # than ``max_slippage_pct`` because at unwind time we already carry
    # uncovered directional risk and need a guaranteed fill more than a
    # tight price. 2% on $4 600 gold ≈ $92 worst case.
    unwind_slippage_pct: float = 2.0
    # Brief settle window after a fill before we poll order/position APIs.
    fill_verify_delay_s: float = 0.5
    # Fix 6: After a successful entry fill, recompute the actual exec
    # spread from the real fills (via position API) and abort+unwind if
    # the captured spread is less than this fraction of the entry
    # threshold. Catches cases where the OMS snapshot looked tradable
    # but the real RFQ fills came out far worse.
    min_actual_spread_ratio: float = 0.5
    # Fix 7: How tight the rolling spread must be over the last N
    # confirmation ticks before we trust the signal. Expressed as the
    # max allowed std-dev / mean ratio of the exec_spread across the
    # confirmation window. 0 disables the check.
    max_spread_volatility_ratio: float = 0.5

    # Operational.
    simulation: bool = True              # Phase 1 default = paper trade
    oms_url: str = "https://oms-v2.defitool.de"
    bot_id: str = "gold-spread"


@dataclass
class GoldSpreadSnapshot:
    """One spread evaluation tick.

    All trading-relevant spread values are computed from real bid/ask
    prices, never from mids. ``spread`` and ``exec_spread`` are the same
    number — kept as separate fields only for analytics back-compat. Mids
    are retained for display only.

    The ``direction`` field indicates which leg assignment yields the
    positive executable spread right now (= which token to short). It
    flips automatically when XAUT becomes the premium token.
    """

    ts: float
    paxg_mid: float
    paxg_bid: float
    paxg_ask: float
    xaut_mid: float
    xaut_bid: float
    xaut_ask: float
    # Executable cross-token entry spread = max(paxg_bid − xaut_ask,
    # xaut_bid − paxg_ask). Same value as exec_spread; this is the chart
    # / threshold value.
    spread: float
    spread_pct: float
    # Direction-aware entry execution spread (= spread).
    #   if paxg_premium: paxg_bid - xaut_ask   (short PAXG @ bid, long XAUT @ ask)
    #   if xaut_premium: xaut_bid - paxg_ask   (short XAUT @ bid, long PAXG @ ask)
    exec_spread: float = 0.0
    # Direction-aware exit execution spread (reverse direction's exec).
    exit_exec_spread: float = 0.0
    # "paxg_premium" | "xaut_premium"
    direction: str = "paxg_premium"
    signal: str = "NONE"  # ENTRY | EXIT | HOLD | NONE


@dataclass
class GoldSpreadPosition:
    """In-memory position tracking.

    The leg layout is direction-aware: ``short_token`` always holds the
    token that traded at a premium when the position was opened (we sold
    it), ``long_token`` the token that was the discount side (we bought
    it). This keeps the position symmetric regardless of whether PAXG or
    XAUT was the premium token at entry.

    Phase 1 stores stub values when simulation=True; Phase 2 will fill
    these from real exchange fills.
    """

    opened_at: float
    direction: str  # "paxg_premium" | "xaut_premium" — which token we shorted
    short_token: str  # "PAXG" | "XAUT"
    short_symbol: str
    short_qty: float
    short_entry_price: float
    long_token: str  # "PAXG" | "XAUT"
    long_symbol: str
    long_qty: float
    long_entry_price: float
    entry_spread: float  # exec_spread captured at entry (unsigned)
    simulation: bool = True
    # Optional exchange order IDs (Phase 2).
    short_order_id: str | None = None
    long_order_id: str | None = None


# ── Bot ───────────────────────────────────────────────────────────


class GoldSpreadBot:
    """Singleton bot tracking PAXG/XAUT spread on Variational."""

    def __init__(
        self,
        config: GoldSpreadConfig,
        clients: dict[str, Any],
        activity_forwarder: Any | None = None,
    ) -> None:
        self.config = config
        self._clients = clients
        self._activity_forwarder = activity_forwarder

        # State
        self._state: State = State.IDLE
        self._position: GoldSpreadPosition | None = None
        self._last_snapshot: GoldSpreadSnapshot | None = None
        self._live_history: list[GoldSpreadSnapshot] = []
        self._activity_log: list[dict] = []
        self._error: str | None = None

        # Signal confirmation counter
        self._signal_count: int = 0
        self._last_signal: str = "NONE"

        # Epoch-ms when the active position was opened. Used by the exit
        # path to scope trade-history and funding-payment queries to the
        # current position. ``None`` while flat.
        self._entry_started_ms: int | None = None

        # Async task handle
        self._loop_task: asyncio.Task | None = None
        self._running = False

        # Restore from disk
        self._load_state()

    # ── Lifecycle ──────────────────────────────────────────────────

    async def start(self) -> dict:
        """Begin the monitoring loop. Idempotent.

        When ``simulation=False`` we first reconcile our local position
        with whatever Variational actually shows. This covers two cases:
        (1) a previous container died mid-trade and the persisted state
        is stale; (2) the user opened a hedge manually and now wants the
        bot to manage exit. See ``_sync_positions_from_exchange``.
        """
        if self._running:
            return {"started": False, "reason": "already running"}

        self._running = True
        self._error = None
        # Resume HOLDING if a position survived a restart, otherwise MONITORING.
        if self._position is not None:
            self._state = State.HOLDING
        else:
            self._state = State.MONITORING

        # Live-trading bootstrap: pull real positions from Variational so
        # we never start blind to an existing exposure.
        if not self.config.simulation:
            try:
                await self._sync_positions_from_exchange()
            except Exception as exc:
                logger.warning("Gold-Spread startup position sync failed: %s", exc)
                self._log_activity(
                    "position_sync_failed",
                    f"Startup sync raised: {exc} — continuing with persisted state",
                )

        self._loop_task = asyncio.create_task(
            self._monitor_loop(), name="gold-spread-monitor",
        )
        self._log_activity(
            "started",
            f"Gold-Spread bot started (entry={self.config.entry_spread}, "
            f"exit={self.config.exit_spread}, qty={self.config.quantity}, "
            f"sim={self.config.simulation})",
        )
        self._save_state()
        return {"started": True, "state": self._state.value}

    async def stop(self) -> dict:
        """Stop the monitoring loop. Does NOT close any open position;
        positions remain on Variational and can be exited manually or by
        restarting the bot which will resume in HOLDING."""
        if not self._running:
            return {"stopped": False, "reason": "not running"}

        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
            self._loop_task = None
        self._state = State.IDLE if self._position is None else State.HOLDING
        self._log_activity("stopped", "Gold-Spread bot stopped")
        self._save_state()
        return {"stopped": True, "state": self._state.value}

    def reset(self) -> dict:
        """Force-clear position tracking and signal counters. Does NOT
        cancel exchange orders — use ``stop()`` first."""
        old_pos = self._position
        self._position = None
        self._entry_started_ms = None
        self._signal_count = 0
        self._last_signal = "NONE"
        self._state = State.IDLE if not self._running else State.MONITORING
        self._error = None
        self._log_activity(
            "reset",
            f"State reset (had_position={old_pos is not None})",
        )
        self._save_state()
        return {"reset": True, "state": self._state.value}

    # ── Config management ──────────────────────────────────────────

    def update_config(self, updates: dict[str, Any]) -> dict:
        """Hot-update selected config fields. Returns the new full config."""
        allowed = {
            "entry_spread", "exit_spread", "threshold_in_pct",
            "quantity", "max_slippage_pct",
            "signal_confirmations", "tick_interval_s",
            "simulation",
            "max_position_value_usd", "execution_timeout_s",
            "unwind_slippage_pct", "fill_verify_delay_s",
            "min_actual_spread_ratio", "max_spread_volatility_ratio",
        }
        applied: dict[str, Any] = {}
        rejected: dict[str, Any] = {}
        float_keys = {
            "entry_spread", "exit_spread", "quantity",
            "max_slippage_pct", "tick_interval_s",
            "max_position_value_usd", "execution_timeout_s",
            "unwind_slippage_pct", "fill_verify_delay_s",
            "min_actual_spread_ratio", "max_spread_volatility_ratio",
        }
        for key, val in updates.items():
            if key not in allowed:
                rejected[key] = "unknown or read-only"
                continue
            try:
                if key in float_keys:
                    setattr(self.config, key, float(val))
                elif key == "signal_confirmations":
                    setattr(self.config, key, int(val))
                elif key in ("threshold_in_pct", "simulation"):
                    setattr(self.config, key, bool(val))
                applied[key] = val
            except (TypeError, ValueError) as exc:
                rejected[key] = f"invalid value: {exc}"

        if applied:
            # Reset signal counter so a stale opposite signal can't fire on
            # the very next tick after a threshold widening.
            self._signal_count = 0
            self._last_signal = "NONE"
            self._log_activity(
                "config_updated",
                "Config updated: "
                + ", ".join(f"{k}={v}" for k, v in applied.items()),
            )
            self._save_state()
        return {"applied": applied, "rejected": rejected, "config": asdict(self.config)}

    # ── Status / SSE ───────────────────────────────────────────────

    def get_status(self) -> dict:
        """Snapshot of everything the UI needs in one REST call."""
        return {
            "state": self._state.value,
            "running": self._running,
            "error": self._error,
            "config": asdict(self.config),
            "spread": (
                self._snapshot_to_dict(self._last_snapshot)
                if self._last_snapshot else None
            ),
            "position": (
                asdict(self._position) if self._position else None
            ),
            "signal_count": self._signal_count,
            "last_signal": self._last_signal,
            "live_history": [
                self._snapshot_to_dict(s)
                for s in self._live_history[-_LIVE_HISTORY_MAX:]
            ],
            "activity": self._activity_log[-200:],
        }

    async def stream_status(
        self, interval_ms: int = 2000,
    ) -> AsyncGenerator[str, None]:
        """SSE generator: emit the current status JSON every ``interval_ms``."""
        interval_s = max(0.5, interval_ms / 1000)
        try:
            while True:
                payload = json.dumps(self.get_status(), default=str)
                yield f"data: {payload}\n\n"
                await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return

    # ── Monitor loop ───────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        """Poll OMS, compute spread, evaluate signal, optionally execute."""
        logger.info("Gold-Spread monitor loop starting")
        consecutive_errors = 0
        while self._running:
            try:
                snap = await self._fetch_spread_snapshot()
                if snap is None:
                    consecutive_errors += 1
                    if consecutive_errors == 3:
                        self._log_activity(
                            "feed_stale",
                            "OMS gold-spread feed unavailable for 3 consecutive ticks",
                        )
                    await asyncio.sleep(self.config.tick_interval_s)
                    continue
                consecutive_errors = 0

                signal = self._evaluate(snap)
                snap.signal = signal
                self._last_snapshot = snap
                self._live_history.append(snap)
                if len(self._live_history) > _LIVE_HISTORY_MAX:
                    self._live_history = self._live_history[-_LIVE_HISTORY_MAX:]

                if signal == "ENTRY" and self._state == State.MONITORING:
                    await self._handle_entry_signal(snap)
                elif signal == "EXIT" and self._state == State.HOLDING:
                    await self._handle_exit_signal(snap)
            except asyncio.CancelledError:
                logger.info("Gold-Spread monitor loop cancelled")
                raise
            except Exception as exc:
                logger.exception("Gold-Spread monitor tick failed: %s", exc)
                self._error = str(exc)
                self._log_activity("error", f"Monitor tick failed: {exc}")

            await asyncio.sleep(self.config.tick_interval_s)

    async def _fetch_spread_snapshot(self) -> GoldSpreadSnapshot | None:
        """Pull the latest PAXG/XAUT data from the OMS aggregate endpoint.

        Falls back to per-symbol book endpoints if /gold-spread/latest is
        unavailable (e.g. very fresh OMS deploy, no data yet).
        """
        url = self.config.oms_url.rstrip("/") + "/gold-spread/latest"
        body = await self._http_get_json(url)
        if body and "spread" in body:
            paxg_mid = float(body.get("paxg_mid", 0))
            paxg_bid = float(body.get("paxg_bid", 0))
            paxg_ask = float(body.get("paxg_ask", 0))
            xaut_mid = float(body.get("xaut_mid", 0))
            xaut_bid = float(body.get("xaut_bid", 0))
            xaut_ask = float(body.get("xaut_ask", 0))
            # Compute the bid/ask-based direction locally so we don't
            # depend on the OMS having the new code deployed. The OMS
            # values are used preferentially when present.
            paxg_prem_exec = paxg_bid - xaut_ask
            xaut_prem_exec = xaut_bid - paxg_ask
            if paxg_prem_exec >= xaut_prem_exec:
                local_dir, local_exec, local_exit = "paxg_premium", paxg_prem_exec, xaut_prem_exec
            else:
                local_dir, local_exec, local_exit = "xaut_premium", xaut_prem_exec, paxg_prem_exec
            direction = str(body.get("direction") or local_dir)
            return GoldSpreadSnapshot(
                ts=time.time(),
                paxg_mid=paxg_mid,
                paxg_bid=paxg_bid,
                paxg_ask=paxg_ask,
                xaut_mid=xaut_mid,
                xaut_bid=xaut_bid,
                xaut_ask=xaut_ask,
                spread=float(body.get("spread", local_exec)),
                spread_pct=float(body.get("spread_pct", 0)),
                exec_spread=float(body.get("exec_spread", local_exec)),
                exit_exec_spread=float(body.get("exit_exec_spread", local_exit)),
                direction=direction,
            )

        # Fallback: build snapshot from two book calls.
        base = self.config.oms_url.rstrip("/")
        paxg_url = f"{base}/book/{self.config.exchange}/{self.config.paxg_symbol}"
        xaut_url = f"{base}/book/{self.config.exchange}/{self.config.xaut_symbol}"
        paxg_book, xaut_book = await asyncio.gather(
            self._http_get_json(paxg_url),
            self._http_get_json(xaut_url),
            return_exceptions=False,
        )
        if not paxg_book or not xaut_book:
            return None
        try:
            paxg_bid = float(paxg_book["bids"][0][0])
            paxg_ask = float(paxg_book["asks"][0][0])
            xaut_bid = float(xaut_book["bids"][0][0])
            xaut_ask = float(xaut_book["asks"][0][0])
        except (KeyError, IndexError, ValueError, TypeError):
            return None
        paxg_mid = (paxg_bid + paxg_ask) / 2
        xaut_mid = (xaut_bid + xaut_ask) / 2
        if paxg_mid <= 0 or xaut_mid <= 0:
            return None
        # Direction follows the executable spread, not the mid order. When
        # both candidates are negative (bid/ask costs > cross-gap), we
        # still pick the larger one so the chart never jumps to a worse
        # number; the bot's entry threshold filters those out.
        paxg_prem_exec = paxg_bid - xaut_ask
        xaut_prem_exec = xaut_bid - paxg_ask
        if paxg_prem_exec >= xaut_prem_exec:
            direction = "paxg_premium"
            exec_spread = paxg_prem_exec
            exit_exec_spread = xaut_prem_exec
        else:
            direction = "xaut_premium"
            exec_spread = xaut_prem_exec
            exit_exec_spread = paxg_prem_exec
        ref_mid = min(paxg_mid, xaut_mid)
        return GoldSpreadSnapshot(
            ts=time.time(),
            paxg_mid=paxg_mid, paxg_bid=paxg_bid, paxg_ask=paxg_ask,
            xaut_mid=xaut_mid, xaut_bid=xaut_bid, xaut_ask=xaut_ask,
            spread=exec_spread,
            spread_pct=(exec_spread / ref_mid * 100) if ref_mid > 0 else 0.0,
            exec_spread=exec_spread,
            exit_exec_spread=exit_exec_spread,
            direction=direction,
        )

    @staticmethod
    async def _http_get_json(url: str, timeout: float = 5.0) -> dict | None:
        def _do() -> dict | None:
            req = urllib.request.Request(url, method="GET", headers=_OMS_HEADERS)
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    if resp.status != 200:
                        return None
                    return json.loads(resp.read().decode())
            except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
                return None

        try:
            return await asyncio.to_thread(_do)
        except Exception:
            return None

    # ── Signal logic ───────────────────────────────────────────────

    def _evaluate(self, snap: GoldSpreadSnapshot) -> str:
        """Return ENTRY / EXIT / HOLD / NONE for this tick.

        Uses **execution spreads** (bid/ask) rather than mid-spreads so the
        signal accounts for the real cost of entering/exiting the position:
          - Entry signal: ``exec_spread`` (paxg_bid − xaut_ask) ≥ threshold
          - Exit signal:  ``exit_exec_spread`` (paxg_ask − xaut_bid) ≤ threshold

        Confirmation: a signal must repeat for ``config.signal_confirmations``
        ticks before the loop acts on it. Otherwise we return HOLD.
        """
        raw_signal: str
        if self._state == State.HOLDING:
            # Fix 3: Use position-aware exit spread, not the snapshot's
            # `exit_exec_spread`. The latter is computed for the current
            # live direction and is wrong when the premium has flipped
            # during our hold period.
            position_exit = self._position_aware_exit_spread(snap)
            raw_signal = "EXIT" if position_exit <= self.config.exit_spread else "HOLD"
        elif self._state in (State.MONITORING, State.IDLE):
            # Compare the entry execution spread against the entry threshold.
            raw_signal = "ENTRY" if snap.exec_spread >= self.config.entry_spread else "HOLD"
        else:
            raw_signal = "NONE"

        # Confirmation counter
        if raw_signal in ("ENTRY", "EXIT"):
            if raw_signal == self._last_signal:
                self._signal_count += 1
            else:
                self._last_signal = raw_signal
                self._signal_count = 1
            if self._signal_count >= self.config.signal_confirmations:
                return raw_signal
            return "HOLD"
        else:
            # HOLD or NONE resets the counter.
            self._last_signal = raw_signal
            self._signal_count = 0
            return raw_signal

    # ── Execution helpers (Phase 2) ────────────────────────────────

    def _get_client(self) -> Any | None:
        """Return the configured exchange client (Variational) or None."""
        return self._clients.get(self.config.exchange)

    @staticmethod
    def _is_filled(result: Any) -> bool:
        """Order considered filled iff the call returned a dict and the
        position-delta-verified ``traded_qty`` is positive."""
        if isinstance(result, Exception) or not isinstance(result, dict):
            return False
        try:
            return float(result.get("traded_qty") or 0.0) > 0.0
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _result_status(result: Any) -> str:
        if isinstance(result, Exception):
            return f"EXC:{type(result).__name__}"
        if isinstance(result, dict):
            return str(result.get("status") or "?")
        return "?"

    async def _fetch_fill_price_from_position(
        self, client: Any, symbol: str, fallback: float,
    ) -> float:
        """Resolve the actual fill price by reading the position's
        ``entry_price`` from Variational's position API.

        This is the single source of truth Variational uses internally
        for PnL — much more robust than polling order/trade history,
        which suffers from ID matching issues and pagination delays.

        Used for entry fills: after an IOC buy/sell, the new position's
        ``entry_price`` is the volume-weighted average fill price.

        Returns ``fallback`` if the position is missing or
        ``entry_price`` is empty.
        """
        try:
            positions = await client.async_fetch_positions(
                symbols=[symbol], max_retries=3,
            )
        except Exception as exc:
            logger.warning(
                "Gold-Spread: position lookup failed for %s: %s",
                symbol, exc,
            )
            return fallback
        for pos in positions or []:
            if (pos.get("symbol") == symbol
                    or pos.get("instrument") == symbol):
                ep = pos.get("entry_price")
                try:
                    val = float(ep) if ep not in (None, "") else 0.0
                except (TypeError, ValueError):
                    val = 0.0
                if val > 0:
                    return val
        return fallback

    async def _fetch_fill_price_and_fee_from_trades(
        self, client: Any, order_id: str, qty: float,
        fallback_price: float, since_ms: int,
    ) -> tuple[float, float]:
        """Resolve fill price + total fee for an order_id by polling
        Variational's trade history (``GET /trades``).

        Used for *exit* fills where the position no longer exists after
        the close, so the position API can no longer carry the price.
        Returns ``(price, fee)``; on lookup failure returns
        ``(fallback_price, 0.0)``.

        ``since_ms`` is passed through to the trade-history query so we
        don't sift through unrelated historical fills. Pass the entry
        timestamp for an exit lookup, or current time minus a few seconds
        for an entry lookup.
        """
        if not order_id:
            return fallback_price, 0.0
        try:
            trades = await client.async_fetch_trade_history(
                since_ms=since_ms, limit=200,
            )
        except Exception as exc:
            logger.warning(
                "Gold-Spread: trade history fetch failed for order %s: %s",
                order_id, exc,
            )
            return fallback_price, 0.0
        # Variational's trade history may split a single order into
        # multiple fills. Aggregate by exchange_order_id.
        matched: list[dict] = []
        for row in trades or []:
            row_id = str(row.get("exchange_order_id") or "")
            if row_id and row_id == str(order_id):
                matched.append(row)
        if not matched:
            return fallback_price, 0.0
        total_qty = 0.0
        total_value = 0.0
        total_fee = 0.0
        for row in matched:
            try:
                p = float(row.get("price") or 0)
                q = float(row.get("qty") or 0)
                f = float(row.get("fee") or 0)
            except (TypeError, ValueError):
                continue
            if p <= 0 or q <= 0:
                continue
            total_value += p * q
            total_qty += q
            total_fee += f
        if total_qty <= 0:
            return fallback_price, 0.0
        vwap = total_value / total_qty
        return vwap, total_fee

    async def _fetch_funding_paid(
        self, client: Any, symbol: str, since_ms: int,
    ) -> float:
        """Sum of funding payments for ``symbol`` since ``since_ms``.

        Returns 0 on lookup failure rather than blocking PnL reporting.
        Used to subtract funding from realized PnL on close.
        """
        try:
            payments = await client.async_fetch_funding_payments(
                since_ms=since_ms, limit=200,
            )
        except Exception as exc:
            logger.warning(
                "Gold-Spread: funding lookup failed for %s: %s", symbol, exc,
            )
            return 0.0
        total = 0.0
        for row in payments or []:
            row_sym = row.get("symbol") or row.get("instrument") or ""
            row_token = row.get("token") or row.get("underlying") or ""
            if (row_sym == symbol or row_sym.endswith(symbol)
                    or (row_token and row_token in symbol)):
                try:
                    total += float(row.get("amount") or row.get("value") or 0)
                except (TypeError, ValueError):
                    continue
        return total

    async def _unwind_leg(
        self, symbol: str, side: str, qty: float, expected_price: float,
        reason: str,
    ) -> dict[str, Any]:
        """Reverse a filled leg with widened slippage to maximise the chance
        of an immediate fill. Used after one-sided execution failures and
        timeout sweeps. ``side`` is the *unwind* side: pass "buy" to close
        a short, "sell" to close a long."""
        client = self._get_client()
        if client is None:
            self._log_activity(
                "unwind_failed",
                f"No {self.config.exchange} client available for unwind ({reason})",
            )
            return {"unwound": False, "reason": "no_client"}
        slippage = self.config.unwind_slippage_pct / 100.0
        try:
            result = await client.async_create_ioc_order(
                symbol, side,
                Decimal(str(qty)),
                Decimal(str(expected_price)),
                reduce_only=True,
                max_slippage=slippage,
            )
        except Exception as exc:
            self._log_activity(
                "unwind_failed",
                f"Unwind {side} {qty} {symbol} raised: {exc} ({reason})",
            )
            return {"unwound": False, "error": str(exc)}
        ok = self._is_filled(result)
        self._log_activity(
            "unwound" if ok else "unwind_failed",
            f"Unwind {side} {qty:.4f} {symbol} ({reason}): "
            f"status={self._result_status(result)} "
            f"traded_qty={result.get('traded_qty', 0) if isinstance(result, dict) else 0}",
        )
        return {"unwound": ok, "result": result if isinstance(result, dict) else None}

    async def _sweep_after_timeout(
        self,
        short_symbol: str, short_expected_price: float,
        long_symbol: str, long_expected_price: float,
        reason: str,
    ) -> None:
        """Defensive sweep after an execution_timeout_s breach: query
        Variational for both legs and unwind any unexpected exposure.

        We can't trust the in-flight ``asyncio.gather`` results in this
        case (one or both coroutines may have been mid-flight when the
        timeout fired) so go straight to the position API."""
        client = self._get_client()
        if client is None:
            return
        try:
            positions = await client.async_fetch_positions(
                symbols=[short_symbol, long_symbol], max_retries=3,
            )
        except Exception as exc:
            self._log_activity(
                "sweep_failed",
                f"Position sweep failed after {reason}: {exc}",
            )
            return
        by_symbol = {p.get("symbol") or p.get("instrument"): p for p in positions or []}
        # Short leg expected: position with side == "short". If we see
        # one we need to buy it back to flatten.
        short_pos = by_symbol.get(short_symbol)
        if short_pos and short_pos.get("side") == "short":
            qty = float(short_pos.get("size") or 0.0)
            if qty > 0:
                await self._unwind_leg(
                    short_symbol, "buy", qty, short_expected_price,
                    reason=f"sweep-after-{reason}",
                )
        # Long leg expected: position with side == "long" → sell to flatten.
        long_pos = by_symbol.get(long_symbol)
        if long_pos and long_pos.get("side") == "long":
            qty = float(long_pos.get("size") or 0.0)
            if qty > 0:
                await self._unwind_leg(
                    long_symbol, "sell", qty, long_expected_price,
                    reason=f"sweep-after-{reason}",
                )

    async def _sync_positions_from_exchange(self) -> None:
        """Reconcile the in-memory position with whatever Variational
        actually shows. Called on bot start when ``simulation=False`` so a
        crash mid-trade can't leave us blind to a real exposure."""
        client = self._get_client()
        if client is None:
            return
        try:
            positions = await client.async_fetch_positions(
                symbols=[self.config.paxg_symbol, self.config.xaut_symbol],
                max_retries=3,
            )
        except Exception as exc:
            self._log_activity(
                "position_sync_failed",
                f"Could not fetch Variational positions on startup: {exc}",
            )
            return
        by_symbol = {p.get("symbol") or p.get("instrument"): p for p in positions or []}
        paxg_pos = by_symbol.get(self.config.paxg_symbol)
        xaut_pos = by_symbol.get(self.config.xaut_symbol)
        paxg_size = float(paxg_pos.get("size") or 0) if paxg_pos else 0.0
        xaut_size = float(xaut_pos.get("size") or 0) if xaut_pos else 0.0

        if paxg_size <= 0 and xaut_size <= 0:
            # Exchange shows flat — drop any stale persisted position.
            if self._position is not None:
                self._log_activity(
                    "position_sync",
                    "Exchange shows flat but local state had a position — clearing.",
                )
                self._position = None
                self._state = State.MONITORING
                self._save_state()
            return

        # Either or both legs exist. If our state is empty, adopt them.
        if self._position is None:
            paxg_side = paxg_pos.get("side") if paxg_pos else None
            xaut_side = xaut_pos.get("side") if xaut_pos else None
            # Only adopt a clean delta-neutral hedge.
            if paxg_side == "short" and xaut_side == "long":
                self._adopt_position(
                    direction="paxg_premium",
                    short_token="PAXG", short_symbol=self.config.paxg_symbol,
                    short_qty=paxg_size,
                    short_entry_price=float(paxg_pos.get("entry_price") or 0),
                    long_token="XAUT", long_symbol=self.config.xaut_symbol,
                    long_qty=xaut_size,
                    long_entry_price=float(xaut_pos.get("entry_price") or 0),
                )
            elif xaut_side == "short" and paxg_side == "long":
                self._adopt_position(
                    direction="xaut_premium",
                    short_token="XAUT", short_symbol=self.config.xaut_symbol,
                    short_qty=xaut_size,
                    short_entry_price=float(xaut_pos.get("entry_price") or 0),
                    long_token="PAXG", long_symbol=self.config.paxg_symbol,
                    long_qty=paxg_size,
                    long_entry_price=float(paxg_pos.get("entry_price") or 0),
                )
            else:
                # One-sided exposure — log a warning, do not auto-adopt.
                self._log_activity(
                    "position_sync_warn",
                    f"Variational shows non-hedged exposure (PAXG side={paxg_side} "
                    f"size={paxg_size}, XAUT side={xaut_side} size={xaut_size}). "
                    "Manual intervention required.",
                )

    def _adopt_position(
        self, direction: str,
        short_token: str, short_symbol: str, short_qty: float, short_entry_price: float,
        long_token: str, long_symbol: str, long_qty: float, long_entry_price: float,
    ) -> None:
        self._position = GoldSpreadPosition(
            opened_at=time.time(),
            direction=direction,
            short_token=short_token, short_symbol=short_symbol,
            short_qty=short_qty, short_entry_price=short_entry_price,
            long_token=long_token, long_symbol=long_symbol,
            long_qty=long_qty, long_entry_price=long_entry_price,
            entry_spread=short_entry_price - long_entry_price,
            simulation=False,
        )
        # Adopted positions: we don't know the real entry timestamp.
        # Conservative fallback — use "now" so fee/funding queries scope
        # forward only. This understates fees/funding but never inflates.
        self._entry_started_ms = int(time.time() * 1000)
        self._state = State.HOLDING
        self._log_activity(
            "position_adopted",
            f"Adopted Variational hedge: SHORT {short_qty:.4f} {short_token} "
            f"@ ${short_entry_price:.2f} | LONG {long_qty:.4f} {long_token} "
            f"@ ${long_entry_price:.2f}",
        )
        self._save_state()

    def _position_aware_exit_spread(self, snap: GoldSpreadSnapshot) -> float:
        """Fix 3: compute the EXIT exec spread bound to the persisted
        position's direction, not the current live direction.

        ``snap.exit_exec_spread`` is computed for the snapshot's current
        direction, which may have flipped during the hold period. To
        decide when to close *our* position we always need the spread
        for the direction we entered in:

            entered xaut_premium → exit cost = paxg_bid − xaut_ask
            entered paxg_premium → exit cost = xaut_bid − paxg_ask

        Note the asymmetry in convention: ``exit_exec_spread`` in the
        snapshot is the *reverse* leg's exec spread, which converges to
        zero (and goes negative) as the pair re-converges. So for an
        xaut_premium position we want ``paxg_bid − xaut_ask``.
        """
        if self._position is None:
            return snap.exit_exec_spread
        if self._position.direction == "xaut_premium":
            return snap.paxg_bid - snap.xaut_ask
        return snap.xaut_bid - snap.paxg_ask

    def _spread_volatility_ratio(self) -> float | None:
        """Fix 7: rolling spread stability metric.

        Returns std(exec_spread) / mean(exec_spread) over the last
        ``signal_confirmations`` snapshots, or None if we don't have
        enough samples yet. Smaller is more stable. Used to abort
        entries triggered by single-tick spikes.
        """
        n = max(2, self.config.signal_confirmations)
        history = self._live_history[-n:]
        if len(history) < n:
            return None
        values = [s.exec_spread for s in history]
        mean_v = sum(values) / len(values)
        if abs(mean_v) < 1e-9:
            return None
        var = sum((v - mean_v) ** 2 for v in values) / len(values)
        std_v = var ** 0.5
        return std_v / abs(mean_v)

    # ── Execution (Phase 1 simulation / Phase 2 live) ──────────────

    async def _handle_entry_signal(self, snap: GoldSpreadSnapshot) -> None:
        """Open position: short the premium token, long the discount token.

        Direction is driven by ``snap.direction``: when paxg_premium we
        short PAXG and long XAUT; when xaut_premium we short XAUT and long
        PAXG. The leg layout is symmetric so the same code handles both.

        Phase-2 safety layers, in order:
          1. Fix 7 — spread must be stable across the confirmation window
          2. Fix 5 — refresh the snapshot immediately before submitting
             orders so we trade against current prices, not an older
             5-second-stale tick
          3. Pre-flight notional cap
          4. Parallel IOC submit with timeout + asymmetric-fill unwind
          5. Fix 1 — read actual fill prices from Variational's position
             API (not the order-history-with-fallback path)
          6. Fix 6 — post-fill validation: if the realised entry spread
             came out far worse than expected (price drifted between
             snapshot and RFQ), unwind both legs immediately
        """
        self._state = State.ENTERING

        # Fix 7: stability gate. If the spread has been jittery over the
        # confirmation window, the OMS snapshot we triggered on is more
        # likely a single-tick spike than a real opportunity.
        if self.config.max_spread_volatility_ratio > 0:
            vol = self._spread_volatility_ratio()
            if vol is not None and vol > self.config.max_spread_volatility_ratio:
                self._log_activity(
                    "entry_aborted_volatile",
                    f"Spread too volatile: std/mean={vol:.3f} > "
                    f"max {self.config.max_spread_volatility_ratio} — skipping entry",
                )
                self._signal_count = 0
                self._last_signal = "NONE"
                self._state = State.MONITORING
                return

        # Resolve which token plays which role based on the live direction.
        if snap.direction == "paxg_premium":
            short_token, short_symbol = "PAXG", self.config.paxg_symbol
            short_entry_price = snap.paxg_bid
            long_token, long_symbol = "XAUT", self.config.xaut_symbol
            long_entry_price = snap.xaut_ask
        else:
            short_token, short_symbol = "XAUT", self.config.xaut_symbol
            short_entry_price = snap.xaut_bid
            long_token, long_symbol = "PAXG", self.config.paxg_symbol
            long_entry_price = snap.paxg_ask

        self._log_activity(
            "entry_signal",
            f"ENTRY ({snap.direction}): exec_spread=${snap.exec_spread:.4f} "
            f">= ${self.config.entry_spread} | spread=${snap.spread:.4f} "
            f"| short {short_token} @ ${short_entry_price:.2f} | "
            f"long {long_token} @ ${long_entry_price:.2f}",
        )
        self._signal_count = 0
        self._last_signal = "NONE"

        if self.config.simulation:
            # Phase 1 default — record a virtual position.
            self._position = GoldSpreadPosition(
                opened_at=time.time(),
                direction=snap.direction,
                short_token=short_token,
                short_symbol=short_symbol,
                short_qty=self.config.quantity,
                short_entry_price=short_entry_price,
                long_token=long_token,
                long_symbol=long_symbol,
                long_qty=self.config.quantity,
                long_entry_price=long_entry_price,
                entry_spread=snap.exec_spread,
                simulation=True,
            )
            self._state = State.HOLDING
            self._log_activity(
                "position_opened",
                f"[SIM] SHORT {self.config.quantity} {short_token} "
                f"@ ${short_entry_price:.2f} | LONG {self.config.quantity} "
                f"{long_token} @ ${long_entry_price:.2f}",
            )
            self._save_state()
            return

        # ── Phase 2: real execution ────────────────────────────────
        client = self._get_client()
        if client is None:
            self._log_activity(
                "error",
                f"Cannot execute: no '{self.config.exchange}' client registered",
            )
            self._state = State.ERROR
            return

        # Fix 5: refresh the snapshot right before placing orders. The
        # OMS poll only runs every config.tick_interval_s seconds (5 s
        # default) on top of the OMS's own 1.2 s upstream poll, so the
        # `snap` argument can be ~6 s old. A fresh GET /gold-spread/latest
        # right here is essentially a free correctness check.
        fresh = await self._fetch_spread_snapshot()
        if fresh is None:
            self._log_activity(
                "entry_aborted_no_snap",
                "Could not refresh OMS snapshot before entry — aborting",
            )
            self._state = State.MONITORING
            return
        if fresh.exec_spread < self.config.entry_spread:
            self._log_activity(
                "entry_aborted_drifted",
                f"Spread drifted: signal exec_spread=${snap.exec_spread:.4f} → "
                f"fresh exec_spread=${fresh.exec_spread:.4f} "
                f"< entry threshold ${self.config.entry_spread:.4f}",
            )
            self._state = State.MONITORING
            return
        # Direction may have flipped between snap and fresh. Re-resolve.
        if fresh.direction == "paxg_premium":
            short_token, short_symbol = "PAXG", self.config.paxg_symbol
            short_entry_price = fresh.paxg_bid
            long_token, long_symbol = "XAUT", self.config.xaut_symbol
            long_entry_price = fresh.xaut_ask
        else:
            short_token, short_symbol = "XAUT", self.config.xaut_symbol
            short_entry_price = fresh.xaut_bid
            long_token, long_symbol = "PAXG", self.config.paxg_symbol
            long_entry_price = fresh.paxg_ask

        # Pre-flight notional guard. Use the *higher* of the two entry
        # prices so the cap is conservative regardless of which token is
        # premium.
        notional = self.config.quantity * max(short_entry_price, long_entry_price)
        if notional > self.config.max_position_value_usd:
            self._log_activity(
                "guard_max_notional",
                f"Refusing entry: notional ${notional:.2f} exceeds "
                f"max_position_value_usd ${self.config.max_position_value_usd:.2f}",
            )
            self._state = State.MONITORING
            return

        qty = Decimal(str(self.config.quantity))
        slippage = self.config.max_slippage_pct / 100.0  # 0.7 → 0.007
        entry_started_ms = int(time.time() * 1000)
        self._log_activity(
            "entry_executing",
            f"Placing IOCs: SHORT {qty} {short_symbol} @ ~${short_entry_price:.2f} "
            f"& LONG {qty} {long_symbol} @ ~${long_entry_price:.2f} "
            f"(slippage={slippage:.4f}, timeout={self.config.execution_timeout_s}s, "
            f"fresh_spread=${fresh.exec_spread:.4f})",
        )

        try:
            short_result, long_result = await asyncio.wait_for(
                asyncio.gather(
                    client.async_create_ioc_order(
                        short_symbol, "sell", qty,
                        Decimal(str(short_entry_price)),
                        reduce_only=False,
                        max_slippage=slippage,
                    ),
                    client.async_create_ioc_order(
                        long_symbol, "buy", qty,
                        Decimal(str(long_entry_price)),
                        reduce_only=False,
                        max_slippage=slippage,
                    ),
                    return_exceptions=True,
                ),
                timeout=self.config.execution_timeout_s,
            )
        except asyncio.TimeoutError:
            self._log_activity(
                "entry_timeout",
                f"Entry IOC pair exceeded {self.config.execution_timeout_s}s — "
                "starting defensive position sweep",
            )
            await self._sweep_after_timeout(
                short_symbol, short_entry_price,
                long_symbol, long_entry_price,
                reason="entry_timeout",
            )
            self._state = State.MONITORING
            return
        except Exception as exc:
            logger.exception("Gold-Spread entry gather failed: %s", exc)
            self._log_activity("entry_error", f"Entry gather raised: {exc}")
            await self._sweep_after_timeout(
                short_symbol, short_entry_price,
                long_symbol, long_entry_price,
                reason="entry_error",
            )
            self._state = State.MONITORING
            return

        short_ok = self._is_filled(short_result)
        long_ok = self._is_filled(long_result)
        short_status = self._result_status(short_result)
        long_status = self._result_status(long_result)
        self._log_activity(
            "entry_results",
            f"short={short_status} (filled={short_ok}) | "
            f"long={long_status} (filled={long_ok})",
        )

        if short_ok and long_ok:
            short_dict = short_result if isinstance(short_result, dict) else {}
            long_dict = long_result if isinstance(long_result, dict) else {}
            short_traded = float(short_dict.get("traded_qty") or self.config.quantity)
            long_traded = float(long_dict.get("traded_qty") or self.config.quantity)
            short_oid = str(short_dict.get("id") or "")
            long_oid = str(long_dict.get("id") or "")
            # Allow Variational a moment to publish the position update.
            await asyncio.sleep(self.config.fill_verify_delay_s)
            # Fix 1: read fill prices directly from the position API.
            # Variational's position.entry_price is the VWAP of all fills
            # making up the current position — same number it uses for
            # its own PnL display. Much more reliable than scanning the
            # order/trade history.
            short_fill = await self._fetch_fill_price_from_position(
                client, short_symbol, short_entry_price,
            )
            long_fill = await self._fetch_fill_price_from_position(
                client, long_symbol, long_entry_price,
            )

            # Fix 6: post-fill spread validation. If the realised entry
            # spread is much smaller than the threshold (because the
            # market moved unfavourably between snapshot and RFQ fill,
            # or the RFQ requoted aggressively against us), unwind both
            # legs immediately — better to eat one round-trip slippage
            # than carry a position that started out underwater.
            if fresh.direction == "paxg_premium":
                # Short PAXG (sell @ bid), Long XAUT (buy @ ask)
                # Captured spread = paxg_fill - xaut_fill
                actual_spread = short_fill - long_fill
            else:
                # Short XAUT (sell @ bid), Long PAXG (buy @ ask)
                actual_spread = short_fill - long_fill
            min_acceptable = self.config.entry_spread * self.config.min_actual_spread_ratio
            if actual_spread < min_acceptable:
                self._log_activity(
                    "entry_bad_fill",
                    f"Captured spread ${actual_spread:.4f} < "
                    f"min acceptable ${min_acceptable:.4f} "
                    f"({self.config.min_actual_spread_ratio:.2f} × entry threshold "
                    f"${self.config.entry_spread:.4f}). Unwinding both legs.",
                )
                # Unwind: buy back short, sell out long.
                await asyncio.gather(
                    self._unwind_leg(
                        short_symbol, "buy", short_traded, short_fill,
                        reason="entry_bad_fill_short",
                    ),
                    self._unwind_leg(
                        long_symbol, "sell", long_traded, long_fill,
                        reason="entry_bad_fill_long",
                    ),
                    return_exceptions=True,
                )
                self._state = State.MONITORING
                # Stay flat — no position recorded.
                return

            self._position = GoldSpreadPosition(
                opened_at=time.time(),
                direction=fresh.direction,
                short_token=short_token, short_symbol=short_symbol,
                short_qty=short_traded, short_entry_price=short_fill,
                long_token=long_token, long_symbol=long_symbol,
                long_qty=long_traded, long_entry_price=long_fill,
                entry_spread=actual_spread,
                simulation=False,
                short_order_id=short_oid or None,
                long_order_id=long_oid or None,
            )
            # Stash the entry timestamp on the position so exit-time
            # trade-history and funding queries can scope themselves.
            self._entry_started_ms = entry_started_ms
            self._state = State.HOLDING
            self._log_activity(
                "position_opened",
                f"LIVE SHORT {short_traded:.4f} {short_token} @ ${short_fill:.2f} | "
                f"LONG {long_traded:.4f} {long_token} @ ${long_fill:.2f} "
                f"(entry_spread=${actual_spread:.4f})",
            )
            self._save_state()
            return

        # One-sided fill → unwind whatever filled, return to MONITORING.
        if short_ok and not long_ok:
            short_dict = short_result if isinstance(short_result, dict) else {}
            short_traded = float(short_dict.get("traded_qty") or self.config.quantity)
            await self._unwind_leg(
                short_symbol, "buy", short_traded, short_entry_price,
                reason="entry_one_sided_short_filled",
            )
            self._state = State.MONITORING
            return
        if long_ok and not short_ok:
            long_dict = long_result if isinstance(long_result, dict) else {}
            long_traded = float(long_dict.get("traded_qty") or self.config.quantity)
            await self._unwind_leg(
                long_symbol, "sell", long_traded, long_entry_price,
                reason="entry_one_sided_long_filled",
            )
            self._state = State.MONITORING
            return

        # Both legs failed → no exposure to unwind.
        self._log_activity(
            "entry_failed",
            f"Both legs failed (short={short_status} long={long_status}) — "
            "no exposure created",
        )
        self._state = State.MONITORING

    async def _handle_exit_signal(self, snap: GoldSpreadSnapshot) -> None:
        """Close position: reverse both legs.

        We close the short leg by buying it back at the ask, and the long
        leg by selling it at the bid. Which token is which side comes from
        the persisted position (set at entry time), not from the current
        live direction — the convergence may have flipped the premium side
        but our position layout is fixed.

        PnL accounting (live mode) deducts:
          * Trading fees from Variational's trade-history `fee` field
            (Fix 2)
          * Net funding paid/received during the hold period (Fix 4)
        Both come from API queries scoped to the entry timestamp so
        unrelated history doesn't pollute the result.
        """
        if not self._position:
            self._state = State.MONITORING
            return

        self._state = State.EXITING
        pos = self._position

        # Resolve current bid/ask for each persisted leg using the live
        # snapshot. Note: this is for the log message only; the real
        # close prices come from the position API after the fills.
        if pos.short_token == "PAXG":
            short_close_price = snap.paxg_ask  # buy back PAXG short @ ask
            long_close_price = snap.xaut_bid   # sell XAUT long @ bid
        else:
            short_close_price = snap.xaut_ask  # buy back XAUT short @ ask
            long_close_price = snap.paxg_bid   # sell PAXG long @ bid

        # Fix 3: position-aware exit spread for log clarity.
        position_exit_spread = self._position_aware_exit_spread(snap)
        est_pnl_per_unit = (
            (pos.short_entry_price - short_close_price)   # short profits when close < entry
            + (long_close_price - pos.long_entry_price)   # long profits when close > entry
        )
        est_pnl = est_pnl_per_unit * self.config.quantity

        self._log_activity(
            "exit_signal",
            f"EXIT: position_exit_spread=${position_exit_spread:.4f} "
            f"<= ${self.config.exit_spread} | est_pnl={est_pnl:+.4f} USD "
            f"(pos_dir={pos.direction}, current dir={snap.direction})",
        )
        self._signal_count = 0
        self._last_signal = "NONE"

        if pos.simulation or self.config.simulation:
            self._log_activity(
                "position_closed",
                f"[SIM] Closed {pos.short_token} short @ ${short_close_price:.2f} "
                f"+ {pos.long_token} long @ ${long_close_price:.2f} | "
                f"realized_pnl={est_pnl:+.4f} USD "
                f"(entry_spread=${pos.entry_spread:.4f}, "
                f"exit_spread=${position_exit_spread:.4f})",
            )
            self._position = None
            self._entry_started_ms = None
            self._state = State.MONITORING
            self._save_state()
            return

        # ── Phase 2: real exit ─────────────────────────────────────
        client = self._get_client()
        if client is None:
            self._log_activity(
                "error",
                f"Cannot exit: no '{self.config.exchange}' client registered",
            )
            self._state = State.HOLDING  # keep trying next tick
            return

        # Fix 5: refresh prices right before the close orders. The exit
        # signal may be a few seconds stale and gold can move several
        # dollars in that window — using a fresh snapshot keeps the IOC
        # reference price as close to the real market as possible.
        fresh = await self._fetch_spread_snapshot()
        if fresh is not None:
            if pos.short_token == "PAXG":
                short_close_price = fresh.paxg_ask
                long_close_price = fresh.xaut_bid
            else:
                short_close_price = fresh.xaut_ask
                long_close_price = fresh.paxg_bid

        slippage = self.config.max_slippage_pct / 100.0
        self._log_activity(
            "exit_executing",
            f"Placing close IOCs: BUY {pos.short_qty} {pos.short_symbol} "
            f"@ ~${short_close_price:.2f} & SELL {pos.long_qty} {pos.long_symbol} "
            f"@ ~${long_close_price:.2f} (slippage={slippage:.4f})",
        )

        try:
            short_close, long_close = await asyncio.wait_for(
                asyncio.gather(
                    client.async_create_ioc_order(
                        pos.short_symbol, "buy",
                        Decimal(str(pos.short_qty)),
                        Decimal(str(short_close_price)),
                        reduce_only=True,
                        max_slippage=slippage,
                    ),
                    client.async_create_ioc_order(
                        pos.long_symbol, "sell",
                        Decimal(str(pos.long_qty)),
                        Decimal(str(long_close_price)),
                        reduce_only=True,
                        max_slippage=slippage,
                    ),
                    return_exceptions=True,
                ),
                timeout=self.config.execution_timeout_s,
            )
        except asyncio.TimeoutError:
            self._log_activity(
                "exit_timeout",
                f"Exit IOC pair exceeded {self.config.execution_timeout_s}s — "
                "starting defensive position sweep",
            )
            await self._sweep_after_timeout(
                pos.short_symbol, short_close_price,
                pos.long_symbol, long_close_price,
                reason="exit_timeout",
            )
            # After sweep, assume position is flat.
            self._position = None
            self._entry_started_ms = None
            self._state = State.MONITORING
            self._save_state()
            return
        except Exception as exc:
            logger.exception("Gold-Spread exit gather failed: %s", exc)
            self._log_activity("exit_error", f"Exit gather raised: {exc}")
            await self._sweep_after_timeout(
                pos.short_symbol, short_close_price,
                pos.long_symbol, long_close_price,
                reason="exit_error",
            )
            self._position = None
            self._entry_started_ms = None
            self._state = State.MONITORING
            self._save_state()
            return

        short_ok = self._is_filled(short_close)
        long_ok = self._is_filled(long_close)

        if short_ok and long_ok:
            short_dict = short_close if isinstance(short_close, dict) else {}
            long_dict = long_close if isinstance(long_close, dict) else {}
            short_oid = str(short_dict.get("id") or "")
            long_oid = str(long_dict.get("id") or "")
            # Allow Variational a moment to publish the trade history rows.
            await asyncio.sleep(self.config.fill_verify_delay_s)

            # Fix 1 + Fix 2: pull both fill price AND fee from the trade
            # history (the position has been zeroed at this point so the
            # position-API trick we use at entry doesn't help here). Scope
            # to the entry timestamp minus a small buffer so we capture
            # both entry and exit fills for the realised-PnL math.
            scope_since_ms = (self._entry_started_ms or
                              int(time.time() * 1000) - 3_600_000) - 5000

            (
                short_fill, short_close_fee
            ), (
                long_fill, long_close_fee
            ) = await asyncio.gather(
                self._fetch_fill_price_and_fee_from_trades(
                    client, short_oid, pos.short_qty,
                    short_close_price, scope_since_ms,
                ),
                self._fetch_fill_price_and_fee_from_trades(
                    client, long_oid, pos.long_qty,
                    long_close_price, scope_since_ms,
                ),
            )

            # Also pull entry fees so the round-trip cost is accurate.
            short_entry_fee = 0.0
            long_entry_fee = 0.0
            if pos.short_order_id:
                _, short_entry_fee = await self._fetch_fill_price_and_fee_from_trades(
                    client, pos.short_order_id, pos.short_qty,
                    pos.short_entry_price, scope_since_ms,
                )
            if pos.long_order_id:
                _, long_entry_fee = await self._fetch_fill_price_and_fee_from_trades(
                    client, pos.long_order_id, pos.long_qty,
                    pos.long_entry_price, scope_since_ms,
                )
            total_fees = (
                short_entry_fee + long_entry_fee
                + short_close_fee + long_close_fee
            )

            # Fix 4: net funding paid over the hold period.
            funding_short = 0.0
            funding_long = 0.0
            if self._entry_started_ms is not None:
                funding_short, funding_long = await asyncio.gather(
                    self._fetch_funding_paid(
                        client, pos.short_symbol, self._entry_started_ms,
                    ),
                    self._fetch_funding_paid(
                        client, pos.long_symbol, self._entry_started_ms,
                    ),
                )
            net_funding = funding_short + funding_long

            gross_pnl = (
                (pos.short_entry_price - short_fill) * pos.short_qty
                + (long_fill - pos.long_entry_price) * pos.long_qty
            )
            realized_pnl = gross_pnl - total_fees + net_funding

            self._log_activity(
                "position_closed",
                f"LIVE Closed {pos.short_token} short @ ${short_fill:.2f} + "
                f"{pos.long_token} long @ ${long_fill:.2f} | "
                f"gross_pnl={gross_pnl:+.4f} − fees=${total_fees:.4f} "
                f"+ funding={net_funding:+.4f} = realized_pnl={realized_pnl:+.4f} USD "
                f"(entry_spread=${pos.entry_spread:.4f}, "
                f"exit_spread=${position_exit_spread:.4f})",
            )
            self._position = None
            self._entry_started_ms = None
            self._state = State.MONITORING
            self._save_state()
            return

        # One-sided close failure: re-fire the failed leg with widened
        # slippage. This is essentially the unwind path — we already hold
        # the residual exposure that the failed close left behind.
        if short_ok and not long_ok:
            await self._unwind_leg(
                pos.long_symbol, "sell", pos.long_qty, long_close_price,
                reason="exit_one_sided_short_closed",
            )
        elif long_ok and not short_ok:
            await self._unwind_leg(
                pos.short_symbol, "buy", pos.short_qty, short_close_price,
                reason="exit_one_sided_long_closed",
            )
        else:
            # Both close orders failed — try a wider-slippage retry on
            # both legs; if they still fail we'll be back next tick.
            await self._unwind_leg(
                pos.short_symbol, "buy", pos.short_qty, short_close_price,
                reason="exit_both_failed_short",
            )
            await self._unwind_leg(
                pos.long_symbol, "sell", pos.long_qty, long_close_price,
                reason="exit_both_failed_long",
            )

        # After any of the above we trust the exchange more than our
        # state — confirm via positions whether we're flat.
        await self._sweep_after_timeout(
            pos.short_symbol, short_close_price,
            pos.long_symbol, long_close_price,
            reason="exit_partial",
        )
        # Optimistically clear local state; the next tick's sync (or
        # explicit reset) will repair if the exchange disagrees.
        self._position = None
        self._entry_started_ms = None
        self._state = State.MONITORING
        self._save_state()

    # ── Persistence ────────────────────────────────────────────────

    def _state_path(self) -> Path:
        return _STATE_DIR / "state.json"

    def _save_state(self) -> None:
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "config": asdict(self.config),
                "state": self._state.value,
                "position": asdict(self._position) if self._position else None,
                # Persist the entry timestamp so realised-PnL fee/funding
                # queries still scope correctly after a container restart.
                "entry_started_ms": self._entry_started_ms,
                # Don't persist live_history (rebuilds on next tick) or
                # activity_log (kept ephemeral; the canonical log is in
                # Cloudflare Analytics Engine via the activity forwarder).
            }
            with open(self._state_path(), "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as exc:
            logger.warning("Could not save Gold-Spread state: %s", exc)

    def _load_state(self) -> None:
        path = self._state_path()
        if not path.exists():
            return
        try:
            with open(path) as f:
                data = json.load(f)
            cfg = data.get("config") or {}
            for key, val in cfg.items():
                if hasattr(self.config, key):
                    try:
                        setattr(self.config, key, val)
                    except Exception:
                        pass
            try:
                self._state = State(data.get("state", "IDLE"))
            except ValueError:
                self._state = State.IDLE
            pos = data.get("position")
            if pos:
                try:
                    self._position = GoldSpreadPosition(**pos)
                except TypeError as exc:
                    # Schema mismatch (e.g. upgraded from the legacy
                    # paxg_short_qty/xaut_long_qty layout). Drop the stale
                    # position and reset the state so the bot starts clean
                    # rather than getting stuck in a phantom HOLDING state.
                    logger.warning(
                        "Gold-Spread legacy position schema dropped: %s", exc,
                    )
                    self._position = None
                    if self._state == State.HOLDING:
                        self._state = State.IDLE
            entry_ms = data.get("entry_started_ms")
            if entry_ms is not None:
                try:
                    self._entry_started_ms = int(entry_ms)
                except (TypeError, ValueError):
                    self._entry_started_ms = None
            logger.info(
                "Gold-Spread state restored: state=%s position=%s",
                self._state.value,
                "yes" if self._position else "no",
            )
        except Exception as exc:
            logger.warning("Could not load Gold-Spread state: %s", exc)

    # ── Misc helpers ───────────────────────────────────────────────

    def _log_activity(self, event: str, message: str) -> None:
        entry = {"timestamp": time.time(), "event": event, "message": message}
        self._activity_log.append(entry)
        if len(self._activity_log) > 500:
            self._activity_log = self._activity_log[-500:]
        logger.info("[gold-spread] %s: %s", event, message)
        if self._activity_forwarder:
            try:
                self._activity_forwarder.forward(
                    event, message, "gold_spread", self.config.bot_id,
                )
            except Exception:
                pass

    @staticmethod
    def _snapshot_to_dict(snap: GoldSpreadSnapshot | None) -> dict | None:
        if snap is None:
            return None
        return {
            "ts": snap.ts,
            "paxg_mid": snap.paxg_mid,
            "paxg_bid": snap.paxg_bid,
            "paxg_ask": snap.paxg_ask,
            "xaut_mid": snap.xaut_mid,
            "xaut_bid": snap.xaut_bid,
            "xaut_ask": snap.xaut_ask,
            "spread": snap.spread,
            "spread_pct": snap.spread_pct,
            "exec_spread": snap.exec_spread,
            "exit_exec_spread": snap.exit_exec_spread,
            "direction": snap.direction,
            "signal": snap.signal,
        }
