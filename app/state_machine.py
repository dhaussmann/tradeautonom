"""Maker-Taker TWAP execution state machine for the Funding-Arb engine.

States:
  IDLE → ENTERING → HOLDING → EXITING → IDLE

ENTERING / EXITING substates (per chunk):
  CHUNK_MAKER_PLACE → CHUNK_MAKER_WAIT → CHUNK_TAKER_HEDGE → CHUNK_DONE

Entry/Exit is triggered manually by the user via the engine API.
"""

from __future__ import annotations

import asyncio
import enum
import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.data_layer import DataLayer

from app.spread_analyzer import analyze_cross_venue_spread, SpreadAnalysis
from app.execution_logger import ExecutionLogger, new_execution_id
from app.safety import walk_book

logger = logging.getLogger("tradeautonom.state_machine")

# Transient network errors worth retrying (DNS failures, connection resets, etc.)
_TRANSIENT_ERRORS = (ConnectionError, OSError, TimeoutError)
_MAX_CONN_RETRIES = 5
_CONN_RETRY_DELAY = 3.0  # seconds


def _analyze_depth_spread(long_book: dict, short_book: dict, qty: Decimal, max_slippage_bps: float) -> SpreadAnalysis | None:
    """Thin wrapper to call spread_analyzer without propagating import exceptions."""
    try:
        return analyze_cross_venue_spread(long_book, short_book, qty, max_slippage_bps)
    except Exception:
        return None


# ── State enums ───────────────────────────────────────────────────────

class JobState(str, enum.Enum):
    IDLE = "IDLE"
    ENTERING = "ENTERING"
    HOLDING = "HOLDING"
    EXITING = "EXITING"
    PAUSED_ENTERING = "PAUSED_ENTERING"
    PAUSED_EXITING = "PAUSED_EXITING"
    ERROR = "ERROR"


class ChunkState(str, enum.Enum):
    MAKER_PLACE = "MAKER_PLACE"
    MAKER_WAIT = "MAKER_WAIT"
    TAKER_HEDGE = "TAKER_HEDGE"
    CHUNK_DONE = "CHUNK_DONE"


# ── Data classes ──────────────────────────────────────────────────────

@dataclass
class ChunkResult:
    """Result of a single TWAP chunk execution."""
    chunk_index: int = 0
    maker_order_id: str | None = None
    taker_order_id: str | None = None
    maker_filled_qty: float = 0.0
    taker_filled_qty: float = 0.0
    maker_price: float = 0.0
    taker_price: float = 0.0
    maker_exchange: str = ""
    taker_exchange: str = ""
    state: ChunkState = ChunkState.CHUNK_DONE
    error: str | None = None
    start_ts: float = 0.0
    end_ts: float = 0.0


@dataclass
class ExecutionResult:
    """Result of a full entry or exit execution (all chunks)."""
    action: str = ""  # "ENTER" or "EXIT"
    chunks: list[ChunkResult] = field(default_factory=list)
    total_maker_qty: float = 0.0
    total_taker_qty: float = 0.0
    success: bool = False
    error: str | None = None
    start_ts: float = 0.0
    end_ts: float = 0.0


@dataclass
class MakerTakerConfig:
    """Configuration for a single entry/exit execution."""
    maker_exchange: str = ""
    taker_exchange: str = ""
    maker_symbol: str = ""
    taker_symbol: str = ""
    maker_side: str = ""       # "buy" or "sell"
    taker_side: str = ""       # opposite of maker_side
    total_qty: Decimal = Decimal("0")
    num_chunks: int = 1
    chunk_interval_s: float = 2.0
    maker_timeout_ms: int = 5000
    maker_reprice_ticks: int = 3
    maker_max_chase_rounds: int = 5
    maker_offset_ticks: int = 0
    simulation: bool = False
    reduce_only: bool = False
    max_chunk_spread_usd: float = 1.0
    min_spread_pct: float = -0.5
    max_spread_pct: float = 0.05
    use_depth_spread: bool = False
    max_slippage_bps: float = 10.0
    taker_drift_guard: bool = False
    max_taker_drift_bps: float = 3.0
    ohi_monitoring: bool = False
    min_ohi: float = 0.4
    long_exchange: str = ""
    short_exchange: str = ""


class StateMachine:
    """Maker-Taker TWAP execution state machine.

    Manages the lifecycle of a funding-arb position:
    IDLE → ENTERING → HOLDING → EXITING → IDLE

    All transitions are logged with ms precision.
    """

    _DEFAULT_STATE_FILE = Path("data/fn_position.json")

    def __init__(self, clients: dict[str, Any], data_layer: "DataLayer | None" = None, activity_log_fn=None, bot_id: str = "", execution_logger: "ExecutionLogger | None" = None, funding_monitor: Any = None) -> None:
        self._bot_id = bot_id
        if bot_id:
            self._STATE_FILE = Path(f"data/bots/{bot_id}/position.json")
        else:
            self._STATE_FILE = self._DEFAULT_STATE_FILE
        self._clients = clients  # {exchange_name: client}
        self._data_layer = data_layer
        self._log = activity_log_fn or (lambda cat, msg, **kw: None)
        self._state = JobState.IDLE
        self._current_config: MakerTakerConfig | None = None
        self._execution_result: ExecutionResult | None = None
        self._chunk_results: list[ChunkResult] = []
        self._current_chunk_index: int = 0
        self._current_chunk_state: ChunkState | None = None

        # Pause/resume support
        self._paused = asyncio.Event()
        self._paused.set()  # starts unpaused (set = running)
        self._pre_pause_state: JobState | None = None

        # Fill event tracking (WS-based)
        self._fill_events: dict[str, list[dict]] = {}  # order_id -> [fill_events]
        self._fill_event = asyncio.Event()  # signalled on any fill arrival
        self._fill_sub_tasks: list[asyncio.Task] = []
        self._fill_subs_running = False

        # Position tracking
        self._long_qty: float = 0.0
        self._short_qty: float = 0.0
        self._baseline_maker_size: Decimal = Decimal("0")
        self._baseline_taker_size: Decimal = Decimal("0")
        self._long_exchange: str = ""
        self._short_exchange: str = ""
        self._long_symbol: str = ""
        self._short_symbol: str = ""
        self._long_entry_price: float = 0.0
        self._short_entry_price: float = 0.0
        self._carry_over_gap: Decimal = Decimal("0")

        # Execution logger (AI training data)
        self._execution_logger = execution_logger
        self._funding_monitor = funding_monitor

        # Last depth spread analysis (cached for status reporting)
        self.last_depth_analysis: SpreadAnalysis | None = None

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def state(self) -> JobState:
        return self._state

    @property
    def position_info(self) -> dict:
        return {
            "long_exchange": self._long_exchange,
            "short_exchange": self._short_exchange,
            "long_symbol": self._long_symbol,
            "short_symbol": self._short_symbol,
            "long_qty": self._long_qty,
            "short_qty": self._short_qty,
            "net_delta": self._long_qty + self._short_qty,  # should be ~0
            "long_entry_price": self._long_entry_price,
            "short_entry_price": self._short_entry_price,
        }

    @property
    def execution_status(self) -> dict:
        return {
            "state": self._state.value,
            "chunk_index": self._current_chunk_index,
            "chunk_state": self._current_chunk_state.value if self._current_chunk_state else None,
            "chunks_completed": len(self._chunk_results),
            "total_chunks": self._current_config.num_chunks if self._current_config else 0,
            "last_result": {
                "success": self._execution_result.success if self._execution_result else None,
                "error": self._execution_result.error if self._execution_result else None,
                "total_maker_qty": self._execution_result.total_maker_qty if self._execution_result else 0,
                "total_taker_qty": self._execution_result.total_taker_qty if self._execution_result else 0,
            },
        }

    async def execute_entry(self, config: MakerTakerConfig) -> ExecutionResult:
        """Execute a full entry (IDLE → ENTERING → HOLDING).

        Triggered manually by the user.
        """
        self._log("ENGINE", f"=== ENTRY STARTED ===")
        self._log("ENGINE", f"Config: maker={config.maker_exchange}:{config.maker_symbol} side={config.maker_side}")
        self._log("ENGINE", f"Config: taker={config.taker_exchange}:{config.taker_symbol} side={config.taker_side}")
        self._log("ENGINE", f"Config: qty={config.total_qty}, chunks={config.num_chunks}, interval={config.chunk_interval_s}s")

        if self._state != JobState.IDLE:
            err_msg = f"Cannot enter: state is {self._state.value}, expected IDLE"
            self._log("ENGINE", err_msg, level="error")
            raise RuntimeError(err_msg)

        self._transition(JobState.ENTERING)
        self._long_exchange = config.taker_exchange if config.maker_side == "sell" else config.maker_exchange
        self._short_exchange = config.taker_exchange if config.maker_side == "buy" else config.maker_exchange
        self._long_symbol = config.taker_symbol if config.maker_side == "sell" else config.maker_symbol
        self._short_symbol = config.taker_symbol if config.maker_side == "buy" else config.maker_symbol

        self._log("ENGINE", f"Long position: {self._long_exchange}:{self._long_symbol}")
        self._log("ENGINE", f"Short position: {self._short_exchange}:{self._short_symbol}")

        result = await self._execute_maker_taker(config, action="ENTER")

        # Compute VWAP entry prices from chunk results
        self._compute_entry_prices(result, config)

        # Position qty already updated incrementally per chunk in _execute_maker_taker
        entry_duration_sec = result.end_ts - result.start_ts if result.end_ts else 0
        if result.success:
            self._log("ENGINE", f"=== ENTRY SUCCESS === Duration: {entry_duration_sec:.1f}s, Final state: HOLDING")
            self._transition(JobState.HOLDING)
        else:
            self._log("ENGINE", f"=== ENTRY FAILED === Duration: {entry_duration_sec:.1f}s, Error: {result.error}")
            self._log("ENGINE", f"Entry result: maker_qty={result.total_maker_qty:.6f}, taker_qty={result.total_taker_qty:.6f}, chunks={len(result.chunks)}")
            if result.total_maker_qty > 0 and result.total_taker_qty == 0:
                self._log("ENGINE", "CRITICAL: Maker filled but taker did not — initiating emergency unwind", level="error")
                logger.error("Entry failed: maker filled but taker did not — emergency unwind")
                await self._emergency_unwind(config, result)
                self._transition(JobState.IDLE)
            elif result.total_maker_qty == 0:
                self._log("ENGINE", "Entry failed: No fills — returning to IDLE", level="warn")
                logger.warning("Entry failed: no fills — returning to IDLE")
                self._transition(JobState.IDLE)
            else:
                self._log("ENGINE", f"Entry partial: maker={result.total_maker_qty:.6f} taker={result.total_taker_qty:.6f} — holding partial position", level="warn")
                logger.warning("Entry partial: maker=%.6f taker=%.6f — holding partial position",
                               result.total_maker_qty, result.total_taker_qty)
                self._transition(JobState.HOLDING)

        self._execution_result = result
        self.save_state()
        return result

    async def execute_exit(self, config: MakerTakerConfig) -> ExecutionResult:
        """Execute a full exit (HOLDING → EXITING → IDLE).

        Triggered manually by the user. Config should have reversed sides.
        """
        if self._state != JobState.HOLDING:
            raise RuntimeError(f"Cannot exit: state is {self._state.value}, expected HOLDING")

        self._transition(JobState.EXITING)
        result = await self._execute_maker_taker(config, action="EXIT")

        # ── Final residual sweep: query actual exchange positions ──
        # Belt-and-suspenders: if any residual remains after TWAP, attempt
        # one last market-close to avoid leaving positions open.
        try:
            maker_client = self._clients.get(config.maker_exchange)
            taker_client = self._clients.get(config.taker_exchange)
            maker_pos = await self._get_position_size(
                config.maker_exchange, config.maker_symbol, maker_client, force_rest=True) if maker_client else Decimal("0")
            taker_pos = await self._get_position_size(
                config.taker_exchange, config.taker_symbol, taker_client, force_rest=True) if taker_client else Decimal("0")
            residual = max(maker_pos, taker_pos)
            if residual > Decimal("0.001"):
                self._log("EXIT", f"Post-TWAP residual detected: maker={maker_pos:.6f} taker={taker_pos:.6f} — attempting final market-close")
                logger.warning("Post-TWAP residual: maker=%.6f taker=%.6f — final market-close", maker_pos, taker_pos)
                mc = await self._market_close_residual(config, residual, chunk_index=-1)
                result.chunks.append(mc)
                result.total_maker_qty += mc.maker_filled_qty
                result.total_taker_qty += mc.taker_filled_qty
                if mc.maker_filled_qty > 0 or mc.taker_filled_qty > 0:
                    self._update_position_incremental(config, "EXIT", mc.maker_filled_qty, mc.taker_filled_qty)
                    self._log("EXIT", f"Final market-close result: maker={mc.maker_filled_qty:.6f} taker={mc.taker_filled_qty:.6f}")
                elif mc.error:
                    self._log("EXIT", f"Final market-close failed: {mc.error}", level="error")
        except Exception as sweep_exc:
            self._log("EXIT", f"Post-TWAP residual sweep failed: {sweep_exc}", level="error")
            logger.error("Post-TWAP residual sweep error: %s", sweep_exc)

        # Position qty already updated incrementally per chunk in _execute_maker_taker
        if result.success or (abs(self._long_qty) < 1e-8 and abs(self._short_qty) < 1e-8):
            self._long_qty = 0.0
            self._short_qty = 0.0
            self._long_entry_price = 0.0
            self._short_entry_price = 0.0
            self._transition(JobState.IDLE)
        else:
            if result.total_maker_qty > 0 and result.total_taker_qty == 0:
                logger.error("Exit failed: maker filled but taker did not — emergency unwind")
                await self._emergency_unwind(config, result)
            if abs(self._long_qty) < 1e-8 and abs(self._short_qty) < 1e-8:
                self._long_entry_price = 0.0
                self._short_entry_price = 0.0
                self._transition(JobState.IDLE)
            else:
                self._transition(JobState.HOLDING)
                logger.warning("Exit partial: remaining long=%.6f short=%.6f", self._long_qty, self._short_qty)

        self._execution_result = result
        self.save_state()
        return result

    def reset(self) -> None:
        """Force reset to IDLE (e.g. after manual position close)."""
        self._transition(JobState.IDLE)
        self._long_qty = 0.0
        self._short_qty = 0.0
        self._long_entry_price = 0.0
        self._short_entry_price = 0.0
        self._chunk_results.clear()
        self._execution_result = None
        self.save_state()

    async def abort_execution(self) -> None:
        """Abort any running TWAP execution by forcing state to IDLE.

        The chunk loop in _execute_single_chunk checks state on every iteration
        and will exit cleanly when it sees state != ENTERING/EXITING.
        """
        if self._state in (JobState.ENTERING, JobState.EXITING, JobState.PAUSED_ENTERING, JobState.PAUSED_EXITING):
            self._log("ENGINE", "Aborting execution — forcing state to IDLE", level="warn")
            self._paused.set()  # unblock any paused waiter
            self._pre_pause_state = None
            self._transition(JobState.IDLE)
            # Give the running chunk loop a chance to detect the state change
            await asyncio.sleep(0.2)
        else:
            logger.info("abort_execution: state is %s, nothing to abort", self._state.value)

    @property
    def is_paused(self) -> bool:
        return self._state in (JobState.PAUSED_ENTERING, JobState.PAUSED_EXITING)

    def pause(self) -> None:
        """Pause the running TWAP execution. The chunk loop will block at the next safe point."""
        if self._state == JobState.ENTERING:
            self._pre_pause_state = JobState.ENTERING
            self._paused.clear()
            self._transition(JobState.PAUSED_ENTERING)
            self._log("ENGINE", "Execution PAUSED (was ENTERING)")
        elif self._state == JobState.EXITING:
            self._pre_pause_state = JobState.EXITING
            self._paused.clear()
            self._transition(JobState.PAUSED_EXITING)
            self._log("ENGINE", "Execution PAUSED (was EXITING)")
        else:
            logger.info("pause: state is %s — nothing to pause", self._state.value)

    def resume(self) -> None:
        """Resume a paused TWAP execution."""
        if self._state == JobState.PAUSED_ENTERING:
            self._transition(JobState.ENTERING)
            self._paused.set()
            self._pre_pause_state = None
            self._log("ENGINE", "Execution RESUMED → ENTERING")
        elif self._state == JobState.PAUSED_EXITING:
            self._transition(JobState.EXITING)
            self._paused.set()
            self._pre_pause_state = None
            self._log("ENGINE", "Execution RESUMED → EXITING")
        else:
            logger.info("resume: state is %s — nothing to resume", self._state.value)

    def _is_executing(self) -> bool:
        """True if the state machine is in an active or paused execution state."""
        return self._state in (
            JobState.ENTERING, JobState.EXITING,
            JobState.PAUSED_ENTERING, JobState.PAUSED_EXITING,
        )

    async def _wait_if_paused(self) -> bool:
        """Block until unpaused. Returns False if execution was aborted while paused."""
        if self._paused.is_set():
            return True
        self._log("ENGINE", "Waiting for resume…")
        while not self._paused.is_set():
            await asyncio.sleep(0.5)
            if not self._is_executing():
                return False
        return True

    # ── State persistence ──────────────────────────────────────────────

    def save_state(self) -> None:
        """Persist position state to disk so it survives container restarts."""
        data = {
            "state": self._state.value,
            "long_qty": self._long_qty,
            "short_qty": self._short_qty,
            "long_exchange": self._long_exchange,
            "short_exchange": self._short_exchange,
            "long_symbol": self._long_symbol,
            "short_symbol": self._short_symbol,
            "long_entry_price": self._long_entry_price,
            "short_entry_price": self._short_entry_price,
        }
        try:
            self._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(self._STATE_FILE, "w") as fh:
                json.dump(data, fh, indent=2)
            logger.debug("Saved fn position state to %s", self._STATE_FILE)
        except Exception as exc:
            logger.warning("Failed to save fn position state: %s", exc)

    def load_state(self) -> bool:
        """Load persisted position state from disk. Returns True if state was restored."""
        if not self._STATE_FILE.exists():
            return False
        try:
            with open(self._STATE_FILE) as fh:
                data = json.load(fh)
            saved_state = data.get("state", "IDLE")
            if saved_state not in ("HOLDING",):
                logger.debug("Saved state is %s — not restoring position", saved_state)
                return False
            self._state = JobState.HOLDING
            self._long_qty = float(data.get("long_qty", 0))
            self._short_qty = float(data.get("short_qty", 0))
            self._long_exchange = data.get("long_exchange", "")
            self._short_exchange = data.get("short_exchange", "")
            self._long_symbol = data.get("long_symbol", "")
            self._short_symbol = data.get("short_symbol", "")
            self._long_entry_price = float(data.get("long_entry_price", 0))
            self._short_entry_price = float(data.get("short_entry_price", 0))
            logger.info("Restored fn position: state=%s long=%.6f short=%.6f",
                        saved_state, self._long_qty, self._short_qty)
            return True
        except Exception as exc:
            logger.warning("Failed to load fn position state: %s", exc)
            return False

    async def sync_position_from_exchange(self) -> bool:
        """After restoring from disk, query actual exchange positions and overwrite
        _long_qty / _short_qty with real values. Disk values may be stale.
        Uses WS position cache when fresh, REST as fallback.
        Returns True on success, False on failure."""
        long_exch = self._long_exchange
        short_exch = self._short_exchange
        long_sym = self._long_symbol
        short_sym = self._short_symbol
        if not long_exch or not short_exch:
            return True

        long_client = self._clients.get(long_exch)
        short_client = self._clients.get(short_exch)
        if not long_client or not short_client:
            logger.warning("sync_position_from_exchange: missing client for %s or %s", long_exch, short_exch)
            return False

        try:
            long_size = float(await self._get_position_size(long_exch, long_sym, long_client))
            short_size = float(await self._get_position_size(short_exch, short_sym, short_client))

            old_long, old_short = self._long_qty, self._short_qty
            self._long_qty = long_size
            self._short_qty = -short_size
            logger.info("Synced from exchange: long %.6f→%.6f (%s) short %.6f→%.6f (%s)",
                        old_long, self._long_qty, long_exch,
                        old_short, self._short_qty, short_exch)
            # If both positions are zero, reset to IDLE (position was closed externally)
            if long_size < 1e-8 and short_size < 1e-8 and self._state == JobState.HOLDING:
                logger.info("Exchange positions are zero — resetting state to IDLE")
                self._state = JobState.HOLDING  # keep for transition log
                self._transition(JobState.IDLE)
                self._long_entry_price = 0.0
                self._short_entry_price = 0.0
            self.save_state()
            return True
        except Exception as exc:
            logger.warning("sync_position_from_exchange failed: %s", exc)
            return False

    def _compute_entry_prices(self, result: ExecutionResult, config: MakerTakerConfig) -> None:
        """Compute VWAP entry prices from chunk results and store on position."""
        maker_total_qty = 0.0
        maker_cost = 0.0
        taker_total_qty = 0.0
        taker_cost = 0.0
        for c in result.chunks:
            if c.maker_filled_qty > 0 and c.maker_price > 0:
                maker_total_qty += c.maker_filled_qty
                maker_cost += c.maker_filled_qty * c.maker_price
            if c.taker_filled_qty > 0 and c.taker_price > 0:
                taker_total_qty += c.taker_filled_qty
                taker_cost += c.taker_filled_qty * c.taker_price

        maker_vwap = maker_cost / maker_total_qty if maker_total_qty > 0 else 0.0
        taker_vwap = taker_cost / taker_total_qty if taker_total_qty > 0 else 0.0

        # Maker side = buy → maker is the long leg; maker side = sell → maker is the short leg
        if config.maker_side == "buy":
            self._long_entry_price = maker_vwap
            self._short_entry_price = taker_vwap
        else:
            self._long_entry_price = taker_vwap
            self._short_entry_price = maker_vwap

        logger.info(
            "Entry prices computed: long=%.4f short=%.4f (maker_vwap=%.4f taker_vwap=%.4f)",
            self._long_entry_price, self._short_entry_price, maker_vwap, taker_vwap,
        )

    # ── Core execution ────────────────────────────────────────────────────────────

    async def _execute_maker_taker(self, config: MakerTakerConfig, action: str) -> ExecutionResult:
        """Execute the full TWAP: N chunks of maker-place → wait → taker-hedge.

        After each chunk, queries actual exchange positions to detect and repair
        any imbalance. Continues extra chunks beyond the planned N if total_qty
        has not been fully filled.
        """
        self._current_config = config
        self._chunk_results = []
        self._carry_over_gap = Decimal("0")
        result = ExecutionResult(action=action, start_ts=time.time())

        # Snapshot baseline positions BEFORE any chunks execute
        # so we compare only the delta this run creates, not leftover positions
        self._baseline_maker_size, self._baseline_taker_size = await self._snapshot_baseline_positions(config)
        self._log("RISK", f"Baseline positions: {config.maker_exchange}={self._baseline_maker_size:.6f} {config.taker_exchange}={self._baseline_taker_size:.6f}")

        # ── Config summary (once before first chunk) ──
        self._log("INFO",
            f"Execution config: qty={config.total_qty} chunks={config.num_chunks} "
            f"interval={config.chunk_interval_s}s timeout={config.maker_timeout_ms}ms "
            f"chase={config.maker_max_chase_rounds} offset={config.maker_offset_ticks}ticks"
        )
        guard_parts = [f"Spread gate: [{config.min_spread_pct}, {config.max_spread_pct}]%"]
        if config.use_depth_spread:
            guard_parts.append(f"Depth spread: ON (max {config.max_slippage_bps}bps)")
        else:
            guard_parts.append("Depth spread: OFF")
        if config.taker_drift_guard:
            guard_parts.append(f"Drift guard: ON (max {config.max_taker_drift_bps}bps)")
        else:
            guard_parts.append("Drift guard: OFF")
        if config.reduce_only:
            guard_parts.append("reduce_only=True")
        if config.simulation:
            guard_parts.append("SIMULATION")
        self._log("INFO", " | ".join(guard_parts))

        # Generate execution ID for AI training data
        _exec_id = new_execution_id()
        # Derive pair name from maker symbol (strip exchange-specific suffixes)
        _pair = config.maker_symbol.split("_")[0].split("-Perp")[0]

        # Query min order size from taker exchange (used as repair threshold)
        taker_client = self._clients.get(config.taker_exchange)
        try:
            taker_min_size = float(await taker_client.async_get_min_order_size(config.taker_symbol)) if taker_client else 0.1
        except Exception:
            taker_min_size = 0.1
        min_repair_qty = max(taker_min_size, 0.02)  # at least 0.02 — exchanges often reject below this
        self._log("RISK", f"Taker min order size: {taker_min_size} → min_repair_qty={min_repair_qty}")

        base_chunk_qty = config.total_qty / config.num_chunks
        filled_so_far = Decimal("0")
        chunk_index = 0

        # Query maker min order size (used for sub-minimum market-close fallback)
        maker_client_init = self._clients.get(config.maker_exchange)
        try:
            maker_min_size = float(await maker_client_init.async_get_min_order_size(config.maker_symbol)) if maker_client_init else 0.1
        except Exception:
            maker_min_size = 0.1
        min_chunk_size = Decimal(str(max(maker_min_size, taker_min_size, 0.02)))

        while True:
            i = chunk_index

            # ── Pause checkpoint: block until resumed ──
            if not await self._wait_if_paused():
                result.error = "Execution aborted during pause"
                break
            if not self._is_executing():
                result.error = "Execution aborted"
                break

            # Determine chunk quantity
            if i < config.num_chunks:
                # Planned chunks
                if i == config.num_chunks - 1:
                    chunk_qty = config.total_qty - filled_so_far
                else:
                    chunk_qty = base_chunk_qty
            else:
                # Extra chunks: fill whatever is remaining
                remaining = config.total_qty - filled_so_far
                if remaining <= Decimal("0.001"):
                    break  # target reached
                chunk_qty = min(base_chunk_qty, remaining)
                self._log("CHUNK", f"Extra chunk {i}: chunk_qty={chunk_qty:.6f} (remaining={remaining:.6f}, base={base_chunk_qty:.6f}) to fill target {config.total_qty}")

            # Apply carry-over gap from previous chunk's sub-minimum residual
            if self._carry_over_gap > 0 and chunk_qty > self._carry_over_gap:
                self._log("RISK", f"Chunk {i}: reducing maker qty by carry-over gap {self._carry_over_gap:.6f}")
                chunk_qty -= self._carry_over_gap
                self._carry_over_gap = Decimal("0")

            self._current_chunk_index = i

            if i > 0:
                logger.info("TWAP chunk %d/%d: waiting %.1fs", i + 1, max(config.num_chunks, i + 1), config.chunk_interval_s)
                await asyncio.sleep(config.chunk_interval_s)

            # For reduce_only exits: cap chunk_qty to actual remaining maker position
            # Must run AFTER sleep — position may have changed due to previous chunk's repair
            if config.reduce_only:
                try:
                    maker_client = self._clients.get(config.maker_exchange)
                    remaining_pos = await self._get_position_size(config.maker_exchange, config.maker_symbol, maker_client, force_rest=True)
                    if remaining_pos < Decimal("0.001"):
                        self._log("EXIT", f"Chunk {i}: maker position already closed (remaining={remaining_pos}) — stopping TWAP")
                        break
                    if chunk_qty > remaining_pos:
                        self._log("RISK", f"Chunk {i}: capping chunk_qty {chunk_qty} → {remaining_pos} (remaining maker position)")
                        chunk_qty = remaining_pos
                except Exception as exc:
                    self._log("RISK", f"Chunk {i}: failed to query maker position for cap: {exc} — using uncapped qty", level="warn")

            # ── Sub-minimum market-close fallback ──
            # If chunk_qty is below exchange min order size, a maker post-only
            # order will be rejected. Use simultaneous IOC on both sides instead.
            if chunk_qty > Decimal("0") and chunk_qty < min_chunk_size:
                self._log("EXIT", f"Chunk {i}: chunk_qty={chunk_qty:.6f} below min order size {min_chunk_size} — using market-close")
                mc_chunk = await self._market_close_residual(config, chunk_qty, chunk_index=i)
                self._chunk_results.append(mc_chunk)
                result.chunks.append(mc_chunk)
                result.total_maker_qty += mc_chunk.maker_filled_qty
                result.total_taker_qty += mc_chunk.taker_filled_qty
                filled_so_far += Decimal(str(mc_chunk.maker_filled_qty))
                if mc_chunk.maker_filled_qty > 0 or mc_chunk.taker_filled_qty > 0:
                    self._update_position_incremental(config, action, mc_chunk.maker_filled_qty, mc_chunk.taker_filled_qty)
                if mc_chunk.error:
                    self._log("EXIT", f"Chunk {i}: market-close error: {mc_chunk.error} — accepting residual as dust", level="warn")
                break  # done — sub-minimum qty handled (or accepted as dust)

            # ── Pre-chunk balance check (skip chunk 0 — no prior fills) ──
            if i > 0 and self._is_executing():
                try:
                    pre_gap, pre_maker_d, pre_taker_d = await self._mandatory_verify_positions(config, i, expected_maker_delta=result.total_maker_qty, expected_taker_delta=result.total_taker_qty)
                    if pre_gap is not None and pre_gap >= min_repair_qty:
                        # Only repair if taker is behind (not oversized)
                        taker_behind = True
                        if pre_maker_d is not None and pre_taker_d is not None:
                            if abs(pre_taker_d) > abs(pre_maker_d) + float(min_repair_qty):
                                taker_behind = False
                                self._log("RISK", f"Chunk {i}: pre-chunk taker already ahead — skipping pre-repair")
                        if taker_behind:
                            self._log("RISK", f"Chunk {i}: pre-chunk gap={pre_gap:.6f} — rebalancing before chunk start")
                            repair_ok = await self._repair_imbalance(config, i, ChunkResult(chunk_index=i, maker_exchange=config.maker_exchange, taker_exchange=config.taker_exchange, start_ts=time.time()), pre_gap)
                            if repair_ok:
                                result.total_taker_qty += pre_gap
                                self._log("RISK", f"Chunk {i}: pre-chunk rebalance OK — repaired {pre_gap:.6f}")
                            else:
                                self._log("RISK", f"Chunk {i}: pre-chunk rebalance FAILED — proceeding anyway", level="warn")
                except Exception as exc:
                    self._log("RISK", f"Chunk {i}: pre-chunk balance check failed: {exc}", level="warn")

            # ── OHI check per chunk (opt-in) ──
            if config.ohi_monitoring and self._data_layer:
                # Map each exchange to its symbol via maker/taker assignment — works for
                # both entry and exit regardless of which side is maker.
                _ohi_exch_sym = {
                    config.maker_exchange: config.maker_symbol,
                    config.taker_exchange: config.taker_symbol,
                }
                ohi_ok = True
                for ohi_exch in [config.long_exchange, config.short_exchange]:
                    ohi_sym = _ohi_exch_sym.get(ohi_exch)
                    if not ohi_exch or not ohi_sym:
                        continue
                    try:
                        ohi_data = self._data_layer.get_orderbook_health(ohi_exch, ohi_sym)
                        ohi_val = ohi_data.get("ohi", 0) if ohi_data else 0
                        parts = []
                        if ohi_data:
                            for k in ("spread_score", "depth_score", "symmetry_score"):
                                if k in ohi_data:
                                    parts.append(f"{k.replace('_score','')}={ohi_data[k]:.2f}")
                        detail = f" ({', '.join(parts)})" if parts else ""
                        passed = ohi_val >= config.min_ohi
                        self._log("OHI", f"Chunk {i} {ohi_exch}:{ohi_sym} = {ohi_val:.2f}{detail} — {'OK' if passed else 'BELOW threshold'} (min {config.min_ohi})")
                        if not passed:
                            ohi_ok = False
                    except Exception as ohi_exc:
                        self._log("OHI", f"Chunk {i} {ohi_exch} — unavailable ({ohi_exc})", level="warn")
                if not ohi_ok:
                    self._log("OHI", f"Chunk {i}: OHI below threshold — waiting 5s before retry", level="warn")
                    await asyncio.sleep(5.0)
                    # Re-check after wait; if still below, skip chunk but continue TWAP
                    skip_chunk = False
                    for ohi_exch in [config.long_exchange, config.short_exchange]:
                        ohi_sym = _ohi_exch_sym.get(ohi_exch)
                        if not ohi_exch or not ohi_sym:
                            continue
                        try:
                            ohi_data = self._data_layer.get_orderbook_health(ohi_exch, ohi_sym)
                            ohi_val = ohi_data.get("ohi", 0) if ohi_data else 0
                            if ohi_val < config.min_ohi:
                                skip_chunk = True
                                self._log("OHI", f"Chunk {i} {ohi_exch} still below threshold (OHI={ohi_val:.2f}) — skipping chunk", level="warn")
                        except Exception:
                            pass
                    if skip_chunk:
                        chunk_index += 1
                        continue

            chunk = await self._execute_single_chunk(config, i, chunk_qty, execution_id=_exec_id, action=action, pair=_pair)
            self._chunk_results.append(chunk)
            result.chunks.append(chunk)

            result.total_maker_qty += chunk.maker_filled_qty
            result.total_taker_qty += chunk.taker_filled_qty
            filled_so_far += Decimal(str(chunk.maker_filled_qty))

            # ── Live position update (from API fill reports) ──
            if chunk.maker_filled_qty > 0 or chunk.taker_filled_qty > 0:
                self._update_position_incremental(config, action, chunk.maker_filled_qty, chunk.taker_filled_qty)

            # ── Real exchange position verification (MANDATORY) ──
            # Blocks until both exchange positions are confirmed — no new orders until then.
            await asyncio.sleep(1.0)
            pos_gap, actual_maker_delta, actual_taker_delta = await self._mandatory_verify_positions(
                config, i,
                expected_maker_delta=result.total_maker_qty,
                expected_taker_delta=result.total_taker_qty,
            )

            # If mandatory verify returned None, execution was aborted
            if pos_gap is None:
                if not self._is_executing():
                    result.error = "Execution aborted during position verification"
                    break

            # Exchange position gap is authoritative
            chunk_gap = abs(chunk.maker_filled_qty - chunk.taker_filled_qty)
            effective_gap = pos_gap if pos_gap is not None else 0.0

            # Sync filled_so_far with actual maker position to prevent overfilling
            if actual_maker_delta is not None:
                actual_maker_dec = abs(Decimal(str(actual_maker_delta)))
                if actual_maker_dec > filled_so_far + Decimal("0.01"):
                    old_filled = filled_so_far
                    filled_so_far = actual_maker_dec
                    result.total_maker_qty = float(actual_maker_dec)
                    self._log("RISK", f"Chunk {i}: synced filled_so_far {old_filled:.6f} → {filled_so_far:.6f} (actual exchange delta)")

            # Direction guard: only repair if maker has MORE than taker (taker needs catching up).
            # If taker already has more, buying more on taker side would snowball the imbalance.
            # Compare absolute deltas — signed comparison breaks for sell/exit trades.
            taker_oversized = False
            if actual_maker_delta is not None and actual_taker_delta is not None:
                if abs(actual_taker_delta) > abs(actual_maker_delta) + float(min_repair_qty):
                    taker_oversized = True
                    self._log("RISK", f"Chunk {i}: taker OVERSIZED (|maker_delta|={abs(actual_maker_delta):.6f} |taker_delta|={abs(actual_taker_delta):.6f}) — skipping repair to avoid snowball", level="error")

            # Sanity cap: repair should never exceed 2× the current chunk qty
            max_repair = float(chunk_qty) * 2

            if effective_gap >= min_repair_qty and not taker_oversized:
                if effective_gap > max_repair and chunk.maker_filled_qty > 0:
                    self._log("RISK", f"Chunk {i}: repair gap {effective_gap:.6f} exceeds sanity cap {max_repair:.6f} (2× chunk_qty={chunk_qty}) — capping", level="warn")
                    effective_gap = max_repair
                logger.info("REPAIR TRIGGERED chunk %d: effective_gap=%.6f (pos_gap=%s chunk_gap=%.6f) min_repair=%.6f",
                            i, effective_gap, pos_gap, chunk_gap, min_repair_qty)
                self._log("RISK", f"Chunk {i}: effective gap={effective_gap:.6f} (exchange={pos_gap}, chunk={chunk_gap:.6f}) — repair IOC on taker side")
                repair_ok = False
                remaining_gap = effective_gap
                total_repaired = 0.0
                attempt = 0
                while remaining_gap >= min_repair_qty:
                    attempt += 1
                    # Check if bot was stopped or paused during repair
                    if not self._is_executing():
                        self._log("RISK", f"Chunk {i}: repair aborted — state changed to {self._state.value}", level="warn")
                        break
                    if not await self._wait_if_paused():
                        self._log("RISK", f"Chunk {i}: repair aborted during pause", level="warn")
                        break
                    repair_ok = await self._repair_imbalance(config, i, chunk, remaining_gap)
                    if repair_ok:
                        total_repaired += remaining_gap
                        break
                    # Re-query actual exchange positions to get true remaining gap
                    await asyncio.sleep(1.0)
                    new_gap, new_maker_d, new_taker_d = await self._mandatory_verify_positions(config, i, expected_maker_delta=result.total_maker_qty, expected_taker_delta=result.total_taker_qty)
                    if new_maker_d is not None:
                        filled_so_far = Decimal(str(new_maker_d))
                        result.total_maker_qty = float(new_maker_d)
                    if new_gap is not None and new_gap > min_repair_qty:
                        total_repaired += (remaining_gap - new_gap)
                        remaining_gap = new_gap
                    else:
                        # Exchange positions are balanced now (partial repair was enough)
                        total_repaired += remaining_gap
                        repair_ok = True
                        self._log("RISK", f"Chunk {i}: positions balanced after partial repair (remaining={new_gap:.6f})" if new_gap else f"Chunk {i}: positions balanced after partial repair")
                        break
                    self._log("RISK", f"Chunk {i}: repair attempt {attempt} — remaining_gap={remaining_gap:.6f} — retrying with fresh orderbook", level="warn")
                    await asyncio.sleep(2.0)
                if remaining_gap < min_repair_qty and not repair_ok:
                    self._log("RISK", f"Chunk {i}: remaining gap {remaining_gap:.6f} below min repair qty {min_repair_qty} — accepting")
                    repair_ok = True
                if repair_ok and total_repaired > 0:
                    result.total_taker_qty += total_repaired
                    if chunk.error:
                        self._log("RISK", f"Chunk {i}: repair succeeded — clearing taker error (was: {chunk.error})")
                        chunk.error = None
                # Re-sync position from exchange after repair (also updates _long_qty/_short_qty)
                await self._mandatory_verify_positions(config, i, expected_maker_delta=result.total_maker_qty, expected_taker_delta=result.total_taker_qty)
            elif effective_gap >= min_repair_qty and taker_oversized:
                self._log("RISK", f"Chunk {i}: gap={effective_gap:.6f} NOT repaired (taker oversized) — will retry in pre-chunk check", level="warn")
            elif effective_gap > 0.001:
                self._log("RISK", f"Chunk {i}: micro gap={effective_gap:.6f} below min repair qty {min_repair_qty} — accepting")

            # If exchange confirms positions are balanced, clear taker fill errors
            # (taker fill-check sometimes reports 0 even though GRVT actually filled)
            # Only clear if maker actually filled something — if maker_filled_qty==0
            # the chunk truly failed and we should stop, not retry indefinitely.
            if pos_gap is not None and pos_gap <= min_repair_qty and chunk.error and chunk.maker_filled_qty > 0:
                self._log("RISK", f"Chunk {i}: exchange confirms balanced (gap={pos_gap:.6f}) — clearing taker error (was: {chunk.error})")
                # Correct taker qty to match exchange reality (fill-check reported 0 but exchange filled)
                if chunk.taker_filled_qty < chunk.maker_filled_qty:
                    missing = chunk.maker_filled_qty - chunk.taker_filled_qty
                    result.total_taker_qty += missing
                    chunk.taker_filled_qty = chunk.maker_filled_qty
                    self._log("RISK", f"Chunk {i}: corrected taker_filled_qty += {missing:.6f} (exchange-verified)")
                chunk.error = None

            if chunk.error:
                # Classify error: fatal (stop TWAP) vs retryable (continue as new chunk)
                _fatal_keywords = ("Missing client", "Execution cancelled", "Execution aborted")
                _is_fatal = any(kw in (chunk.error or "") for kw in _fatal_keywords)
                if _is_fatal:
                    logger.error("Chunk %d FATAL error: %s — stopping TWAP", i, chunk.error)
                    result.error = f"Chunk {i} failed: {chunk.error}"
                    break
                else:
                    logger.warning("Chunk %d retryable error: %s — will retry as next chunk", i, chunk.error)
                    self._log("CHUNK", f"Chunk {i}: retryable error ({chunk.error}) — continuing to next chunk", level="warn")

            chunk_index += 1

            # Check if we've completed all planned chunks
            if i >= config.num_chunks - 1 and i < config.num_chunks:
                # Last planned chunk done — check if we need extra chunks
                if filled_so_far >= config.total_qty - Decimal("0.001"):
                    break  # target reached
                else:
                    self._log("CHUNK", f"After {config.num_chunks} chunks: filled={filled_so_far:.6f} target={config.total_qty} — continuing extra chunks")
                    continue
            elif i >= config.num_chunks:
                # Already in extra chunks — check again
                if filled_so_far >= config.total_qty - Decimal("0.001"):
                    break
                continue
            # Still within planned chunks — continue loop

        # ── Cleanup: cancel all open orders on maker exchange ──────────
        # Chase loop cancels are best-effort; stale orders may survive on
        # exchanges with cancel latency (e.g. Extended). Belt-and-suspenders.
        maker_client = self._clients.get(config.maker_exchange)
        if maker_client and hasattr(maker_client, "async_cancel_all_orders"):
            try:
                await maker_client.async_cancel_all_orders()
                self._log("ORDER", f"Post-TWAP cleanup: cancelled all open orders on {config.maker_exchange}")
            except Exception as exc:
                self._log("ORDER", f"Post-TWAP cleanup: cancel_all failed on {config.maker_exchange}: {exc}", level="warn")

        result.end_ts = time.time()
        result.success = (
            result.error is None
            and result.total_maker_qty > 0
            and result.total_taker_qty > 0
        )

        # Ensure result.error is always populated on failure so callers log meaningful detail
        if not result.success and result.error is None:
            chunk_errs = [c.error for c in result.chunks if c.error]
            if chunk_errs:
                result.error = f"Chunks failed: {'; '.join(chunk_errs[-3:])}"
            elif result.total_maker_qty > 0 and result.total_taker_qty == 0:
                result.error = f"Maker filled {result.total_maker_qty:.6f} but taker not filled"
            else:
                result.error = "No fills — spread gate or order placement failed"

        logger.info(
            "%s complete: %d chunks, maker=%.6f taker=%.6f success=%s (%.1fs)",
            action, len(result.chunks), result.total_maker_qty, result.total_taker_qty,
            result.success, result.end_ts - result.start_ts,
        )
        return result

    async def _execute_single_chunk(
        self, config: MakerTakerConfig, chunk_index: int, chunk_qty: Decimal,
        execution_id: str = "", action: str = "", pair: str = "",
    ) -> ChunkResult:
        """Execute one chunk: maker post-only → wait for fill → taker IOC hedge."""
        chunk_start_time = time.time()
        chunk = ChunkResult(
            chunk_index=chunk_index,
            maker_exchange=config.maker_exchange,
            taker_exchange=config.taker_exchange,
            start_ts=chunk_start_time,
        )

        maker_client = self._clients.get(config.maker_exchange)
        taker_client = self._clients.get(config.taker_exchange)

        # ── Chunk startup logging ─────────────────────────────────────
        self._log("CHUNK", f"=== STARTING CHUNK {chunk_index} ===")
        self._log("CHUNK", f"Config: action={action}, pair={pair}, execution_id={execution_id}")
        self._log("CHUNK", f"Maker: {config.maker_exchange} {config.maker_side} {config.maker_symbol}, qty={chunk_qty}")
        self._log("CHUNK", f"Taker: {config.taker_exchange} {config.taker_side} {config.taker_symbol}, qty={chunk_qty}")
        self._log("CHUNK", f"Settings: chunks={config.num_chunks}, timeout={config.maker_timeout_ms}ms, offset_ticks={config.maker_offset_ticks}")
        self._log("CHUNK", f"Spread gate: min={config.min_spread_pct}%, max={config.max_spread_pct}%, depth_spread={config.use_depth_spread}")
        self._log("CHUNK", f"Options: drift_guard={config.taker_drift_guard}, ohi_monitor={config.ohi_monitoring}")

        # ── AI training: capture orderbook snapshot at decision time ──
        _el_snapshot: dict = {}
        _el_chase_rounds = 0
        if self._execution_logger and self._data_layer:
            try:
                _el_snapshot = self._execution_logger.capture_snapshot(
                    self._data_layer,
                    config.maker_exchange, config.maker_symbol,
                    config.taker_exchange, config.taker_symbol,
                    funding_monitor=self._funding_monitor,
                )
                # BTC vol is async — fire and forget, will be in snapshot for record_chunk
                asyncio.create_task(self._execution_logger.capture_btc_volatility(_el_snapshot))
            except Exception as el_exc:
                logger.debug("ExecutionLogger snapshot error: %s", el_exc)

        if not maker_client or not taker_client:
            missing = []
            if not maker_client:
                missing.append(f"maker={config.maker_exchange}")
            if not taker_client:
                missing.append(f"taker={config.taker_exchange}")
            chunk.error = f"Missing clients: {', '.join(missing)}"
            self._log("CHUNK", f"Chunk {chunk_index}: ABORTED - Missing {len(missing)} client(s): {missing}", level="error")
            chunk.end_ts = time.time()
            return chunk

        if config.simulation:
            self._log("SIM", f"Chunk {chunk_index}: maker {config.maker_side} {config.maker_symbol} {chunk_qty}, taker {config.taker_side} {config.taker_symbol} {chunk_qty}")
            logger.info("[SIM] Chunk %d: maker %s %s %.6f, taker %s %s %.6f",
                        chunk_index, config.maker_side, config.maker_symbol, chunk_qty,
                        config.taker_side, config.taker_symbol, chunk_qty)
            chunk.maker_filled_qty = float(chunk_qty)
            chunk.taker_filled_qty = float(chunk_qty)
            chunk.state = ChunkState.CHUNK_DONE
            chunk.end_ts = time.time()
            return chunk

        # ── Step 1: Get best price for maker order ────────────────────
        self._log("BOOK", f"Chunk {chunk_index}: Step 1 - Getting orderbook for maker order placement")
        try:
            book = await self._get_book(config.maker_exchange, config.maker_symbol, maker_client, chunk_index)
        except Exception as exc:
            chunk.error = f"Failed to fetch maker orderbook: {exc}"
            self._log("BOOK", f"Chunk {chunk_index}: orderbook fetch FAILED: {exc}", level="error")
            chunk.end_ts = time.time()
            return chunk

        tick = await maker_client.async_get_tick_size(config.maker_symbol)

        if config.maker_side == "buy":
            if not book.get("bids"):
                chunk.error = "No bids in maker orderbook"
                chunk.end_ts = time.time()
                return chunk
            best = Decimal(str(book["bids"][0][0]))
            maker_price = best + tick * config.maker_offset_ticks
        else:
            if not book.get("asks"):
                chunk.error = "No asks in maker orderbook"
                chunk.end_ts = time.time()
                return chunk
            best = Decimal(str(book["asks"][0][0]))
            maker_price = best - tick * config.maker_offset_ticks
        self._log("BOOK", f"Chunk {chunk_index}: best={best} → maker_price={maker_price} (offset={config.maker_offset_ticks} ticks)")

        # ── Step 2: Place maker post-only order + infinite chase loop ──
        maker_filled_qty = Decimal("0")
        maker_order_id = None
        remaining_qty = chunk_qty
        chase_round = 0
        # Taker sweep price computed in spread gate — reused at hedge time
        taker_sweep_price: Decimal | None = None
        # Snapshot maker position at chunk start for late-fill detection
        chunk_start_maker_pos = await self._get_position_size(
            config.maker_exchange, config.maker_symbol, maker_client, force_rest=True)
        mid_loop_hedged = Decimal("0")  # taker qty hedged mid-loop for late fills

        while True:
            # Check if execution was cancelled externally
            if not self._is_executing():
                # Cancel any open maker order before aborting
                if maker_order_id is not None:
                    self._log("ORDER", f"Chunk {chunk_index}: execution cancelled — cancelling open order {maker_order_id}")
                    try:
                        await maker_client.async_cancel_order(str(maker_order_id))
                    except Exception:
                        pass
                chunk.error = "Execution cancelled"
                chunk.end_ts = time.time()
                return chunk

            # ── Depth-aware spread guard (opt-in) ──
            if config.use_depth_spread:
                self._log("SPREAD", f"Chunk {chunk_index} round {chase_round}: Depth-aware spread guard ENABLED (max_slippage_bps={config.max_slippage_bps})")
                depth_ok = False
                depth_check_start = time.time()
                while self._is_executing():
                    try:
                        sg_m_book = await self._get_book(config.maker_exchange, config.maker_symbol, maker_client, chunk_index)
                        sg_t_book = await self._get_book(config.taker_exchange, config.taker_symbol, taker_client, chunk_index)
                    except Exception as sg_exc:
                        self._log("SPREAD", f"Chunk {chunk_index} round {chase_round}: depth book fetch failed ({sg_exc}) — falling back to BBO", level="warn")
                        depth_ok = True
                        break
                    if config.maker_side == "buy":
                        long_book, short_book = sg_m_book, sg_t_book
                    else:
                        long_book, short_book = sg_t_book, sg_m_book
                    analysis = _analyze_depth_spread(long_book, short_book, remaining_qty, config.max_slippage_bps)
                    if analysis is None:
                        self._log("SPREAD", f"Chunk {chunk_index} round {chase_round}: depth analysis unavailable — falling back to BBO", level="warn")
                        depth_ok = True
                        break
                    self.last_depth_analysis = analysis
                    if analysis.is_acceptable:
                        self._log("SPREAD", f"Chunk {chunk_index} round {chase_round}: depth spread bbo={analysis.bbo_spread_pct:+.4f}% exec={analysis.exec_spread_pct:+.4f}% slip={analysis.slippage_bps:.1f}bps — OK (max {config.max_slippage_bps}bps)")
                        depth_ok = True
                        break
                    self._log("SPREAD", f"Chunk {chunk_index} round {chase_round}: depth slip={analysis.slippage_bps:.1f}bps exceeds {config.max_slippage_bps}bps — waiting 2s", level="warn")
                    await asyncio.sleep(2.0)
                if not depth_ok:
                    continue

            # ── Per-round spread gate: BBO + taker depth check ──────────
            # Two conditions must both pass before placing a maker order:
            #   1. BBO spread is within [min_spread_pct, max_spread_pct]
            #   2. Taker orderbook has sufficient depth to fill remaining_qty
            #      at a price that keeps the spread within bounds
            # The worst taker fill price (sweep price) is stored and reused at
            # hedge time — the IOC limit is set exactly at that level.
            if config.max_spread_pct > 0 or config.min_spread_pct < 0:
                self._log("SPREAD", f"Chunk {chunk_index} round {chase_round}: Entering spread gate check (min={config.min_spread_pct}%, max={config.max_spread_pct}%)")
                spread_ok = False
                spread_check_iterations = 0
                while self._is_executing():
                    spread_check_iterations += 1
                    try:
                        sg_m_book = await self._get_book(config.maker_exchange, config.maker_symbol, maker_client, chunk_index)
                        sg_t_book = await self._get_book(config.taker_exchange, config.taker_symbol, taker_client, chunk_index)
                    except Exception as sg_exc:
                        self._log("SPREAD", f"Chunk {chunk_index} round {chase_round}: book fetch failed ({sg_exc}) — skipping spread check", level="warn")
                        spread_ok = True
                        break

                    # Step 1: BBO spread check
                    long_ask: float | None = None
                    short_bid: float | None = None
                    if config.maker_side == "buy":
                        long_ask = float(sg_m_book["asks"][0][0]) if sg_m_book.get("asks") else None
                        short_bid = float(sg_t_book["bids"][0][0]) if sg_t_book.get("bids") else None
                    else:
                        long_ask = float(sg_t_book["asks"][0][0]) if sg_t_book.get("asks") else None
                        short_bid = float(sg_m_book["bids"][0][0]) if sg_m_book.get("bids") else None
                    if long_ask is None or short_bid is None or short_bid <= 0:
                        self._log("SPREAD", f"Chunk {chunk_index} round {chase_round}: incomplete books — skipping spread check", level="warn")
                        spread_ok = True
                        break
                    sg_pct = (long_ask - short_bid) / short_bid * 100
                    if sg_pct < config.min_spread_pct:
                        self._log("SPREAD", f"Chunk {chunk_index} round {chase_round}: BBO spread {sg_pct:+.4f}% below min {config.min_spread_pct}% — waiting 2s", level="warn")
                        await asyncio.sleep(2.0)
                        continue
                    if sg_pct > config.max_spread_pct:
                        self._log("SPREAD", f"Chunk {chunk_index} round {chase_round}: BBO spread {sg_pct:+.4f}% exceeds max {config.max_spread_pct}% — waiting 2s", level="warn")
                        await asyncio.sleep(2.0)
                        continue

                    # Step 2: taker depth check — can the taker book absorb remaining_qty
                    # and what is the worst price (sweep level) we'd need to hit?
                    taker_side_for_walk = config.taker_side  # "buy" or "sell"
                    t_vwap, t_worst, t_unfilled = walk_book(sg_t_book, taker_side_for_walk, remaining_qty)

                    if t_worst <= 0 or t_vwap <= 0:
                        self._log("SPREAD", f"Chunk {chunk_index} round {chase_round}: taker book empty — skipping depth check", level="warn")
                        taker_sweep_price = None
                        spread_ok = True
                        break

                    if t_unfilled > float(remaining_qty) * 0.05:
                        # Taker can fill < 95% of remaining_qty — wait for liquidity
                        self._log("SPREAD", f"Chunk {chunk_index} round {chase_round}: taker depth insufficient — only {float(remaining_qty)-t_unfilled:.2f}/{float(remaining_qty):.2f} available at sweep={t_worst:.4f} — waiting 2s", level="warn")
                        await asyncio.sleep(2.0)
                        continue

                    # Verify spread holds at the VWAP fill price, not just BBO
                    if config.maker_side == "buy":
                        # maker buys (long_ask = maker best ask), taker sells at t_vwap
                        depth_spread_pct = (long_ask - t_vwap) / t_vwap * 100
                    else:
                        # maker sells (short_bid = maker best bid), taker buys at t_vwap
                        depth_spread_pct = (t_vwap - short_bid) / short_bid * 100

                    if depth_spread_pct > config.max_spread_pct:
                        self._log("SPREAD", f"Chunk {chunk_index} round {chase_round}: depth-weighted spread {depth_spread_pct:+.4f}% exceeds max {config.max_spread_pct}% (taker VWAP={t_vwap:.4f}, unfilled={t_unfilled:.2f}) — waiting 2s", level="warn")
                        await asyncio.sleep(2.0)
                        continue

                    # Both checks pass — store sweep price for taker hedge
                    taker_sweep_price = Decimal(str(t_worst))
                    self._log("SPREAD", f"Chunk {chunk_index} round {chase_round}: BBO={sg_pct:+.4f}% depth={depth_spread_pct:+.4f}% — OK (taker VWAP={t_vwap:.4f} sweep={t_worst:.4f} unfilled={t_unfilled:.2f})")
                    spread_ok = True
                    break

                if not spread_ok:
                    self._log("SPREAD", f"Chunk {chunk_index} round {chase_round}: spread_ok is False, restarting loop", level="warn")
                    continue

            # Spread gate passed - proceeding to place maker order
            self._log("SPREAD", f"Chunk {chunk_index} round {chase_round}: Spread gate PASSED after {spread_check_iterations} iteration(s)")
            self._current_chunk_state = ChunkState.MAKER_PLACE

            self._log("ORDER", f"Chunk {chunk_index} round {chase_round}: === PLACING MAKER ORDER ===")
            self._log("ORDER", f"Chunk {chunk_index} round {chase_round}: Exchange={config.maker_exchange}, Side={config.maker_side.upper()}, Qty={remaining_qty}, Symbol={config.maker_symbol}, Price={maker_price}")
            try:
                # Retry on transient connection/DNS errors
                for _conn_attempt in range(1, _MAX_CONN_RETRIES + 1):
                    try:
                        resp = await maker_client.async_create_post_only_order(
                            symbol=config.maker_symbol,
                            side=config.maker_side,
                            amount=remaining_qty,
                            price=maker_price,
                            reduce_only=config.reduce_only,
                        )
                        break  # success
                    except Exception as conn_exc:
                        if self._is_transient(conn_exc) and _conn_attempt < _MAX_CONN_RETRIES:
                            self._log("ORDER", f"Chunk {chunk_index}: maker connection error (attempt {_conn_attempt}/{_MAX_CONN_RETRIES}): {conn_exc} — retrying in {_CONN_RETRY_DELAY}s", level="warn")
                            await asyncio.sleep(_CONN_RETRY_DELAY)
                            continue
                        raise  # non-transient or retries exhausted
                maker_order_id = resp.get("id") or resp.get("order_id") or resp.get("digest")
                chunk.maker_order_id = maker_order_id
                chunk.maker_price = float(maker_price)
                self._log("ORDER", f"Chunk {chunk_index}: maker order placed → id={maker_order_id}")
            except RuntimeError as exc:
                if "post-only" in str(exc).lower() or "FAIL_POST_ONLY" in str(exc):
                    self._log("ORDER", f"Chunk {chunk_index} round {chase_round}: POST-ONLY rejected — re-anchoring to market", level="warn")
                    logger.warning("Post-only rejected (round %d): %s — re-anchoring", chase_round, exc)
                    chase_round += 1
                    # Limit consecutive post-only rejections to avoid infinite loop
                    if chase_round > 50:
                        chunk.error = f"Post-only rejected {chase_round} times — aborting chunk"
                        self._log("ORDER", f"Chunk {chunk_index}: POST-ONLY rejected too many times — aborting", level="error")
                        chunk.end_ts = time.time()
                        return chunk
                    # Re-fetch book and re-anchor to current best price
                    await asyncio.sleep(0.5)
                    try:
                        book = await self._get_book(config.maker_exchange, config.maker_symbol, maker_client)
                        if config.maker_side == "buy" and book.get("bids"):
                            maker_price = Decimal(str(book["bids"][0][0])) - tick
                        elif config.maker_side == "sell" and book.get("asks"):
                            maker_price = Decimal(str(book["asks"][0][0])) + tick
                        self._log("ORDER", f"Chunk {chunk_index}: re-anchored to {maker_price} (1 tick conservative)")
                    except Exception as book_exc:
                        self._log("ORDER", f"Chunk {chunk_index}: book re-fetch failed ({book_exc}), shifting by {config.maker_reprice_ticks} ticks")
                        if config.maker_side == "buy":
                            maker_price -= tick * config.maker_reprice_ticks
                        else:
                            maker_price += tick * config.maker_reprice_ticks
                    continue
                chunk.error = f"Maker order failed: {exc}"
                self._log("ORDER", f"Chunk {chunk_index}: maker order FAILED: {exc}", level="error")
                chunk.end_ts = time.time()
                return chunk
            except Exception as exc:
                import traceback as _tb
                logger.error("Maker order traceback:\n%s", _tb.format_exc())
                chunk.error = f"Maker order failed: {exc}"
                self._log("ORDER", f"Chunk {chunk_index}: maker order FAILED: {exc}", level="error")
                chunk.end_ts = time.time()
                return chunk

            # ── Step 3: Wait for maker fill ───────────────────────────
            self._current_chunk_state = ChunkState.MAKER_WAIT
            filled = await self._wait_for_maker_fill(
                maker_client, maker_order_id, config.maker_timeout_ms,
                config=config, taker_client=taker_client,
            )

            if filled.get("drift_cancelled"):
                # Taker-drift-guard cancelled the maker order — re-run spread gates
                self._log("DRIFT", f"Chunk {chunk_index} round {chase_round}: taker drift exceeded {config.max_taker_drift_bps}bps — maker cancelled, re-evaluating spread")
                chase_round += 1
                maker_order_id = None
                # Re-fetch book for repricing
                try:
                    book = await self._get_book(config.maker_exchange, config.maker_symbol, maker_client)
                    if config.maker_side == "buy" and book.get("bids"):
                        maker_price = Decimal(str(book["bids"][0][0])) + tick * config.maker_offset_ticks
                    elif config.maker_side == "sell" and book.get("asks"):
                        maker_price = Decimal(str(book["asks"][0][0])) - tick * config.maker_offset_ticks
                except Exception:
                    pass
                continue  # back to top → spread gates re-check

            if filled.get("filled"):
                maker_filled_qty = Decimal(str(filled.get("traded_qty", 0)))
                self._log("FILL", f"Chunk {chunk_index}: MAKER FILLED qty={maker_filled_qty} (round {chase_round})")
                logger.info("Maker filled: qty=%.6f (round %d)", maker_filled_qty, chase_round)
                break
            elif filled.get("traded_qty", 0) > 0:
                # Partial fill — cancel remainder, then re-check actual filled qty
                maker_filled_qty = Decimal(str(filled["traded_qty"]))
                self._log("FILL", f"Chunk {chunk_index}: MAKER PARTIAL FILL qty={maker_filled_qty} — cancelling remainder")
                logger.info("Maker partial fill: qty=%.6f — cancelling remainder", maker_filled_qty)
                await maker_client.async_cancel_order(str(maker_order_id))
                self._log("ORDER", f"Chunk {chunk_index}: cancelled remainder of {maker_order_id}")
                # Re-check: more fills may have arrived before cancel was processed
                await asyncio.sleep(0.3)
                try:
                    final_check = await maker_client.async_check_order_fill(str(maker_order_id))
                    final_qty = Decimal(str(final_check.get("traded_qty", 0)))
                    if final_qty > maker_filled_qty:
                        self._log("FILL", f"Chunk {chunk_index}: post-cancel recheck: qty {maker_filled_qty} → {final_qty}")
                        maker_filled_qty = final_qty
                except Exception as exc:
                    self._log("ORDER", f"Chunk {chunk_index}: post-cancel recheck failed: {exc}", level="warn")
                # Market moved since sweep price was computed — force fresh walk_book for taker
                taker_sweep_price = None
                break
            else:
                # No fill — cancel and reprice
                self._log("ORDER", f"Chunk {chunk_index} round {chase_round}: maker timeout — cancelling order {maker_order_id}")
                logger.info("Maker timeout (round %d) — cancelling and repricing", chase_round + 1)
                cancel_ok = await maker_client.async_cancel_order(str(maker_order_id))
                if not cancel_ok:
                    self._log("ORDER", f"Chunk {chunk_index}: cancel FAILED for {maker_order_id} — retrying", level="warn")
                    # Retry cancel once
                    await asyncio.sleep(0.5)
                    cancel_ok = await maker_client.async_cancel_order(str(maker_order_id))
                    if not cancel_ok:
                        # Nuclear fallback: cancel ALL open orders on this exchange
                        # to prevent zombie orders from accumulating
                        self._log("ORDER", f"Chunk {chunk_index}: cancel retry FAILED — using cancel_all_orders as safety net", level="error")
                        if hasattr(maker_client, "async_cancel_all_orders"):
                            try:
                                await maker_client.async_cancel_all_orders()
                                self._log("ORDER", f"Chunk {chunk_index}: cancel_all_orders sent to {config.maker_exchange}")
                            except Exception as ca_exc:
                                self._log("ORDER", f"Chunk {chunk_index}: cancel_all_orders failed: {ca_exc}", level="error")
                self._log("ORDER", f"Chunk {chunk_index}: cancel result={'OK' if cancel_ok else 'FAILED (used cancel_all)'}")

                # CRITICAL: After cancel, verify the order wasn't already filled
                # The cancel may have arrived too late (order already matched).
                await asyncio.sleep(0.3)  # small delay for exchange to process cancel
                try:
                    post_cancel = await maker_client.async_check_order_fill(str(maker_order_id))
                    post_cancel_qty = float(post_cancel.get("traded_qty", 0))
                    if post_cancel.get("filled") or post_cancel_qty > 0:
                        maker_filled_qty = Decimal(str(post_cancel_qty))
                        self._log("FILL", f"Chunk {chunk_index}: order {maker_order_id} was FILLED before cancel! qty={maker_filled_qty}")
                        logger.info("Order filled before cancel: qty=%.6f", maker_filled_qty)
                        break
                except Exception as exc:
                    self._log("ORDER", f"Chunk {chunk_index}: post-cancel fill check failed: {exc}", level="warn")

                # Also check WS fill events
                ws_qty = self._get_ws_filled_qty(str(maker_order_id))
                if ws_qty > 0:
                    maker_filled_qty = Decimal(str(ws_qty))
                    self._log("FILL", f"Chunk {chunk_index}: WS detected fill after cancel! qty={maker_filled_qty}")
                    break

                # Before repricing: query actual exchange position to detect
                # late fills from previous rounds (Extended has fill-reporting
                # latency — cancel may "succeed" but order was already matched).
                try:
                    actual_pos = await self._get_position_size(
                        config.maker_exchange, config.maker_symbol, maker_client, force_rest=True)
                    chunk_delta = abs(actual_pos - chunk_start_maker_pos)
                    if chunk_delta > Decimal("0.001"):
                        # Exchange position grew from late fills — treat as filled
                        maker_filled_qty = chunk_delta
                        remaining_qty = max(Decimal("0"), chunk_qty - chunk_delta)
                        self._log("FILL", f"Chunk {chunk_index}: exchange position detected late fill! chunk_delta={chunk_delta} (pos {chunk_start_maker_pos}→{actual_pos}) — remaining={remaining_qty}")

                        # Immediately hedge the unhedged portion on taker side
                        unhedged = chunk_delta - mid_loop_hedged
                        if unhedged > Decimal("0.001"):
                            try:
                                t_book = await self._get_book(config.taker_exchange, config.taker_symbol, taker_client)
                                t_tick = await taker_client.async_get_tick_size(config.taker_symbol)
                                if config.taker_side == "buy":
                                    t_best = Decimal(str(t_book["asks"][0][0])) if t_book.get("asks") else None
                                    t_price = t_best + t_tick * 50 if t_best else None
                                else:
                                    t_best = Decimal(str(t_book["bids"][0][0])) if t_book.get("bids") else None
                                    t_price = t_best - t_tick * 50 if t_best else None
                                if t_price is not None:
                                    self._log("FILL", f"Chunk {chunk_index}: mid-loop hedge IOC {config.taker_side.upper()} {unhedged} {config.taker_symbol} @ {t_price} (best={t_best})")
                                    for _ml_attempt in range(1, 3):
                                        try:
                                            t_resp = await taker_client.async_create_ioc_order(
                                                symbol=config.taker_symbol, side=config.taker_side,
                                                amount=unhedged, price=t_price, reduce_only=config.reduce_only)
                                            break
                                        except Exception as _ml_exc:
                                            if self._is_transient(_ml_exc) and _ml_attempt < 2:
                                                await asyncio.sleep(0.5)
                                                continue
                                            raise
                                    t_id = t_resp.get("id") or t_resp.get("order_id") or t_resp.get("digest")
                                    t_filled = await self._check_taker_fill(taker_client, t_id, t_resp)
                                    if t_filled > 0:
                                        mid_loop_hedged += Decimal(str(t_filled))
                                        self._log("FILL", f"Chunk {chunk_index}: mid-loop hedge FILLED qty={t_filled:.6f} (total mid-loop hedged={mid_loop_hedged})")
                                    else:
                                        self._log("FILL", f"Chunk {chunk_index}: mid-loop hedge NOT FILLED — will retry at chunk end", level="warn")
                            except Exception as hedge_exc:
                                self._log("FILL", f"Chunk {chunk_index}: mid-loop hedge failed: {hedge_exc} — will retry at chunk end", level="warn")

                        if remaining_qty < Decimal("0.001"):
                            break  # chunk fully filled by late fills
                        # If remaining is small (< 10% of chunk), treat as done
                        # to avoid post-only rejection loops for tiny leftover qty
                        if remaining_qty < chunk_qty * Decimal("0.1"):
                            self._log("FILL", f"Chunk {chunk_index}: remaining {remaining_qty} < 10% of chunk — treating as filled")
                            break
                except Exception as exc:
                    self._log("ORDER", f"Chunk {chunk_index}: pre-reprice position check failed: {exc}", level="warn")

                # Re-fetch book and re-anchor to current best price
                try:
                    book = await self._get_book(config.maker_exchange, config.maker_symbol, maker_client)
                    old_price = maker_price
                    top_bid = book["bids"][0] if book.get("bids") else None
                    top_ask = book["asks"][0] if book.get("asks") else None
                    logger.info("Reprice round %d: top_bid=%s top_ask=%s old=%s", chase_round, top_bid, top_ask, old_price)
                    if config.maker_side == "buy" and top_bid:
                        maker_price = Decimal(str(top_bid[0]))
                    elif config.maker_side == "sell" and top_ask:
                        maker_price = Decimal(str(top_ask[0]))
                    moved = "MOVED" if maker_price != old_price else "unchanged"
                    logger.info("Reprice round %d: %s → new_price=%s", chase_round, moved, maker_price)
                    self._log("ORDER", f"Chunk {chunk_index}: repriced to {maker_price} ({moved}, old={old_price})")
                except Exception as exc:
                    logger.warning("Reprice round %d: book fetch failed: %s", chase_round, exc)
                    self._log("ORDER", f"Chunk {chunk_index}: book fetch failed ({exc}), keeping {maker_price}")

                chase_round += 1
                taker_sweep_price = None  # invalidate — spread gate will recompute next round

        _el_chase_rounds = chase_round  # capture for AI training data

        if maker_filled_qty <= 0:
            chunk.error = "Maker not filled (cancelled)"
            chunk.end_ts = time.time()
            return chunk

        chunk.maker_filled_qty = float(maker_filled_qty)

        # ── Step 4: Immediate taker hedge ─────────────────────────────
        # Subtract any amount already hedged mid-loop (from late-fill detection)
        taker_hedge_qty = maker_filled_qty - mid_loop_hedged
        chunk.taker_filled_qty = float(mid_loop_hedged)  # credit mid-loop hedges

        if taker_hedge_qty <= Decimal("0.001"):
            self._log("FILL", f"Chunk {chunk_index}: taker fully hedged mid-loop ({mid_loop_hedged}) — skipping end-of-chunk hedge")
        else:
            self._current_chunk_state = ChunkState.TAKER_HEDGE
            self._log("BOOK", f"Chunk {chunk_index}: fetching {config.taker_exchange} orderbook for taker hedge (remaining={taker_hedge_qty}, mid-loop={mid_loop_hedged})")

            try:
                # Snapshot taker position before IOC — best-effort cache read only.
                # Do NOT fall through to REST here: this snapshot runs on the critical
                # path between maker fill and taker IOC placement. A stale/missing
                # snapshot is fine (position delta check just skips).
                taker_pos_before = Decimal("0")
                if self._data_layer:
                    _pos_snap = self._data_layer.get_position(config.taker_exchange, config.taker_symbol)
                    taker_pos_before = Decimal(str(abs(_pos_snap.size)))

                taker_book = await self._get_book(config.taker_exchange, config.taker_symbol, taker_client)
                taker_tick = await taker_client.async_get_tick_size(config.taker_symbol)

                if config.taker_side == "buy":
                    if not taker_book.get("asks"):
                        raise RuntimeError("No asks in taker orderbook")
                    taker_best = Decimal(str(taker_book["asks"][0][0]))
                    # Always walk the freshly-fetched book — taker_sweep_price from spread gate
                    # may be stale (book can move significantly during maker fill wait).
                    _, t_worst, _ = walk_book(taker_book, "buy", taker_hedge_qty)
                    taker_price = Decimal(str(t_worst)) + taker_tick * 50 if t_worst > 0 else taker_best + taker_tick * 50
                else:
                    if not taker_book.get("bids"):
                        raise RuntimeError("No bids in taker orderbook")
                    taker_best = Decimal(str(taker_book["bids"][0][0]))
                    _, t_worst, _ = walk_book(taker_book, "sell", taker_hedge_qty)
                    taker_price = Decimal(str(t_worst)) - taker_tick * 50 if t_worst > 0 else taker_best - taker_tick * 50

                self._log("ORDER", f"Chunk {chunk_index}: placing IOC {config.taker_side.upper()} {taker_hedge_qty} {config.taker_symbol} @ {taker_price} (best={taker_best} book_worst={t_worst:.4f}) on {config.taker_exchange}")
                # Retry on transient connection/DNS errors — short delay for IOC (time-critical)
                _TAKER_CONN_RETRIES = 2
                _TAKER_RETRY_DELAY = 0.5
                for _conn_attempt in range(1, _TAKER_CONN_RETRIES + 1):
                    try:
                        taker_resp = await taker_client.async_create_ioc_order(
                            symbol=config.taker_symbol,
                            side=config.taker_side,
                            amount=taker_hedge_qty,
                            price=taker_price,
                            reduce_only=config.reduce_only,
                        )
                        break  # success
                    except Exception as conn_exc:
                        if self._is_transient(conn_exc) and _conn_attempt < _TAKER_CONN_RETRIES:
                            self._log("ORDER", f"Chunk {chunk_index}: taker connection error (attempt {_conn_attempt}/{_TAKER_CONN_RETRIES}): {conn_exc} — retrying in {_TAKER_RETRY_DELAY}s", level="warn")
                            await asyncio.sleep(_TAKER_RETRY_DELAY)
                            continue
                        raise  # non-transient or retries exhausted

                taker_order_id = taker_resp.get("id") or taker_resp.get("order_id") or taker_resp.get("digest")
                chunk.taker_order_id = taker_order_id
                chunk.taker_price = float(taker_price)
                self._log("ORDER", f"Chunk {chunk_index}: taker IOC placed → id={taker_order_id}")

                # Check taker fill (WS fill-event path)
                taker_filled = await self._check_taker_fill(taker_client, taker_order_id, taker_resp)

                # ── Position-delta fallback: if WS fill events missed the fill,
                # verify via position size change (works for all exchanges: Extended/GRVT/Nado
                # via live WS cache; Variational via REST polling cache refreshed every 2s).
                if taker_filled <= 0 and self._data_layer:
                    pos_after = await self._get_position_size(
                        config.taker_exchange, config.taker_symbol, taker_client, force_rest=False)
                    delta = abs(pos_after - taker_pos_before)
                    if delta >= float(taker_hedge_qty) * 0.95:
                        taker_filled = float(delta)
                        self._log("FILL", f"Chunk {chunk_index}: taker fill confirmed via position delta: {taker_pos_before:.6f}→{pos_after:.6f} Δ={delta:.6f}")
                    elif delta > 0:
                        # Partial position change — credit what the exchange confirms
                        taker_filled = float(delta)
                        self._log("FILL", f"Chunk {chunk_index}: taker partial fill via position delta: Δ={delta:.6f} of {float(taker_hedge_qty):.6f}", level="warn")

                chunk.taker_filled_qty += taker_filled  # add to mid-loop hedged amount

                if taker_filled > 0:
                    self._log("FILL", f"Chunk {chunk_index}: TAKER FILLED qty={taker_filled:.6f} (total chunk taker={chunk.taker_filled_qty:.6f})")
                else:
                    chunk.error = "Taker IOC not filled — needs emergency unwind of maker fill"
                    self._log("FILL", f"Chunk {chunk_index}: TAKER NOT FILLED — emergency unwind needed", level="error")
                    logger.error("Taker hedge FAILED for chunk %d — maker filled %.6f but taker 0",
                                 chunk_index, maker_filled_qty)

            except Exception as exc:
                chunk.error = f"Taker hedge failed: {exc}"
                self._log("ORDER", f"Chunk {chunk_index}: taker hedge FAILED: {exc}", level="error")
                logger.error("Taker hedge exception for chunk %d: %s", chunk_index, exc)

        self._current_chunk_state = ChunkState.CHUNK_DONE
        chunk.state = ChunkState.CHUNK_DONE
        chunk.end_ts = time.time()

        duration_ms = (chunk.end_ts - chunk.start_ts) * 1000
        self._log("CHUNK", f"Chunk {chunk_index} DONE: maker={chunk.maker_filled_qty:.6f}@{chunk.maker_price:.2f} taker={chunk.taker_filled_qty:.6f}@{chunk.taker_price:.2f} ({duration_ms:.0f}ms)")
        logger.info(
            "Chunk %d done: maker=%.6f@%.2f taker=%.6f@%.2f (%.1fms)",
            chunk_index, chunk.maker_filled_qty, chunk.maker_price,
            chunk.taker_filled_qty, chunk.taker_price,
            duration_ms,
        )

        # ── AI training: record chunk result ──
        if self._execution_logger and _el_snapshot:
            try:
                self._execution_logger.record_chunk(
                    execution_id=execution_id,
                    action=action,
                    chunk_index=chunk_index,
                    snapshot=_el_snapshot,
                    chunk_result=chunk,
                    config=config,
                    chase_rounds=_el_chase_rounds,
                    pair=pair,
                )
            except Exception as el_exc:
                logger.debug("ExecutionLogger record error: %s", el_exc)

        return chunk

    # ── Position tracking helpers ────────────────────────────────────

    async def _get_position_size(self, exchange: str, symbol: str, client, force_rest: bool = False) -> Decimal:
        """Read position size from WS cache (DataLayer) first, REST as fallback.

        Returns absolute position size as Decimal.
        If force_rest is True, skip the cache and always query REST.
        """
        # Try WS cache first (near-instant, no network)
        if self._data_layer and not force_rest:
            snap = self._data_layer.get_position(exchange, symbol)
            # Trust cache if fresh (<3s) OR if WS is connected (event-based
            # streams like GRVT v1.position only push on change — age can be
            # high even though the data is perfectly valid).
            if snap.connected and (snap.update_count > 0 or self._data_layer.is_position_fresh(exchange, symbol, max_age_ms=3000)):
                age_ms = round(time.time() * 1000 - snap.timestamp_ms) if snap.timestamp_ms else -1
                logger.debug("Position from WS cache: %s:%s size=%.6f (age=%dms, updates=%d)",
                             exchange, symbol, snap.size, age_ms, snap.update_count)
                return Decimal(str(snap.size))

        # REST fallback
        if client:
            # Only VariationalClient supports max_retries; other clients don't.
            # Use max_retries=2 (not None) — infinite retries are for order placement
            # only; a position read that takes >20s blocks the IOC critical path.
            from app.variational_client import VariationalClient
            if isinstance(client, VariationalClient):
                positions = await client.async_fetch_positions([symbol], max_retries=2)
            else:
                positions = await client.async_fetch_positions([symbol])
            logger.info("Position REST query %s:%s returned %d positions", exchange, symbol, len(positions))
            for p in positions:
                p_inst = p.get("instrument", p.get("symbol", ""))
                p_size = abs(Decimal(str(p.get("size", 0))))
                logger.info("Position REST match check: p_inst=%s vs symbol=%s size=%s", p_inst, symbol, p_size)
                if p_inst == symbol:
                    return p_size
                # Fallback: match by underlying (handles Variational funding_interval mismatch)
                try:
                    p_parts = p_inst.split("-")
                    s_parts = symbol.split("-")
                    if len(p_parts) >= 2 and len(s_parts) >= 2 and p_parts[1].upper() == s_parts[1].upper():
                        logger.info("Position REST matched by underlying: %s ~ %s → size=%s", p_inst, symbol, p_size)
                        return p_size
                except Exception:
                    pass
            if positions:
                logger.warning("Position REST: no match found for %s in %d positions", symbol, len(positions))
        return Decimal("0")

    async def _snapshot_baseline_positions(
        self, config: MakerTakerConfig,
    ) -> tuple[Decimal, Decimal]:
        """Snapshot current positions on both exchanges before TWAP starts.

        Returns (maker_abs_size, taker_abs_size) as Decimal. Defaults to 0 on error.
        Always uses REST to ensure fresh data.
        """
        maker_size = Decimal("0")
        taker_size = Decimal("0")
        try:
            maker_client = self._clients.get(config.maker_exchange)
            taker_client = self._clients.get(config.taker_exchange)
            maker_size = await self._get_position_size(config.maker_exchange, config.maker_symbol, maker_client, force_rest=True)
            taker_size = await self._get_position_size(config.taker_exchange, config.taker_symbol, taker_client, force_rest=True)
        except Exception as exc:
            logger.warning("Baseline position snapshot failed: %s", exc)
        logger.info("Baseline positions: %s:%s=%s  %s:%s=%s",
                     config.maker_exchange, config.maker_symbol, maker_size,
                     config.taker_exchange, config.taker_symbol, taker_size)
        return maker_size, taker_size

    async def _verify_exchange_positions(
        self, config: MakerTakerConfig, chunk_index: int,
        expected_maker_delta: float = 0.0, expected_taker_delta: float = 0.0,
    ) -> tuple[float | None, float | None, float | None]:
        """Query actual positions and return (gap, maker_delta, taker_delta).

        Compares only the DELTA from baseline (positions created by this run),
        not total exchange positions (which may include leftover positions).
        Returns (gap, maker_delta, taker_delta) or (None, None, None) on error.

        If expected_maker_delta or expected_taker_delta are non-zero but the API
        returns 0 (or errors), retries every 3s up to 3 times to avoid acting on
        flaky API results (e.g. Variational returning empty positions).

        Uses WS position cache (DataLayer) when fresh (<3s), REST as fallback.
        Also sets _long_qty/_short_qty to the actual absolute exchange positions
        so the UI always shows the real state.
        """
        maker_client = self._clients.get(config.maker_exchange)
        taker_client = self._clients.get(config.taker_exchange)
        if not maker_client or not taker_client:
            return None, None, None

        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                # Always use fresh REST data — the DataLayer cache can be up to 2s
                # stale for REST-polled exchanges (e.g. Variational), which causes
                # the repair logic to see size=0 for positions just opened.
                maker_size = await self._get_position_size(config.maker_exchange, config.maker_symbol, maker_client, force_rest=True)
                taker_size = await self._get_position_size(config.taker_exchange, config.taker_symbol, taker_client, force_rest=True)

                # Calculate position deltas from baseline
                maker_delta_raw = maker_size - self._baseline_maker_size
                taker_delta_raw = taker_size - self._baseline_taker_size

                # Compare only the delta from baseline (Decimal arithmetic — no float rounding)
                maker_delta = maker_delta_raw
                taker_delta = taker_delta_raw
                gap = abs(maker_delta - taker_delta)

                # Only retry if there's a real imbalance (gap > 0.5)
                # Gap = 0 means positions are balanced, even if API shows 0 for both sides
                if gap > Decimal("0.5") and attempt < max_retries:
                    self._log("RISK", f"Chunk {chunk_index}: Position imbalance detected (maker_delta={maker_delta}, taker_delta={taker_delta}, gap={gap}) — retry {attempt+1}/{max_retries} in 3s", level="warn")
                    await asyncio.sleep(3.0)
                    continue
                logger.info("Verify chunk %d: maker=%s(base=%s delta=%s) taker=%s(base=%s delta=%s) gap=%s",
                            chunk_index, maker_size, self._baseline_maker_size, maker_delta,
                            taker_size, self._baseline_taker_size, taker_delta, gap)
                self._log("RISK", f"Chunk {chunk_index}: POSITIONS {config.maker_exchange}={maker_size}(delta={maker_delta}) {config.taker_exchange}={taker_size}(delta={taker_delta}) gap={gap}")

                # Authoritative UI position sync — use absolute exchange sizes
                if config.maker_side == "buy":
                    self._long_qty = float(maker_size)
                    self._short_qty = -float(taker_size)
                else:
                    self._long_qty = float(taker_size)
                    self._short_qty = -float(maker_size)

                return float(gap), float(maker_delta), float(taker_delta)

            except Exception as exc:
                if attempt < max_retries:
                    self._log("RISK", f"Chunk {chunk_index}: position check failed ({exc}) — retry {attempt+1}/{max_retries} in 3s", level="warn")
                    await asyncio.sleep(3.0)
                    continue
                self._log("RISK", f"Chunk {chunk_index}: position verification failed after {max_retries} retries: {exc}", level="warn")
                logger.warning("Position verification failed chunk %d after %d retries: %s", chunk_index, max_retries, exc)
                return None, None, None

        # Should not reach here, but safety fallback
        return None, None, None

    async def _mandatory_verify_positions(
        self, config: MakerTakerConfig, chunk_index: int,
        expected_maker_delta: float = 0.0, expected_taker_delta: float = 0.0,
    ) -> tuple[float | None, float | None, float | None]:
        """Like _verify_exchange_positions but retries indefinitely until both sides are confirmed.

        Blocks the TWAP — no new orders are placed until positions on both exchanges
        are successfully queried. Only returns (None, None, None) if execution is
        aborted (state changed) or paused and then aborted.
        """
        attempt = 0
        while True:
            # Allow abort / stop
            if not self._is_executing():
                self._log("RISK", f"Chunk {chunk_index}: mandatory verify aborted — state={self._state.value}")
                return None, None, None

            # Allow pause to block
            if not await self._wait_if_paused():
                return None, None, None

            result = await self._verify_exchange_positions(
                config, chunk_index,
                expected_maker_delta=expected_maker_delta,
                expected_taker_delta=expected_taker_delta,
            )
            if result[0] is not None:
                return result

            attempt += 1
            self._log("RISK", f"Chunk {chunk_index}: position verification failed (attempt {attempt}) — retrying in 5s (NO new orders until confirmed)", level="error")
            await asyncio.sleep(5.0)

    def _update_position_incremental(
        self, config: MakerTakerConfig, action: str,
        maker_qty: float, taker_qty: float,
    ) -> None:
        """Update _long_qty / _short_qty after a single chunk fill."""
        if action == "ENTER":
            if config.maker_side == "buy":
                self._long_qty += maker_qty
                self._short_qty -= taker_qty
            else:
                self._long_qty += taker_qty
                self._short_qty -= maker_qty
        else:  # EXIT
            if config.maker_side == "sell":
                self._long_qty -= maker_qty
                self._short_qty += taker_qty
            else:
                self._long_qty -= taker_qty
                self._short_qty += maker_qty
        logger.debug("Position update: long=%.6f short=%.6f", self._long_qty, self._short_qty)

    async def _repair_imbalance(
        self, config: MakerTakerConfig, chunk_index: int,
        chunk: "ChunkResult", gap: float,
    ) -> bool:
        """Send a repair IOC on the taker side to close an intra-chunk imbalance.

        Returns True if the repair filled, False otherwise.
        """
        taker_client = self._clients.get(config.taker_exchange)
        if taker_client is None:
            self._log("RISK", f"Chunk {chunk_index}: no taker client for repair", level="error")
            return False

        try:
            # During reduce_only exits: check taker position before repairing
            # to prevent selling past zero and flipping to wrong side
            if config.reduce_only:
                taker_remaining = await self._get_position_size(
                    config.taker_exchange, config.taker_symbol, taker_client, force_rest=True)
                if taker_remaining < Decimal("0.001"):
                    self._log("RISK", f"Chunk {chunk_index}: taker position already closed (remaining={taker_remaining}) — skipping repair")
                    return True
                gap_dec = Decimal(str(gap))
                if gap_dec > taker_remaining:
                    self._log("RISK", f"Chunk {chunk_index}: capping repair {gap:.6f} → {taker_remaining} (remaining taker position)")
                    gap = float(taker_remaining)

            taker_tick = await taker_client.async_get_tick_size(config.taker_symbol)
            taker_book = await self._get_book(config.taker_exchange, config.taker_symbol, taker_client)

            if config.taker_side == "buy":
                if not taker_book.get("asks"):
                    return False
                taker_best = Decimal(str(taker_book["asks"][0][0]))
                repair_price = taker_best + taker_tick * 50
            else:
                if not taker_book.get("bids"):
                    return False
                taker_best = Decimal(str(taker_book["bids"][0][0]))
                repair_price = taker_best - taker_tick * 50

            # Snapshot taker position BEFORE repair for delta verification
            pre_repair_pos = Decimal("0")
            try:
                pre_repair_pos = await self._get_position_size(
                    config.taker_exchange, config.taker_symbol, taker_client, force_rest=True)
            except Exception:
                pass

            self._log("RISK", f"Chunk {chunk_index}: repair IOC {config.taker_side.upper()} {gap:.6f} {config.taker_symbol} @ {repair_price} (best={taker_best})")
            resp = await taker_client.async_create_ioc_order(
                symbol=config.taker_symbol,
                side=config.taker_side,
                amount=Decimal(str(gap)),
                price=repair_price,
                reduce_only=config.reduce_only,
            )

            repair_id = resp.get("id") or resp.get("order_id") or resp.get("digest")
            repair_filled = await self._check_taker_fill(taker_client, repair_id, resp)

            # Belt-and-suspenders: verify fill via position delta
            try:
                await asyncio.sleep(0.5)
                post_repair_pos = await self._get_position_size(
                    config.taker_exchange, config.taker_symbol, taker_client, force_rest=True)
                actual_repair_delta = float(abs(post_repair_pos - pre_repair_pos))
                if repair_filled > 0 and actual_repair_delta < repair_filled * 0.5:
                    self._log("RISK", f"Chunk {chunk_index}: repair position delta={actual_repair_delta:.6f} doesn't match reported fill={repair_filled:.6f} — using position delta", level="error")
                    repair_filled = actual_repair_delta
            except Exception as pos_exc:
                self._log("RISK", f"Chunk {chunk_index}: repair position verification failed: {pos_exc} — trusting fill report", level="warn")

            if repair_filled >= gap - 0.001:
                chunk.taker_filled_qty += repair_filled
                self._log("RISK", f"Chunk {chunk_index}: repair FILLED qty={repair_filled:.6f} — balance restored")
                return True
            elif repair_filled > 0:
                chunk.taker_filled_qty += repair_filled
                residual = gap - repair_filled
                self._log("RISK", f"Chunk {chunk_index}: repair PARTIAL qty={repair_filled:.6f} of {gap:.6f} — residual={residual:.6f}", level="warn")
                return False  # caller will retry with residual
            else:
                self._log("RISK", f"Chunk {chunk_index}: repair IOC NOT FILLED", level="error")
                return False
        except Exception as exc:
            self._log("RISK", f"Chunk {chunk_index}: repair failed: {exc}", level="error")
            logger.error("Repair imbalance exception chunk %d: %s", chunk_index, exc)
            return False

    # ── Connection retry helper ───────────────────────────────────────

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """Return True if the exception is a transient network/DNS error worth retrying."""
        if isinstance(exc, _TRANSIENT_ERRORS):
            return True
        # aiohttp / httpx wrap DNS errors in their own types
        msg = str(exc).lower()
        if "no address associated with hostname" in msg or "name or service not known" in msg:
            return True
        if "connect" in msg and ("timeout" in msg or "refused" in msg or "reset" in msg):
            return True
        return False

    # ── Orderbook helper ─────────────────────────────────────────────

    async def _get_book(self, exchange: str, symbol: str, client, chunk_index: int = -1) -> dict:
        """Read orderbook from WS-based DataLayer cache. Falls back to REST if empty.

        Returns dict with 'bids' and 'asks' as [[price, qty], ...].
        The WS cache receives real-time updates (millisecond latency) and is
        always preferred over REST (~200-500ms roundtrip).  REST is only used
        as a fallback when no WS data has been received yet.
        """
        if self._data_layer:
            snap = self._data_layer.get_orderbook(exchange, symbol)
            if snap.bids and snap.asks:
                cache_age_ms = (time.time() * 1000) - snap.timestamp_ms if snap.timestamp_ms else 0
                self._log("BOOK", f"Chunk {chunk_index}: OMS cache hit for {exchange}:{symbol} (age={cache_age_ms:.0f}ms, updates={snap.update_count})")
                return {"bids": snap.bids, "asks": snap.asks}
            # OMS cache miss - will fall back to REST
            self._log("BOOK", f"Chunk {chunk_index}: OMS cache empty for {exchange}:{symbol} — falling back to REST", level="warn")
        # Fallback to REST (only when WS has no data at all)
        self._log("BOOK", f"Chunk {chunk_index}: fetching {exchange} orderbook via REST for {symbol}")
        try:
            start_ts = time.time()
            book = await client.async_fetch_order_book(symbol, limit=5)
            elapsed_ms = (time.time() - start_ts) * 1000
            bid_count = len(book.get("bids", []))
            ask_count = len(book.get("asks", []))
            self._log("BOOK", f"Chunk {chunk_index}: REST orderbook received for {exchange}:{symbol} (elapsed={elapsed_ms:.0f}ms, bids={bid_count}, asks={ask_count})")
            return book
        except Exception as exc:
            self._log("BOOK", f"Chunk {chunk_index}: REST orderbook FAILED for {exchange}:{symbol}: {exc}", level="error")
            raise

    # ── WS fill subscriptions ──────────────────────────────────────────

    async def start_fill_subscriptions(self, symbols_map: dict[str, str]) -> None:
        """Start WS fill subscriptions for all exchanges.

        Call before execution begins. Each exchange's async_subscribe_fills
        runs in a background task and pushes fills into _fill_events.
        """
        await self.stop_fill_subscriptions()
        self._fill_subs_running = True
        self._fill_events.clear()

        for exch_name, symbol in symbols_map.items():
            client = self._clients.get(exch_name)
            if client is None or not hasattr(client, "async_subscribe_fills"):
                continue
            task = asyncio.create_task(
                self._run_fill_subscription(client, exch_name, symbol),
                name=f"fill-ws-{exch_name}",
            )
            self._fill_sub_tasks.append(task)
            logger.info("StateMachine: started fill WS for %s:%s", exch_name, symbol)

    async def stop_fill_subscriptions(self) -> None:
        """Stop all WS fill subscription tasks."""
        self._fill_subs_running = False
        for task in self._fill_sub_tasks:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._fill_sub_tasks.clear()
        logger.info("StateMachine: fill WS subscriptions stopped")

    async def _run_fill_subscription(self, client, exch_name: str, symbol: str) -> None:
        """Wrapper that runs a fill subscription with auto-reconnect."""
        while self._fill_subs_running:
            try:
                await client.async_subscribe_fills(symbol, self._on_fill_event)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("StateMachine: fill WS %s error: %s — retrying in 3s", exch_name, exc)
                await asyncio.sleep(3)

    async def _on_fill_event(self, fill: dict) -> None:
        """Callback for WS fill events. Stores by order_id and signals waiters."""
        oid = str(fill.get("order_id", ""))
        if not oid:
            return
        self._fill_events.setdefault(oid, []).append(fill)
        self._fill_event.set()  # wake any waiter
        logger.info("WS fill: order=%s qty=%.6f price=%.4f taker=%s",
                    oid, fill.get("filled_qty", 0), fill.get("price", 0), fill.get("is_taker"))

    def _get_ws_filled_qty(self, order_id: str) -> float:
        """Sum all WS fill events for a given order_id."""
        fills = self._fill_events.get(str(order_id), [])
        return sum(f.get("filled_qty", 0) for f in fills)

    # ── Fill waiting ──────────────────────────────────────────────────

    async def _wait_for_maker_fill(
        self, client, order_id: str, timeout_ms: int,
        *, config: MakerTakerConfig | None = None, taker_client=None,
    ) -> dict:
        """Wait for a maker order to fill via WS events, with REST polling fallback.

        Primary: wait on _fill_event (set by WS callback) in short loops.
        Fallback: REST poll every 500ms if WS hasn't delivered.

        If taker_drift_guard is enabled in config, a parallel task monitors the
        taker-side orderbook mid-price.  When drift exceeds max_taker_drift_bps
        the maker order is proactively cancelled and {"drift_cancelled": True}
        is returned so the caller can re-evaluate spread gates.
        """
        if order_id is None:
            return {"filled": False, "traded_qty": 0}

        oid = str(order_id)
        wait_start = time.time()
        deadline = wait_start + (timeout_ms / 1000)
        timeout_s = timeout_ms / 1000
        ws_check_interval = 0.1  # check WS events every 100ms
        rest_poll_interval = 0.5  # REST fallback every 500ms
        last_rest_poll = 0.0

        # ── Taker-drift-guard setup ──
        drift_triggered = asyncio.Event()
        drift_task: asyncio.Task | None = None
        drift_label = "OFF"
        if (config and config.taker_drift_guard and taker_client
                and config.taker_exchange and config.taker_symbol):
            drift_task = asyncio.create_task(
                self._monitor_taker_drift(
                    config, taker_client, drift_triggered,
                ),
                name="taker-drift-monitor",
            )
            drift_label = f"ON (max {config.max_taker_drift_bps}bps)"

        self._log("WAIT", f"Waiting for maker fill: order={oid} timeout={timeout_s:.1f}s drift_guard={drift_label}")

        try:
            while time.time() < deadline:
                elapsed = time.time() - wait_start
                # Drift-guard check (non-blocking)
                if drift_triggered.is_set():
                    # Cancel maker order proactively
                    try:
                        await client.async_cancel_order(str(oid))
                    except Exception:
                        pass
                    self._log("DRIFT", f"Drift guard triggered — maker order {oid} cancelled after {elapsed:.1f}s")
                    logger.warning("Taker drift guard triggered — maker order %s cancelled", oid)
                    return {"filled": False, "traded_qty": 0, "drift_cancelled": True}

                # Check WS fill events first (instant)
                ws_qty = self._get_ws_filled_qty(oid)
                if ws_qty > 0:
                    # Settle window: exchanges like Extended deliver fills in rapid batches;
                    # wait 300ms for in-flight events, then REST-confirm final qty.
                    await asyncio.sleep(0.3)
                    ws_qty = self._get_ws_filled_qty(oid)
                    try:
                        rest_result = await client.async_check_order_fill(oid)
                        rest_qty = float(rest_result.get("traded_qty", 0))
                        if rest_qty > ws_qty:
                            self._log("FILL", f"WS settle REST correction: {ws_qty:.6f} → {rest_qty:.6f}")
                            ws_qty = rest_qty
                    except Exception as _exc:
                        self._log("WAIT", f"WS settle REST check failed: {_exc}", level="warn")
                    self._log("FILL", f"Maker fill via WS: qty={ws_qty:.6f} ({elapsed:.1f}s/{timeout_s:.1f}s)")
                    logger.info("Maker fill detected via WS: order=%s qty=%.6f", oid, ws_qty)
                    return {"filled": True, "traded_qty": ws_qty}

                # REST fallback at slower interval
                now = time.time()
                if now - last_rest_poll >= rest_poll_interval:
                    last_rest_poll = now
                    try:
                        result = await client.async_check_order_fill(oid)
                        r_status = result.get("status", "?")
                        r_qty = result.get("traded_qty", 0)
                        if result.get("filled"):
                            self._log("FILL", f"Maker fill via REST: qty={r_qty:.6f} status={r_status} ({elapsed:.1f}s/{timeout_s:.1f}s)")
                            logger.info("Maker fill detected via REST: order=%s qty=%.6f", oid, r_qty)
                            return result
                        if r_qty > 0:
                            self._log("FILL", f"Maker partial via REST: qty={r_qty:.6f} status={r_status} ({elapsed:.1f}s/{timeout_s:.1f}s)")
                            return result
                        self._log("WAIT", f"REST poll: status={r_status} traded_qty={r_qty} ({elapsed:.1f}s/{timeout_s:.1f}s)")
                    except Exception as exc:
                        self._log("WAIT", f"REST poll error: {exc} ({elapsed:.1f}s/{timeout_s:.1f}s)", level="warn")
                        logger.debug("Fill check error for %s: %s", oid, exc)

                # Wait for WS event or timeout
                self._fill_event.clear()
                try:
                    remaining = deadline - time.time()
                    wait_time = min(ws_check_interval, max(remaining, 0))
                    await asyncio.wait_for(self._fill_event.wait(), timeout=wait_time)
                except asyncio.TimeoutError:
                    pass

            total_wait = time.time() - wait_start

            # Final drift check before returning timeout
            if drift_triggered.is_set():
                try:
                    await client.async_cancel_order(str(oid))
                except Exception:
                    pass
                self._log("DRIFT", f"Drift guard triggered at timeout — maker order {oid} cancelled ({total_wait:.1f}s)")
                return {"filled": False, "traded_qty": 0, "drift_cancelled": True}

            # Final check: WS then REST (same settle logic as in-loop path)
            ws_qty = self._get_ws_filled_qty(oid)
            if ws_qty > 0:
                await asyncio.sleep(0.3)
                ws_qty = self._get_ws_filled_qty(oid)
                try:
                    rest_result = await client.async_check_order_fill(oid)
                    rest_qty = float(rest_result.get("traded_qty", 0))
                    if rest_qty > ws_qty:
                        self._log("FILL", f"WS settle REST correction (final): {ws_qty:.6f} → {rest_qty:.6f}")
                        ws_qty = rest_qty
                except Exception:
                    pass
                self._log("FILL", f"Maker fill via WS (final check): qty={ws_qty:.6f} ({total_wait:.1f}s)")
                return {"filled": True, "traded_qty": ws_qty}
            try:
                final = await client.async_check_order_fill(oid)
                f_qty = final.get("traded_qty", 0)
                f_status = final.get("status", "?")
                if final.get("filled") or f_qty > 0:
                    self._log("FILL", f"Maker fill via REST (final check): qty={f_qty} status={f_status} ({total_wait:.1f}s)")
                else:
                    self._log("WAIT", f"Maker wait TIMEOUT after {total_wait:.1f}s — no fill (final status={f_status})")
                return final
            except Exception:
                self._log("WAIT", f"Maker wait TIMEOUT after {total_wait:.1f}s — no fill (final REST check failed)")
                return {"filled": False, "traded_qty": 0}
        finally:
            # Always clean up drift monitor task
            if drift_task and not drift_task.done():
                drift_task.cancel()
                try:
                    await drift_task
                except (asyncio.CancelledError, Exception):
                    pass

    async def _monitor_taker_drift(
        self, config: MakerTakerConfig, taker_client,
        drift_triggered: asyncio.Event,
    ) -> None:
        """Background task: poll taker mid-price every ~1s and signal if drift exceeds budget.

        Runs concurrently with _wait_for_maker_fill.  Sets drift_triggered
        when |current_mid - baseline_mid| / baseline_mid > max_taker_drift_bps.
        """
        # Snapshot baseline taker mid-price
        try:
            book = await self._get_book(config.taker_exchange, config.taker_symbol, taker_client)
        except Exception as exc:
            self._log("DRIFT", f"Drift monitor: initial book fetch failed ({exc}) — guard disabled", level="warn")
            logger.warning("Drift monitor: initial taker book fetch failed (%s) — guard disabled", exc)
            return

        bids = book.get("bids", [])
        asks = book.get("asks", [])
        if not bids or not asks:
            self._log("DRIFT", "Drift monitor: taker book empty — guard disabled", level="warn")
            logger.warning("Drift monitor: taker book empty — guard disabled")
            return

        baseline_mid = (float(bids[0][0]) + float(asks[0][0])) / 2.0
        if baseline_mid <= 0:
            return

        self._log("DRIFT", f"Drift monitor started: {config.taker_exchange}:{config.taker_symbol} baseline_mid={baseline_mid:.4f} max={config.max_taker_drift_bps}bps")
        logger.info("Drift monitor started: taker=%s:%s baseline_mid=%.4f max_drift=%sbps",
                     config.taker_exchange, config.taker_symbol, baseline_mid, config.max_taker_drift_bps)

        while True:
            await asyncio.sleep(1.0)
            try:
                book = await self._get_book(config.taker_exchange, config.taker_symbol, taker_client)
                bids = book.get("bids", [])
                asks = book.get("asks", [])
                if not bids or not asks:
                    self._log("DRIFT", "Drift monitor: taker book empty — skipping", level="warn")
                    continue
                current_mid = (float(bids[0][0]) + float(asks[0][0])) / 2.0
                if current_mid <= 0:
                    continue
                drift_bps = abs(current_mid - baseline_mid) / baseline_mid * 10000
                if drift_bps > config.max_taker_drift_bps:
                    logger.warning(
                        "Drift monitor: taker %s:%s drifted %.1fbps (baseline=%.4f current=%.4f, max=%sbps) — triggering cancel",
                        config.taker_exchange, config.taker_symbol,
                        drift_bps, baseline_mid, current_mid, config.max_taker_drift_bps,
                    )
                    self._log("DRIFT",
                              f"Taker drift {drift_bps:.1f}bps > {config.max_taker_drift_bps}bps "
                              f"(base={baseline_mid:.4f} now={current_mid:.4f}) — cancelling maker")
                    drift_triggered.set()
                    return
                self._log("DRIFT", f"Drift monitor: mid={current_mid:.4f} drift={drift_bps:.1f}bps/{config.max_taker_drift_bps}bps — OK")
            except asyncio.CancelledError:
                return
            except Exception as exc:
                self._log("DRIFT", f"Drift monitor: poll error ({exc})", level="warn")
                logger.debug("Drift monitor poll error: %s", exc)

    async def _check_taker_fill(self, client, order_id: str | None, resp: dict) -> float:
        """Check taker IOC fill. WS-first with short timeout, REST as final fallback.

        IOC orders fill instantly, so WS events typically arrive within 100ms.
        """
        # Some exchanges return traded_qty directly in the IOC response
        if resp.get("traded_qty", 0) > 0:
            return float(resp["traded_qty"])

        oid = str(order_id) if order_id else None

        # For exchanges where IOC success = filled (e.g. NADO)
        # Retry up to 5× with 500ms delay — matching engine may not update traded_qty instantly
        if resp.get("status") in ("success", "FILLED", "CLOSED"):
            if oid:
                for attempt in range(5):
                    try:
                        fill = await client.async_check_order_fill(oid)
                        fill_qty = float(fill.get("traded_qty", 0))
                        if fill_qty > 0:
                            logger.info("Taker fill (Nado) attempt=%d order=%s qty=%.6f", attempt, oid, fill_qty)
                            return fill_qty
                    except Exception:
                        pass
                    await asyncio.sleep(0.5)
                logger.warning("Taker fill (Nado) order=%s: traded_qty still 0 after 5 attempts", oid)
            return 0.0

        if not oid:
            return 0.0

        # ── WS-first fill detection ──
        # Check immediately (event may have arrived during order placement)
        ws_qty = self._get_ws_filled_qty(oid)
        if ws_qty > 0:
            logger.info("Taker fill via WS (instant): order=%s qty=%.6f", oid, ws_qty)
            return ws_qty

        # Wait up to 2s for WS fill event in short intervals
        deadline = time.time() + 2.0
        while time.time() < deadline:
            self._fill_event.clear()
            try:
                remaining = max(deadline - time.time(), 0)
                await asyncio.wait_for(self._fill_event.wait(), timeout=min(0.1, remaining))
            except asyncio.TimeoutError:
                pass
            ws_qty = self._get_ws_filled_qty(oid)
            if ws_qty > 0:
                logger.info("Taker fill via WS (waited): order=%s qty=%.6f", oid, ws_qty)
                return ws_qty

        # ── REST fallback (only if WS didn't deliver) ──
        try:
            fill = await client.async_check_order_fill(oid)
            fill_qty = float(fill.get("traded_qty", 0))
            logger.info("Taker fill via REST fallback: order=%s status=%s traded_qty=%s", oid, fill.get("status"), fill_qty)
            return fill_qty
        except Exception as exc:
            logger.warning("Taker REST fill check error: order=%s error=%s", oid, exc)

        return 0.0

    # ── Market close for sub-minimum residual ────────────────────────

    async def _market_close_residual(
        self, config: MakerTakerConfig, qty: Decimal, chunk_index: int = -1,
    ) -> ChunkResult:
        """Close a sub-minimum residual via simultaneous IOC on both sides.

        Used when the remaining qty is too small for a maker post-only order
        (below exchange min order size). Fires aggressive IOC orders on both
        maker and taker exchanges concurrently with reduce_only=True.
        """
        chunk = ChunkResult(
            chunk_index=chunk_index,
            maker_exchange=config.maker_exchange,
            taker_exchange=config.taker_exchange,
            start_ts=time.time(),
        )
        self._log("EXIT", f"Market-close residual: {qty:.6f} on {config.maker_exchange}/{config.taker_exchange}")

        maker_client = self._clients.get(config.maker_exchange)
        taker_client = self._clients.get(config.taker_exchange)
        if not maker_client or not taker_client:
            chunk.error = f"Missing client for market close: maker={config.maker_exchange} taker={config.taker_exchange}"
            chunk.end_ts = time.time()
            return chunk

        async def _fire_ioc(client, exchange: str, symbol: str, side: str, amount: Decimal) -> tuple[float, str | None]:
            """Fire a single aggressive IOC order. Returns (filled_qty, error_or_None)."""
            try:
                book = await self._get_book(exchange, symbol, client)
                tick = await client.async_get_tick_size(symbol)
                if side == "buy":
                    if not book.get("asks"):
                        return 0.0, "No asks in orderbook"
                    best = Decimal(str(book["asks"][0][0]))
                    price = best + tick * 50
                else:
                    if not book.get("bids"):
                        return 0.0, "No bids in orderbook"
                    best = Decimal(str(book["bids"][0][0]))
                    price = best - tick * 50
                self._log("ORDER", f"Market-close IOC: {side.upper()} {amount} {symbol} @ {price} (best={best}) on {exchange}")
                resp = await client.async_create_ioc_order(
                    symbol=symbol, side=side, amount=amount,
                    price=price, reduce_only=True,
                )
                oid = resp.get("id") or resp.get("order_id") or resp.get("digest")
                filled = await self._check_taker_fill(client, oid, resp)
                return filled, None
            except Exception as exc:
                return 0.0, str(exc)

        # Fire both sides concurrently
        maker_task = _fire_ioc(maker_client, config.maker_exchange, config.maker_symbol, config.maker_side, qty)
        taker_task = _fire_ioc(taker_client, config.taker_exchange, config.taker_symbol, config.taker_side, qty)
        (maker_filled, maker_err), (taker_filled, taker_err) = await asyncio.gather(maker_task, taker_task)

        chunk.maker_filled_qty = maker_filled
        chunk.taker_filled_qty = taker_filled
        chunk.state = ChunkState.CHUNK_DONE
        chunk.end_ts = time.time()

        if maker_err:
            self._log("EXIT", f"Market-close maker error: {maker_err}", level="warn")
        if taker_err:
            self._log("EXIT", f"Market-close taker error: {taker_err}", level="warn")

        if maker_filled > 0 or taker_filled > 0:
            self._log("EXIT", f"Market-close result: maker={maker_filled:.6f} taker={taker_filled:.6f}")
        else:
            chunk.error = f"Market-close failed: maker={maker_err} taker={taker_err}"
            self._log("EXIT", f"Market-close FAILED on both sides", level="error")

        return chunk

    # ── Emergency unwind ──────────────────────────────────────────────

    async def _emergency_unwind(self, config: MakerTakerConfig, result: ExecutionResult) -> None:
        """Emergency: unwind maker fill when taker hedge fails."""
        unwind_qty = Decimal(str(result.total_maker_qty - result.total_taker_qty))
        if unwind_qty <= 0:
            return

        # Reverse the maker side to unwind
        unwind_side = "sell" if config.maker_side == "buy" else "buy"
        maker_client = self._clients.get(config.maker_exchange)

        logger.error(
            "EMERGENCY UNWIND: %s %s %.6f on %s",
            unwind_side.upper(), config.maker_symbol, unwind_qty, config.maker_exchange,
        )

        try:
            book = await maker_client.async_fetch_order_book(config.maker_symbol, limit=5)
            tick = await maker_client.async_get_tick_size(config.maker_symbol)

            if unwind_side == "sell":
                best = Decimal(str(book["bids"][0][0])) if book.get("bids") else Decimal("0")
                price = best - tick * 10  # Very aggressive
            else:
                best = Decimal(str(book["asks"][0][0])) if book.get("asks") else Decimal("0")
                price = best + tick * 10

            resp = await maker_client.async_create_ioc_order(
                symbol=config.maker_symbol,
                side=unwind_side,
                amount=unwind_qty,
                price=price,
            )
            logger.info("Emergency unwind response: %s", resp)
        except Exception as exc:
            logger.critical("EMERGENCY UNWIND FAILED: %s — MANUAL INTERVENTION REQUIRED", exc)

    # ── State transitions ─────────────────────────────────────────────

    def _transition(self, new_state: JobState) -> None:
        old = self._state
        self._state = new_state
        logger.info("State: %s → %s [%s]", old.value, new_state.value, time.strftime("%H:%M:%S.") + f"{time.time() % 1:.3f}"[2:])
