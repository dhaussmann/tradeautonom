"""Funding-Arb engine orchestrator.

Ties together: DataLayer → FundingMonitor → StateMachine → RiskManager.

Lifecycle:
  run()  → set leverage → enter position → start countdown timer
  stop() → cancel timer → exit position → cleanup
  manual_entry() / manual_exit() still available for fine-grained control.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.config import Settings
from app.data_layer import DataLayer
from app.execution_logger import ExecutionLogger
from app.funding_monitor import FundingMonitor, FundingSuggestion
from app.position_sizer import compute_position_size
from app.risk_manager import RiskManager
from app.state_machine import (
    ChunkResult,
    ExecutionResult,
    JobState,
    MakerTakerConfig,
    StateMachine,
)

logger = logging.getLogger("tradeautonom.engine")


@dataclass
class EngineConfig:
    """Per-job configuration for the Funding-Arb engine."""
    job_id: str = ""
    long_exchange: str = "extended"
    short_exchange: str = "grvt"
    maker_exchange: str = "extended"
    instrument_a: str = "SOL-USD"          # instrument on long exchange
    instrument_b: str = "SOL_USDT_Perp"    # instrument on short exchange
    quantity: Decimal = Decimal("20")

    # Maker execution
    maker_timeout_ms: int = 10000
    maker_reprice_ticks: int = 3
    maker_max_chase_rounds: int = 5
    maker_offset_ticks: int = 0

    # TWAP
    twap_num_chunks: int = 10
    twap_interval_s: float = 10.0

    # Risk
    delta_max_usd: float = 50.0
    circuit_breaker_loss_usd: float = 500.0
    min_spread_pct: float = -0.5
    max_spread_pct: float = 0.05
    max_chunk_spread_usd: float = 1.0

    # Funding monitor
    funding_poll_interval_s: float = 60.0

    # Run duration
    duration_h: int = 1
    duration_m: int = 0
    auto_entry: bool = True

    # Leverage (per exchange)
    leverage_long: int = 25
    leverage_short: int = 25

    # Simulation
    simulation: bool = False

    # Opt-in optimizations
    fn_opt_depth_spread: bool = False
    fn_opt_max_slippage_bps: float = 10.0
    fn_opt_ohi_monitoring: bool = False
    fn_opt_min_ohi: float = 0.4
    fn_opt_funding_history: bool = False
    fn_opt_funding_api_url: str = "https://api.fundingrate.de"
    fn_opt_min_funding_consistency: float = 0.3
    fn_opt_dynamic_sizing: bool = False
    fn_opt_max_utilization: float = 0.80
    fn_opt_max_per_pair_ratio: float = 0.25
    fn_opt_shared_monitor_url: str = ""
    fn_opt_taker_drift_guard: bool = False
    fn_opt_max_taker_drift_bps: float = 3.0

    # Execution log (AI training data)
    history_ingest_url: str = ""
    history_ingest_token: str = ""
    execution_log_enabled: bool = True

    @property
    def duration_total_s(self) -> float:
        """Total run duration in seconds. 0 = run indefinitely."""
        return (self.duration_h * 3600) + (self.duration_m * 60)

    @classmethod
    def from_settings(cls, settings: Settings, job_id: str = "default") -> EngineConfig:
        """Create config from app Settings."""
        return cls(
            job_id=job_id,
            long_exchange=settings.fn_long_exchange,
            short_exchange=settings.fn_short_exchange,
            maker_exchange=settings.fn_maker_exchange,
            instrument_a=settings.fn_instrument_a,
            instrument_b=settings.fn_instrument_b,
            quantity=Decimal(str(settings.fn_quantity)),
            maker_timeout_ms=settings.fn_maker_timeout_ms,
            maker_reprice_ticks=settings.fn_maker_reprice_ticks,
            maker_max_chase_rounds=settings.fn_maker_max_chase_rounds,
            maker_offset_ticks=settings.fn_maker_offset_ticks,
            twap_num_chunks=settings.fn_twap_num_chunks,
            twap_interval_s=settings.fn_twap_interval_s,
            delta_max_usd=settings.fn_delta_max_usd,
            circuit_breaker_loss_usd=settings.fn_circuit_breaker_loss_usd,
            min_spread_pct=settings.fn_min_spread_pct,
            max_spread_pct=settings.fn_max_spread_pct,
            max_chunk_spread_usd=settings.fn_max_chunk_spread_usd,
            funding_poll_interval_s=settings.fn_funding_poll_interval_s,
            duration_h=settings.fn_duration_h,
            duration_m=settings.fn_duration_m,
            auto_entry=settings.fn_auto_entry,
            leverage_long=settings.fn_leverage_long,
            leverage_short=settings.fn_leverage_short,
            simulation=settings.fn_simulation_mode,
            fn_opt_depth_spread=settings.fn_opt_depth_spread,
            fn_opt_max_slippage_bps=settings.fn_opt_max_slippage_bps,
            fn_opt_ohi_monitoring=settings.fn_opt_ohi_monitoring,
            fn_opt_min_ohi=settings.fn_opt_min_ohi,
            fn_opt_funding_history=settings.fn_opt_funding_history,
            fn_opt_funding_api_url=settings.fn_opt_funding_api_url,
            fn_opt_min_funding_consistency=settings.fn_opt_min_funding_consistency,
            fn_opt_dynamic_sizing=settings.fn_opt_dynamic_sizing,
            fn_opt_max_utilization=settings.fn_opt_max_utilization,
            fn_opt_max_per_pair_ratio=settings.fn_opt_max_per_pair_ratio,
            fn_opt_shared_monitor_url=settings.fn_opt_shared_monitor_url,
            fn_opt_taker_drift_guard=settings.fn_opt_taker_drift_guard,
            fn_opt_max_taker_drift_bps=settings.fn_opt_max_taker_drift_bps,
            history_ingest_url=settings.history_ingest_url,
            history_ingest_token=settings.history_ingest_token,
            execution_log_enabled=settings.execution_log_enabled,
        )


class FundingArbEngine:
    """Orchestrator for a single funding-arb job.

    Owns: DataLayer, FundingMonitor, StateMachine, RiskManager.

    Lifecycle:
      run()  → set leverage → enter position → start countdown
      graceful_stop() → cancel timer → exit position → cleanup
    """

    def __init__(
        self,
        config: EngineConfig,
        clients: dict[str, Any],
        activity_forwarder: Any | None = None,
    ) -> None:
        self.config = config
        self._clients = clients
        self._activity_forwarder = activity_forwarder

        # Determine which symbol maps to which exchange
        self._symbols_map = {
            config.long_exchange: config.instrument_a,
            config.short_exchange: config.instrument_b,
        }
        # Handle case where long and short are on same exchange (different symbols)
        if config.long_exchange == config.short_exchange:
            logger.warning("Long and short on same exchange — unusual but supported")

        # Sub-components (initialized in start())
        self._data_layer: DataLayer | None = None
        self._funding_monitor: FundingMonitor | None = None
        self._state_machine: StateMachine | None = None
        self._risk_manager: RiskManager | None = None
        self._execution_logger: ExecutionLogger | None = None

        self._started = False
        self._trade_log: list[dict] = []

        # Real-time activity log (ring buffer)
        self._activity_log: collections.deque[dict] = collections.deque(maxlen=300)
        self._activity_seq: int = 0

        # Run / timer state
        self._is_running = False
        self._countdown_task: asyncio.Task | None = None
        self._execution_task: asyncio.Task | None = None  # background entry/exit TWAP task
        self._started_at: float | None = None   # epoch when run() was called
        self._expires_at: float | None = None    # epoch when auto-exit triggers (None = indefinite)
        self._stop_reason: str | None = None     # why the run ended

    # ── Timer persistence ─────────────────────────────────────────────

    @property
    def _timer_file(self) -> Path:
        if self.config.job_id:
            return Path(f"data/bots/{self.config.job_id}/timer.json")
        return Path("data/fn_timer.json")

    def _save_timer(self) -> None:
        """Persist timer state to disk so it survives container restarts."""
        data = {
            "is_running": self._is_running,
            "started_at": self._started_at,
            "expires_at": self._expires_at,
        }
        try:
            self._timer_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self._timer_file, "w") as fh:
                json.dump(data, fh, indent=2)
            logger.debug("Saved timer state to %s", self._timer_file)
        except Exception as exc:
            logger.warning("Failed to save timer state: %s", exc)

    def _load_timer(self) -> None:
        """Restore timer state from disk after a container restart.

        Called in start() after load_state() returns True (position is HOLDING).
        If expires_at is still in the future, resumes the countdown.
        If expired, schedules immediate graceful_stop.
        """
        if not self._timer_file.exists():
            return
        try:
            with open(self._timer_file) as fh:
                data = json.load(fh)
            was_running = data.get("is_running", False)
            started_at = data.get("started_at")
            expires_at = data.get("expires_at")
            if not was_running:
                logger.debug("Timer was not running — not restoring")
                return
            self._started_at = started_at
            self._is_running = True
            now = time.time()
            if expires_at is not None:
                remaining = expires_at - now
                if remaining > 0:
                    self._expires_at = expires_at
                    self._countdown_task = asyncio.create_task(
                        self._duration_countdown(remaining),
                        name=f"fn-countdown-{self.config.job_id}",
                    )
                    self.log_activity("ENGINE", f"Timer restored: {remaining:.0f}s remaining")
                    logger.info("Timer restored: %.0fs remaining (expires_at=%.0f)", remaining, expires_at)
                else:
                    self._expires_at = expires_at
                    self.log_activity("ENGINE", "Timer expired during downtime — scheduling exit", level="warn")
                    logger.warning("Timer expired during downtime (%.0fs ago) — scheduling exit", -remaining)
                    asyncio.create_task(self.graceful_stop(reason="duration_expired"))
            else:
                # Was running indefinitely
                self._expires_at = None
                self.log_activity("ENGINE", "Resumed: running indefinitely (no timer)")
                logger.info("Timer restored: running indefinitely")
        except Exception as exc:
            logger.warning("Failed to load timer state: %s", exc)

    async def adjust_timer(self, duration_h: int | None = None, duration_m: int | None = None) -> dict:
        """Adjust the countdown timer on a running bot.

        Computes new expires_at from now + new duration.
        If both h and m are 0, sets indefinite (no auto-exit).
        """
        is_holding = (self._state_machine and self._state_machine.state == JobState.HOLDING)
        if not self._is_running and not is_holding:
            raise RuntimeError("Bot is not running — cannot adjust timer")

        h = duration_h if duration_h is not None else 0
        m = duration_m if duration_m is not None else 0
        total_s = (h * 3600) + (m * 60)

        # If bot is HOLDING but not marked as running, mark it running so timer is visible
        if not self._is_running and is_holding:
            self._is_running = True
            if not self._started_at:
                self._started_at = time.time()

        # Cancel existing countdown
        if self._countdown_task and not self._countdown_task.done():
            self._countdown_task.cancel()
            try:
                await self._countdown_task
            except asyncio.CancelledError:
                pass
            self._countdown_task = None

        now = time.time()
        if total_s > 0:
            self._expires_at = now + total_s
            self._countdown_task = asyncio.create_task(
                self._duration_countdown(total_s),
                name=f"fn-countdown-{self.config.job_id}",
            )
            self.log_activity("ENGINE", f"Timer adjusted: {h}h{m}m ({total_s:.0f}s from now)")
            logger.info("Timer adjusted: %dh%dm (%.0fs), new expires_at=%.0f", h, m, total_s, self._expires_at)
        else:
            self._expires_at = None
            self.log_activity("ENGINE", "Timer removed — running indefinitely")
            logger.info("Timer removed — running indefinitely")

        self._save_timer()
        remaining_s = max(0, self._expires_at - now) if self._expires_at else None
        return {"success": True, "remaining_s": remaining_s, "expires_at": self._expires_at}

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self, data_layer: DataLayer | None = None) -> None:
        """Initialize and start all sub-components.

        Optionally accepts a shared DataLayer (for multi-job setups).
        """
        if self._started:
            return

        # DataLayer
        if data_layer:
            self._data_layer = data_layer
        else:
            self._data_layer = DataLayer(shared_monitor_url=self.config.fn_opt_shared_monitor_url)
            await self._data_layer.start(self._clients, self._symbols_map)

        # FundingMonitor
        self._funding_monitor = FundingMonitor(
            data_layer=self._data_layer,
            exchange_a=self.config.long_exchange,
            symbol_a=self.config.instrument_a,
            exchange_b=self.config.short_exchange,
            symbol_b=self.config.instrument_b,
            poll_interval_s=self.config.funding_poll_interval_s,
            v4_enabled=self.config.fn_opt_funding_history,
            v4_api_url=self.config.fn_opt_funding_api_url,
            v4_min_consistency=self.config.fn_opt_min_funding_consistency,
        )
        await self._funding_monitor.start()

        # ExecutionLogger (AI training data)
        if self.config.execution_log_enabled and self.config.history_ingest_url and self.config.history_ingest_token:
            self._execution_logger = ExecutionLogger(
                ingest_url=self.config.history_ingest_url,
                ingest_token=self.config.history_ingest_token,
                bot_id=self.config.job_id,
                enabled=True,
            )
            self._execution_logger.start()
            self.log_activity("INFO", "Execution logger active (AI training data)")

        # StateMachine
        self._state_machine = StateMachine(
            clients=self._clients,
            data_layer=self._data_layer,
            activity_log_fn=self.log_activity,
            bot_id=self.config.job_id,
            execution_logger=self._execution_logger,
            funding_monitor=self._funding_monitor,
        )
        # Restore persisted position state (survives container restarts)
        if self._state_machine.load_state():
            self.log_activity("ENGINE", f"Restored position from disk: long={self._state_machine.position_info['long_qty']:.6f} short={self._state_machine.position_info['short_qty']:.6f}")
            # Overwrite disk values with actual exchange positions
            await self._state_machine.sync_position_from_exchange()
            self.log_activity("ENGINE", f"Synced from exchange: long={self._state_machine.position_info['long_qty']:.6f} short={self._state_machine.position_info['short_qty']:.6f}")
            # Restore timer state (resume countdown if it was running)
            self._load_timer()
        # Start WS fill subscriptions for real-time fill monitoring
        await self._state_machine.start_fill_subscriptions(self._symbols_map)

        # RiskManager
        self._risk_manager = RiskManager(
            data_layer=self._data_layer,
            clients=self._clients,
            delta_max_usd=self.config.delta_max_usd,
            circuit_breaker_loss_usd=self.config.circuit_breaker_loss_usd,
            max_spread_pct=self.config.max_spread_pct,
        )
        await self._risk_manager.start()

        self._started = True

        # ── Activity Log: OMS status ──
        if self.config.fn_opt_shared_monitor_url:
            oms_mode = "WS real-time" if getattr(self._data_layer, "_oms_ws_active", False) or getattr(self._data_layer, "_oms_ws_task", None) else "HTTP poll"
            self.log_activity("INFO", f"Data feeds active — OMS: {self.config.fn_opt_shared_monitor_url} ({oms_mode}) | Feeds: {len(self._symbols_map)} symbols")
        else:
            self.log_activity("INFO", f"Data feeds active — Direct WS (no OMS) | Feeds: {len(self._symbols_map)} symbols")

        # ── Activity Log: active features summary ──
        features = []
        if self.config.fn_opt_depth_spread:
            features.append(f"Depth Spread (max {self.config.fn_opt_max_slippage_bps}bps)")
        if self.config.fn_opt_taker_drift_guard:
            features.append(f"Taker Drift Guard (max {self.config.fn_opt_max_taker_drift_bps}bps)")
        if self.config.fn_opt_ohi_monitoring:
            features.append(f"OHI Monitoring (min {self.config.fn_opt_min_ohi})")
        if self.config.fn_opt_funding_history:
            features.append(f"V4 Funding History (min consistency {self.config.fn_opt_min_funding_consistency})")
        if self.config.fn_opt_dynamic_sizing:
            features.append(f"Dynamic Sizing ({self.config.fn_opt_max_utilization*100:.0f}%/{self.config.fn_opt_max_per_pair_ratio*100:.0f}%)")
        if features:
            self.log_activity("INFO", f"Active features: {' | '.join(features)}")
        else:
            self.log_activity("INFO", "Active features: none (all opt-in features disabled)")

        logger.info(
            "FundingArbEngine started: job=%s long=%s:%s short=%s:%s maker=%s",
            self.config.job_id,
            self.config.long_exchange, self.config.instrument_a,
            self.config.short_exchange, self.config.instrument_b,
            self.config.maker_exchange,
        )

    async def stop(self) -> None:
        """Stop all sub-components (infrastructure teardown)."""
        # Cancel countdown if running
        if self._countdown_task and not self._countdown_task.done():
            self._countdown_task.cancel()
            try:
                await self._countdown_task
            except asyncio.CancelledError:
                pass
            self._countdown_task = None
        self._is_running = False

        if self._execution_logger:
            await self._execution_logger.stop()
        if self._state_machine:
            await self._state_machine.stop_fill_subscriptions()
        if self._risk_manager:
            await self._risk_manager.stop()
        if self._funding_monitor:
            await self._funding_monitor.stop()
        if self._data_layer:
            await self._data_layer.stop()
        self._started = False
        logger.info("FundingArbEngine stopped: job=%s", self.config.job_id)

    # ── Run / Stop (auto-entry + countdown) ───────────────────────────

    async def run(
        self,
        duration_h: int | None = None,
        duration_m: int | None = None,
        leverage_long: int | None = None,
        leverage_short: int | None = None,
        quantity: Decimal | None = None,
        long_exchange: str | None = None,
        short_exchange: str | None = None,
        instrument_a: str | None = None,
        instrument_b: str | None = None,
    ) -> ExecutionResult:
        """Start the bot: set leverage → enter position → start countdown.

        All parameters are optional overrides over EngineConfig defaults.
        Returns the entry ExecutionResult.
        """
        if not self._started:
            raise RuntimeError("Engine infrastructure not started — call start() first")
        if self._is_running:
            raise RuntimeError("Bot is already running")

        lev_long = leverage_long if leverage_long is not None else self.config.leverage_long
        lev_short = leverage_short if leverage_short is not None else self.config.leverage_short
        dur_h = duration_h if duration_h is not None else self.config.duration_h
        dur_m = duration_m if duration_m is not None else self.config.duration_m
        total_s = (dur_h * 3600) + (dur_m * 60)

        long_exch = long_exchange or self.config.long_exchange
        short_exch = short_exchange or self.config.short_exchange

        # Apply instrument overrides (persisted on config for the duration of this run)
        if instrument_a:
            self.config.instrument_a = instrument_a
            self._symbols_map[long_exch] = instrument_a
            logger.info("Instrument A overridden: %s", instrument_a)
        if instrument_b:
            self.config.instrument_b = instrument_b
            self._symbols_map[short_exch] = instrument_b
            logger.info("Instrument B overridden: %s", instrument_b)

        # Step 0a: Verify exchange credentials (e.g. Variational JWT not expired)
        for exch_name in (long_exch, short_exch):
            client = self._clients.get(exch_name)
            if client and hasattr(client, "async_check_auth"):
                self.log_activity("INFO", f"Credential check: {exch_name} ...")
                try:
                    await client.async_check_auth()
                    self.log_activity("INFO", f"Credential check: {exch_name} OK")
                except Exception as exc:
                    raise RuntimeError(f"{exch_name} credential check failed — token may be expired: {exc}")

        # Step 0b: Check no existing positions on either exchange
        self.log_activity("INFO", f"Position check: verifying no existing positions on {long_exch} / {short_exch}")
        await self._check_no_existing_positions([
            (long_exch, self._get_symbol(long_exch)),
            (short_exch, self._get_symbol(short_exch)),
        ])
        self.log_activity("INFO", f"Position check: no existing positions found — OK")

        # Step 1: Set leverage on both exchanges
        self.log_activity("ENGINE", f"Setting leverage: {long_exch} {lev_long}x, {short_exch} {lev_short}x")
        await self._set_leverage(long_exch, self._get_symbol(long_exch), lev_long)
        await self._set_leverage(short_exch, self._get_symbol(short_exch), lev_short)
        self.log_activity("ENGINE", "Leverage set successfully")

        # Step 2: Enter position
        self.log_activity("ENGINE", f"Starting bot: long={long_exch} short={short_exch} qty={quantity or self.config.quantity}")
        self._is_running = True
        self._stop_reason = None

        entry_result = await self.manual_entry(
            long_exchange=long_exch,
            short_exchange=short_exch,
            quantity=quantity,
        )

        if not entry_result.success:
            self._is_running = False
            self._started_at = None
            self._expires_at = None
            self._stop_reason = f"Entry failed: {entry_result.error}"
            self.log_activity("ENGINE", f"Entry FAILED: {entry_result.error}", level="error")
            logger.error("run() entry failed: %s", entry_result.error)
            return entry_result

        # Step 3: Start countdown timer AFTER entry completes (if duration > 0)
        self._started_at = time.time()
        if total_s > 0:
            self._expires_at = self._started_at + total_s
            self._countdown_task = asyncio.create_task(
                self._duration_countdown(total_s),
                name=f"fn-countdown-{self.config.job_id}",
            )
            self.log_activity("ENGINE", f"Timer started: {dur_h}h{dur_m}m ({total_s:.0f}s)")
            logger.info(
                "Bot running: duration=%dh%dm (%.0fs), expires_at=%.0f",
                dur_h, dur_m, total_s, self._expires_at,
            )
        else:
            self._expires_at = None
            logger.info("Bot running: no time limit (run indefinitely)")

        self._save_timer()
        return entry_result

    async def graceful_stop(self, reason: str = "manual") -> ExecutionResult | None:
        """Stop the bot: cancel countdown → exit position.

        Uses the same Maker-Taker TWAP exit mechanism.
        Returns the exit ExecutionResult, or None if no position to close.
        Also works when bot is not "running" but state machine is HOLDING
        (e.g. after a restart with persisted position).
        """
        is_holding = (self._state_machine and self._state_machine.state == JobState.HOLDING)
        if not self._is_running and not is_holding:
            raise RuntimeError("Bot is not running and has no position to close")

        self.log_activity("ENGINE", f"Stopping bot: reason={reason}")
        logger.info("graceful_stop() triggered: reason=%s", reason)

        # Cancel countdown timer (skip if we ARE the countdown task — avoid self-cancel)
        if self._countdown_task and not self._countdown_task.done():
            if asyncio.current_task() is not self._countdown_task:
                self._countdown_task.cancel()
                try:
                    await self._countdown_task
                except asyncio.CancelledError:
                    pass
            self._countdown_task = None

        # Exit position if in HOLDING state
        exit_result = None
        if self._state_machine and self._state_machine.state == JobState.HOLDING:
            exit_result = await self.manual_exit()
        else:
            logger.warning("graceful_stop: state is %s, no exit needed",
                           self._state_machine.state.value if self._state_machine else "?")

        self._is_running = False
        self._stop_reason = reason
        self._expires_at = None
        self._save_timer()
        self.log_activity("ENGINE", f"Bot stopped: {reason}")
        logger.info("Bot stopped: reason=%s", reason)
        return exit_result

    async def _duration_countdown(self, total_s: float) -> None:
        """Sleep for the configured duration, then trigger automatic exit."""
        try:
            logger.info("Countdown started: %.0fs", total_s)
            await asyncio.sleep(total_s)
            logger.info("Countdown expired — triggering automatic exit")
            await self.graceful_stop(reason="duration_expired")
        except asyncio.CancelledError:
            logger.info("Countdown cancelled")
            raise

    def pause_execution(self) -> dict:
        """Pause the running TWAP execution and the countdown timer.

        The TWAP chunk loop will block at the next safe point until resumed.
        The countdown timer is suspended (remaining time saved).
        """
        if not self._state_machine:
            raise RuntimeError("Engine not started")

        sm_state = self._state_machine.state
        if sm_state not in (JobState.ENTERING, JobState.EXITING):
            raise RuntimeError(f"Cannot pause: state is {sm_state.value}")

        # Pause state machine (TWAP execution)
        self._state_machine.pause()

        # Pause countdown timer: save remaining time, cancel task
        self._paused_timer_remaining: float | None = None
        if self._expires_at:
            self._paused_timer_remaining = max(0, self._expires_at - time.time())
            self._expires_at = None  # clear so UI shows paused, not counting down
        if self._countdown_task and not self._countdown_task.done():
            self._countdown_task.cancel()
            self._countdown_task = None

        self._save_timer()
        self.log_activity("ENGINE", f"PAUSED: execution + timer (remaining={self._paused_timer_remaining:.0f}s)" if self._paused_timer_remaining else "PAUSED: execution (no timer)")
        return {"status": "ok", "paused": True}

    def resume_execution(self) -> dict:
        """Resume a paused TWAP execution and restart the countdown timer."""
        if not self._state_machine:
            raise RuntimeError("Engine not started")

        if not self._state_machine.is_paused:
            raise RuntimeError(f"Cannot resume: state is {self._state_machine.state.value}")

        # Resume state machine
        self._state_machine.resume()

        # Resume countdown timer with saved remaining time
        remaining = getattr(self, "_paused_timer_remaining", None)
        if remaining and remaining > 0:
            self._expires_at = time.time() + remaining
            self._countdown_task = asyncio.create_task(
                self._duration_countdown(remaining),
                name=f"fn-countdown-{self.config.job_id}",
            )
            self.log_activity("ENGINE", f"RESUMED: execution + timer ({remaining:.0f}s remaining)")
        else:
            self.log_activity("ENGINE", "RESUMED: execution (no timer)")
        self._paused_timer_remaining = None

        self._save_timer()
        return {"status": "ok", "paused": False}

    async def force_kill(self) -> dict:
        """Hard kill: cancel background tasks, cancel open orders, reset to IDLE.

        No exit trades — positions stay on exchanges as-is.
        Acts as an emergency stop: immediately cancels the running asyncio Task
        so no further orders can be placed.
        """
        self.log_activity("ENGINE", "KILL triggered — cancelling orders and resetting")
        logger.warning("force_kill() triggered")

        # 1. Cancel countdown timer
        if self._countdown_task and not self._countdown_task.done():
            self._countdown_task.cancel()
            try:
                await self._countdown_task
            except (asyncio.CancelledError, Exception):
                pass
            self._countdown_task = None

        # 2. Hard-cancel the background execution task (entry or exit TWAP)
        #    This immediately interrupts any asyncio.sleep / async I/O in the
        #    TWAP chunk loop, spread guard, maker fill wait, etc.
        if self._execution_task and not self._execution_task.done():
            self._execution_task.cancel()
            try:
                await self._execution_task
            except (asyncio.CancelledError, Exception):
                pass
            self._execution_task = None
            logger.warning("force_kill: execution task cancelled")

        # 3. Force state machine to IDLE (in case abort_execution left it elsewhere)
        if self._state_machine:
            self._state_machine.reset()

        # 4. Best-effort: cancel all open orders on all exchanges
        cancelled = []
        for exch_name, client in self._clients.items():
            if hasattr(client, "async_cancel_all_orders"):
                try:
                    await client.async_cancel_all_orders()
                    cancelled.append(exch_name)
                    logger.info("force_kill: cancelled all orders on %s", exch_name)
                except Exception as exc:
                    logger.warning("force_kill: cancel orders on %s failed: %s", exch_name, exc)

        # 5. Reset engine state
        self._is_running = False
        self._stop_reason = "killed"
        self._started_at = None
        self._expires_at = None
        self._save_timer()

        # 6. Stop fill subscriptions
        if self._state_machine:
            await self._state_machine.stop_fill_subscriptions()

        self.log_activity("ENGINE", f"KILLED — orders cancelled on {cancelled}, state reset to IDLE")
        logger.warning("force_kill() complete: cancelled=%s", cancelled)
        return {"status": "ok", "cancelled_exchanges": cancelled}

    async def _check_no_existing_positions(
        self, exchanges_symbols: list[tuple[str, str]],
    ) -> None:
        """Raise RuntimeError if any position already exists on the given exchanges.

        Called before entry to prevent opening a second position on the same token.
        """
        for exchange, symbol in exchanges_symbols:
            client = self._clients.get(exchange)
            if not client or not hasattr(client, "async_fetch_positions"):
                continue
            try:
                positions = await client.async_fetch_positions([symbol])
                for p in positions:
                    p_inst = p.get("instrument", p.get("symbol", ""))
                    p_size = abs(float(p.get("size", 0)))
                    if p_size < 0.001:
                        continue
                    # Exact match or underlying match
                    matched = p_inst == symbol
                    if not matched:
                        try:
                            p_parts = p_inst.split("-")
                            s_parts = symbol.split("-")
                            matched = len(p_parts) >= 2 and len(s_parts) >= 2 and p_parts[1].upper() == s_parts[1].upper()
                        except Exception:
                            pass
                    if matched:
                        raise RuntimeError(
                            f"Cannot start: existing position on {exchange} for {p_inst} "
                            f"(size={p_size}). Please close it first."
                        )
            except RuntimeError:
                raise  # re-raise our own error
            except Exception as exc:
                logger.warning("Position pre-check failed for %s:%s: %s", exchange, symbol, exc)

    async def _set_leverage(
        self, exchange: str, symbol: str, leverage: int,
    ) -> None:
        """Set leverage on a specific exchange. Logs warning on failure."""
        client = self._clients.get(exchange)
        if client is None:
            logger.warning("_set_leverage: no client for exchange '%s'", exchange)
            return

        if not hasattr(client, "async_set_leverage"):
            logger.warning("_set_leverage: client '%s' has no async_set_leverage method", exchange)
            return

        try:
            ok = await client.async_set_leverage(symbol, leverage)
            if ok:
                logger.info("Leverage set: %s:%s -> %dx", exchange, symbol, leverage)
            else:
                logger.warning("Leverage set returned False: %s:%s -> %dx", exchange, symbol, leverage)
        except Exception as exc:
            logger.warning("_set_leverage error on %s:%s: %s", exchange, symbol, exc)

    # ── Manual Entry/Exit ─────────────────────────────────────────────

    async def manual_entry(
        self,
        long_exchange: str | None = None,
        short_exchange: str | None = None,
        quantity: Decimal | None = None,
    ) -> ExecutionResult:
        """Manually trigger an entry.

        Optional overrides for long/short exchange and quantity.
        """
        if not self._started:
            raise RuntimeError("Engine not started")

        long_exch = long_exchange or self.config.long_exchange
        short_exch = short_exchange or self.config.short_exchange
        qty = quantity or self.config.quantity
        maker_exch = self.config.maker_exchange

        # Determine taker exchange
        taker_exch = short_exch if maker_exch == long_exch else long_exch

        # Determine sides
        # Maker exchange gets the side corresponding to its role
        if maker_exch == long_exch:
            maker_side = "buy"
            taker_side = "sell"
            maker_symbol = self._get_symbol(long_exch)
            taker_symbol = self._get_symbol(short_exch)
        else:
            maker_side = "sell"
            taker_side = "buy"
            maker_symbol = self._get_symbol(short_exch)
            taker_symbol = self._get_symbol(long_exch)

        # Pre-check: no existing positions on either exchange
        self.log_activity("INFO", f"Position check: verifying no existing positions on {long_exch} / {short_exch}")
        await self._check_no_existing_positions([
            (long_exch, self._get_symbol(long_exch)),
            (short_exch, self._get_symbol(short_exch)),
        ])
        self.log_activity("INFO", f"Position check: no existing positions — OK")

        # Pre-trade checks (use chunk_qty — TWAP only needs one chunk of liquidity at a time)
        # Auto-reduce num_chunks so each chunk meets the min order size on both exchanges
        num_chunks = max(self.config.twap_num_chunks, 1)
        for exch, sym in [(maker_exch, maker_symbol), (taker_exch, taker_symbol)]:
            client = self._clients.get(exch)
            if client and hasattr(client, "async_get_min_order_size"):
                min_sz = await client.async_get_min_order_size(sym)
                if min_sz and min_sz > 0:
                    max_chunks = int(qty / min_sz)
                    if max_chunks < num_chunks:
                        self.log_activity("INFO", f"Auto-reducing chunks {num_chunks} -> {max(max_chunks, 1)} (min_order_size={min_sz:.4f} on {exch})")
                        logger.info("Auto-reducing num_chunks %d → %d (min_order_size=%.4f on %s)", num_chunks, max(max_chunks, 1), min_sz, exch)
                        num_chunks = max(max_chunks, 1)
        if num_chunks != self.config.twap_num_chunks:
            self.config.twap_num_chunks = num_chunks
        chunk_qty = qty / Decimal(str(num_chunks))

        # Pre-trade risk check: maker
        ok, reason = await self._risk_manager.pre_trade_check(maker_exch, maker_symbol, maker_side, chunk_qty)
        if ok:
            ob_m = self._data_layer.get_orderbook(maker_exch, maker_symbol)
            m_levels = ob_m.asks if maker_side == "buy" else ob_m.bids
            m_liq = sum(float(lv[1]) for lv in m_levels[:10]) if m_levels else 0
            self.log_activity("INFO", f"Pre-trade check {maker_exch}:{maker_symbol} {maker_side.upper()} {chunk_qty} — OK (liquidity={m_liq:.2f}, updates={ob_m.update_count})")
        else:
            raise RuntimeError(f"Pre-trade check failed: {reason}")

        # Pre-trade risk check: taker
        ok, reason = await self._risk_manager.pre_trade_check(taker_exch, taker_symbol, taker_side, chunk_qty)
        if ok:
            ob_t = self._data_layer.get_orderbook(taker_exch, taker_symbol)
            t_levels = ob_t.asks if taker_side == "buy" else ob_t.bids
            t_liq = sum(float(lv[1]) for lv in t_levels[:10]) if t_levels else 0
            self.log_activity("INFO", f"Pre-trade check {taker_exch}:{taker_symbol} {taker_side.upper()} {chunk_qty} — OK (liquidity={t_liq:.2f}, updates={ob_t.update_count})")
        else:
            raise RuntimeError(f"Pre-trade check failed: {reason}")

        # Spread check (log only, do not block entry)
        ok, spread_pct, reason = self._risk_manager.check_spread(
            long_exch, self._get_symbol(long_exch),
            short_exch, self._get_symbol(short_exch),
        )
        self.log_activity("INFO", f"Spread check: {spread_pct:.4f}% — {'OK' if ok else reason}")
        if not ok:
            logger.warning("Spread check warning (not blocking): %s", reason)

        # OHI check (if enabled)
        if self.config.fn_opt_ohi_monitoring:
            for ohi_exch, ohi_sym in [(long_exch, self._get_symbol(long_exch)), (short_exch, self._get_symbol(short_exch))]:
                try:
                    ohi_data = self._data_layer.get_orderbook_health(ohi_exch, ohi_sym)
                    ohi_val = ohi_data.get("ohi", 0) if ohi_data else 0
                    ohi_detail = ""
                    if ohi_data:
                        parts = []
                        for k in ("spread_score", "depth_score", "symmetry_score"):
                            if k in ohi_data:
                                parts.append(f"{k.replace('_score','')}={ohi_data[k]:.2f}")
                        if parts:
                            ohi_detail = f" ({', '.join(parts)})"
                    passed = ohi_val >= self.config.fn_opt_min_ohi
                    self.log_activity("INFO", f"OHI {ohi_exch}:{ohi_sym} = {ohi_val:.2f}{ohi_detail} — {'OK' if passed else 'BELOW threshold'} (min {self.config.fn_opt_min_ohi})")
                except Exception as ohi_exc:
                    self.log_activity("INFO", f"OHI {ohi_exch}:{ohi_sym} — unavailable ({ohi_exc})", level="warn")

        # V4 Funding History check (if enabled)
        if self.config.fn_opt_funding_history and self._funding_monitor:
            try:
                v4 = self._funding_monitor.get_v4_data()
                v4_consistent = self._funding_monitor.is_v4_consistent()
                if v4:
                    self.log_activity("INFO",
                        f"V4 Funding: consistency={v4.get('spread_consistency', 0):.2f}, "
                        f"confidence={v4.get('confidence_score', 0)}, "
                        f"spread_apr={v4.get('spread_apr', 0):.4f}, "
                        f"pair_found={v4.get('pair_found', False)} — "
                        f"{'OK' if v4_consistent else 'BELOW threshold'} (min {self.config.fn_opt_min_funding_consistency})")
                else:
                    self.log_activity("INFO", "V4 Funding: no data available yet (poll pending)", level="warn")
            except Exception as v4_exc:
                self.log_activity("INFO", f"V4 Funding: error fetching data ({v4_exc})", level="warn")

        # Dynamic position sizing (opt-in)
        if self.config.fn_opt_dynamic_sizing and not quantity:
            try:
                long_snap = self._data_layer.get_orderbook(long_exch, self._get_symbol(long_exch))
                short_snap = self._data_layer.get_orderbook(short_exch, self._get_symbol(short_exch))
                long_book = {"asks": long_snap.asks, "bids": long_snap.bids}
                short_book = {"asks": short_snap.asks, "bids": short_snap.bids}
                # Get mark price from mid of long book
                mark_price = 0.0
                if long_snap.asks and long_snap.bids:
                    mark_price = (float(long_snap.asks[0][0]) + float(long_snap.bids[0][0])) / 2.0
                if mark_price > 0:
                    # Estimate collateral from both exchanges
                    collateral = 0.0
                    for exch in (long_exch, short_exch):
                        client = self._clients.get(exch)
                        if client and hasattr(client, "async_fetch_balance"):
                            try:
                                bal = await client.async_fetch_balance()
                                collateral += float(bal.get("total", bal.get("equity", 0)))
                            except Exception:
                                pass
                    if collateral > 0:
                        leverage = max(self.config.leverage_long, self.config.leverage_short)
                        sizing = compute_position_size(
                            collateral_usd=collateral,
                            leverage=leverage,
                            max_utilization=self.config.fn_opt_max_utilization,
                            max_per_pair_ratio=self.config.fn_opt_max_per_pair_ratio,
                            mark_price=mark_price,
                            long_book=long_book,
                            short_book=short_book,
                            max_slippage_bps=self.config.fn_opt_max_slippage_bps,
                        )
                        old_qty = qty
                        qty = min(qty, sizing.recommended_qty)
                        self.log_activity("SIZING", f"Dynamic sizing: {sizing.reason} (was {old_qty}, now {qty})")
                    else:
                        self.log_activity("SIZING", "Dynamic sizing skipped: could not fetch collateral", level="warn")
                else:
                    self.log_activity("SIZING", "Dynamic sizing skipped: no mark price", level="warn")
            except Exception as sizing_exc:
                self.log_activity("SIZING", f"Dynamic sizing error: {sizing_exc}", level="warn")

        config = MakerTakerConfig(
            maker_exchange=maker_exch,
            taker_exchange=taker_exch,
            maker_symbol=maker_symbol,
            taker_symbol=taker_symbol,
            maker_side=maker_side,
            taker_side=taker_side,
            total_qty=qty,
            num_chunks=self.config.twap_num_chunks,
            chunk_interval_s=self.config.twap_interval_s,
            maker_timeout_ms=self.config.maker_timeout_ms,
            maker_reprice_ticks=self.config.maker_reprice_ticks,
            maker_max_chase_rounds=self.config.maker_max_chase_rounds,
            maker_offset_ticks=self.config.maker_offset_ticks,
            simulation=self.config.simulation,
            max_chunk_spread_usd=self.config.max_chunk_spread_usd,
            min_spread_pct=self.config.min_spread_pct,
            max_spread_pct=self.config.max_spread_pct,
            use_depth_spread=self.config.fn_opt_depth_spread,
            max_slippage_bps=self.config.fn_opt_max_slippage_bps,
            taker_drift_guard=self.config.fn_opt_taker_drift_guard,
            max_taker_drift_bps=self.config.fn_opt_max_taker_drift_bps,
        )

        self.log_activity("ENTRY", f"Maker={maker_exch} {maker_side} {maker_symbol}, Taker={taker_exch} {taker_side} {taker_symbol}, qty={qty}, chunks={config.num_chunks}")
        logger.info(
            "Manual ENTRY: maker=%s(%s %s) taker=%s(%s %s) qty=%s chunks=%d spread=%.4f%%",
            maker_exch, maker_side, maker_symbol,
            taker_exch, taker_side, taker_symbol,
            qty, config.num_chunks, spread_pct,
        )

        result = await self._state_machine.execute_entry(config)
        if result.success:
            self.log_activity("ENTRY", f"Entry COMPLETE: maker={result.total_maker_qty:.6f} taker={result.total_taker_qty:.6f} ({result.end_ts - result.start_ts:.1f}s)")
        else:
            self.log_activity("ENTRY", f"Entry FAILED: {result.error}", level="error")
        self._log_trade("ENTRY", result)
        return result

    async def manual_exit(self, quantity: Decimal | None = None) -> ExecutionResult:
        """Manually trigger an exit.

        Reverses the entry direction. Uses the same maker/taker config
        but with flipped sides.
        """
        if not self._started:
            raise RuntimeError("Engine not started")

        pos = self._state_machine.position_info
        long_exch = pos["long_exchange"]
        short_exch = pos["short_exchange"]
        maker_exch = self.config.maker_exchange

        # For exit: reverse sides
        # If maker was buying (long) on entry, now it sells on exit
        taker_exch = short_exch if maker_exch == long_exch else long_exch

        if maker_exch == long_exch:
            maker_side = "sell"   # Closing the long
            taker_side = "buy"    # Closing the short
            maker_symbol = self._get_symbol(long_exch)
            taker_symbol = self._get_symbol(short_exch)
        else:
            maker_side = "buy"    # Closing the short
            taker_side = "sell"   # Closing the long
            maker_symbol = self._get_symbol(short_exch)
            taker_symbol = self._get_symbol(long_exch)

        # Use position size if no qty override
        qty = quantity or Decimal(str(abs(pos["long_qty"]))) or self.config.quantity

        config = MakerTakerConfig(
            maker_exchange=maker_exch,
            taker_exchange=taker_exch,
            maker_symbol=maker_symbol,
            taker_symbol=taker_symbol,
            maker_side=maker_side,
            taker_side=taker_side,
            total_qty=qty,
            num_chunks=self.config.twap_num_chunks,
            chunk_interval_s=self.config.twap_interval_s,
            maker_timeout_ms=self.config.maker_timeout_ms,
            maker_reprice_ticks=self.config.maker_reprice_ticks,
            maker_max_chase_rounds=self.config.maker_max_chase_rounds,
            maker_offset_ticks=self.config.maker_offset_ticks,
            simulation=self.config.simulation,
            reduce_only=True,
            max_chunk_spread_usd=self.config.max_chunk_spread_usd,
            min_spread_pct=self.config.min_spread_pct,
            max_spread_pct=self.config.max_spread_pct,
            use_depth_spread=self.config.fn_opt_depth_spread,
            max_slippage_bps=self.config.fn_opt_max_slippage_bps,
            taker_drift_guard=self.config.fn_opt_taker_drift_guard,
            max_taker_drift_bps=self.config.fn_opt_max_taker_drift_bps,
        )

        self.log_activity("EXIT", f"Maker={maker_exch} {maker_side} {maker_symbol}, Taker={taker_exch} {taker_side} {taker_symbol}, qty={qty} (reduce_only)")
        logger.info(
            "Manual EXIT: maker=%s(%s %s) taker=%s(%s %s) qty=%s",
            maker_exch, maker_side, maker_symbol,
            taker_exch, taker_side, taker_symbol, qty,
        )

        result = await self._state_machine.execute_exit(config)
        if result.success:
            self.log_activity("EXIT", f"Exit COMPLETE: maker={result.total_maker_qty:.6f} taker={result.total_taker_qty:.6f} ({result.end_ts - result.start_ts:.1f}s)")
        else:
            self.log_activity("EXIT", f"Exit FAILED: {result.error}", level="error")
        self._log_trade("EXIT", result)
        return result

    # ── Status / Info ─────────────────────────────────────────────────

    def get_live_prices(self) -> dict:
        """Return best bid/ask from DataLayer orderbooks for both exchanges."""
        if not self._data_layer:
            return {}
        result = {}
        for exch, sym in self._symbols_map.items():
            ob = self._data_layer.get_orderbook(exch, sym)
            best_bid = float(ob.bids[0][0]) if ob.bids else None
            best_ask = float(ob.asks[0][0]) if ob.asks else None
            mid = (best_bid + best_ask) / 2.0 if best_bid and best_ask else None
            result[exch] = {
                "symbol": sym,
                "best_bid": best_bid,
                "best_ask": best_ask,
                "mid": mid,
                "synced": ob.is_synced,
            }
        return result

    def get_unrealized_pnl(self) -> dict:
        """Compute unrealized PnL from entry prices vs current mid-prices."""
        if not self._state_machine or not self._data_layer:
            return {"long_pnl": 0.0, "short_pnl": 0.0, "total_pnl": 0.0}
        pos = self._state_machine.position_info
        long_entry = pos.get("long_entry_price", 0.0)
        short_entry = pos.get("short_entry_price", 0.0)
        long_qty = pos.get("long_qty", 0.0)
        short_qty = pos.get("short_qty", 0.0)

        if abs(long_qty) < 1e-8 or long_entry == 0.0:
            return {"long_pnl": 0.0, "short_pnl": 0.0, "total_pnl": 0.0}

        prices = self.get_live_prices()
        long_exch = pos.get("long_exchange", "")
        short_exch = pos.get("short_exchange", "")

        long_mid = prices.get(long_exch, {}).get("mid")
        short_mid = prices.get(short_exch, {}).get("mid")

        long_pnl = (long_mid - long_entry) * abs(long_qty) if long_mid and long_entry else 0.0
        short_pnl = (short_entry - short_mid) * abs(short_qty) if short_mid and short_entry else 0.0

        return {
            "long_pnl": round(long_pnl, 4),
            "short_pnl": round(short_pnl, 4),
            "total_pnl": round(long_pnl + short_pnl, 4),
        }

    def get_status(self) -> dict:
        """Return comprehensive engine status for dashboard."""
        now = time.time()
        remaining_s = None
        if self._expires_at:
            remaining_s = max(0, self._expires_at - now)

        return {
            "job_id": self.config.job_id,
            "started": self._started,
            "is_running": self._is_running,
            "is_paused": self._state_machine.is_paused if self._state_machine else False,
            "state": self._state_machine.state.value if self._state_machine else "NOT_STARTED",
            "timer": {
                "started_at": self._started_at,
                "expires_at": self._expires_at,
                "remaining_s": remaining_s,
                "duration_h": self.config.duration_h,
                "duration_m": self.config.duration_m,
                "stop_reason": self._stop_reason,
            },
            "leverage": {
                "long": self.config.leverage_long,
                "short": self.config.leverage_short,
            },
            "prices": self.get_live_prices(),
            "pnl": self.get_unrealized_pnl(),
            "position": self._state_machine.position_info if self._state_machine else {},
            "execution": self._state_machine.execution_status if self._state_machine else {},
            "funding": self._funding_monitor.get_rates() if self._funding_monitor else {},
            "funding_v4": self._funding_monitor.get_v4_data() if self._funding_monitor and self.config.fn_opt_funding_history else {},
            "risk": self._risk_manager.get_status() if self._risk_manager else {},
            "feeds_ready": self._data_layer.is_ready() if self._data_layer else False,
            "data": self._data_layer.status() if self._data_layer else {},
            "orderbooks": {
                "long": self._data_layer.get_orderbook_depth(self.config.long_exchange, self.config.instrument_a, depth=10),
                "short": self._data_layer.get_orderbook_depth(self.config.short_exchange, self.config.instrument_b, depth=10),
            } if self._data_layer and self.config.instrument_a and self.config.instrument_b else {},
            "ohi": {
                "long": self._data_layer.get_orderbook_health(self.config.long_exchange, self.config.instrument_a),
                "short": self._data_layer.get_orderbook_health(self.config.short_exchange, self.config.instrument_b),
            } if self._data_layer and self.config.fn_opt_ohi_monitoring and self.config.instrument_a and self.config.instrument_b else {},
            "config": {
                "long_exchange": self.config.long_exchange,
                "short_exchange": self.config.short_exchange,
                "maker_exchange": self.config.maker_exchange,
                "instrument_a": self.config.instrument_a,
                "instrument_b": self.config.instrument_b,
                "quantity": float(self.config.quantity),
                "twap_num_chunks": self.config.twap_num_chunks,
                "twap_interval_s": self.config.twap_interval_s,
                "simulation": self.config.simulation,
                "max_chunk_spread_usd": self.config.max_chunk_spread_usd,
                "min_spread_pct": self.config.min_spread_pct,
                "max_spread_pct": self.config.max_spread_pct,
                "fn_opt_depth_spread": self.config.fn_opt_depth_spread,
                "fn_opt_max_slippage_bps": self.config.fn_opt_max_slippage_bps,
                "fn_opt_ohi_monitoring": self.config.fn_opt_ohi_monitoring,
                "fn_opt_min_ohi": self.config.fn_opt_min_ohi,
                "fn_opt_funding_history": self.config.fn_opt_funding_history,
                "fn_opt_min_funding_consistency": self.config.fn_opt_min_funding_consistency,
                "fn_opt_dynamic_sizing": self.config.fn_opt_dynamic_sizing,
                "fn_opt_max_utilization": self.config.fn_opt_max_utilization,
                "fn_opt_max_per_pair_ratio": self.config.fn_opt_max_per_pair_ratio,
                "fn_opt_shared_monitor_url": self.config.fn_opt_shared_monitor_url,
            },
            "trade_count": len(self._trade_log),
            "activity_log": self.get_activity_log(limit=50),
        }

    def get_funding_suggestion(self) -> FundingSuggestion:
        """Return current funding rate suggestion."""
        if self._funding_monitor:
            return self._funding_monitor.get_suggestion()
        return FundingSuggestion()

    def get_trade_log(self, limit: int = 50) -> list[dict]:
        """Return recent trade log entries."""
        return self._trade_log[-limit:]

    # ── Activity Log ─────────────────────────────────────────────────

    def log_activity(self, category: str, message: str, **extra) -> None:
        """Append an activity log entry visible in the UI in real-time.

        Args:
            category: short tag, e.g. "ORDER", "FILL", "ENGINE", "RISK"
            message: human-readable description
            **extra: additional key-value pairs
        """
        self._activity_seq += 1
        entry = {
            "seq": self._activity_seq,
            "ts": time.time(),
            "cat": category,
            "msg": message,
        }
        if extra:
            entry["extra"] = extra
        self._activity_log.append(entry)
        # Forward to Cloudflare Analytics Engine
        if self._activity_forwarder:
            self._activity_forwarder.forward(category, message, "funding_arb", self.config.job_id)

    def get_activity_log(self, since_seq: int = 0, limit: int = 100) -> list[dict]:
        """Return activity log entries with seq > since_seq (for incremental fetch)."""
        if since_seq <= 0:
            return list(self._activity_log)[-limit:]
        return [e for e in self._activity_log if e["seq"] > since_seq][-limit:]

    def get_risk_alerts(self, limit: int = 20) -> list[dict]:
        """Return recent risk alerts."""
        if self._risk_manager:
            return self._risk_manager.get_alerts(limit)
        return []

    # ── Config updates ────────────────────────────────────────────────

    _HOT_UPDATE_KEYS = {
        "min_spread_pct", "max_spread_pct", "max_chunk_spread_usd",
        "quantity", "twap_num_chunks", "twap_interval_s",
        "maker_timeout_ms", "maker_reprice_ticks", "maker_max_chase_rounds",
        "maker_offset_ticks", "simulation", "duration_h", "duration_m",
        "fn_opt_depth_spread", "fn_opt_max_slippage_bps",
        "fn_opt_ohi_monitoring", "fn_opt_min_ohi",
        "fn_opt_funding_history", "fn_opt_min_funding_consistency",
        "fn_opt_dynamic_sizing", "fn_opt_max_utilization", "fn_opt_max_per_pair_ratio",
        "fn_opt_shared_monitor_url",
    }

    def update_config(self, **kwargs) -> None:
        """Update engine config fields.

        Most fields can be changed in IDLE, HOLDING, or PAUSED states.
        Exchange/instrument fields require IDLE (they trigger feed restarts).
        """
        sm_state = self._state_machine.state if self._state_machine else JobState.IDLE
        editable_states = {JobState.IDLE, JobState.HOLDING, JobState.PAUSED_ENTERING, JobState.PAUSED_EXITING}
        if sm_state not in editable_states:
            raise RuntimeError(f"Cannot update config in state {sm_state.value}")
        if sm_state != JobState.IDLE:
            non_hot = {k for k in kwargs if k not in self._HOT_UPDATE_KEYS}
            if non_hot:
                raise RuntimeError(f"Cannot update {non_hot} while not IDLE (requires feed restart)")
        needs_feed_restart = False
        for key, value in kwargs.items():
            if hasattr(self.config, key):
                old_val = getattr(self.config, key)
                setattr(self.config, key, value)
                logger.info("Config updated: %s=%s (was %s)", key, value, old_val)
                if key in ("long_exchange", "short_exchange", "instrument_a", "instrument_b"):
                    needs_feed_restart = True
            else:
                logger.warning("Unknown config key: %s", key)
        # Rebuild symbols map after config changes
        self._symbols_map = {
            self.config.long_exchange: self.config.instrument_a,
            self.config.short_exchange: self.config.instrument_b,
        }
        return needs_feed_restart

    async def apply_config_and_restart_feeds(self, **kwargs) -> dict:
        """Update config and restart DataLayer/FundingMonitor if instruments or exchanges changed."""
        needs_restart = self.update_config(**kwargs)
        if needs_restart and self._started:
            logger.info("Exchange/instrument changed — restarting data feeds")
            # Stop existing feeds
            if self._data_layer:
                await self._data_layer.stop()
            if self._funding_monitor:
                await self._funding_monitor.stop()
            # Restart with new symbols
            self._data_layer = DataLayer(shared_monitor_url=self.config.fn_opt_shared_monitor_url)
            await self._data_layer.start(self._clients, self._symbols_map)
            self._funding_monitor = FundingMonitor(
                data_layer=self._data_layer,
                exchange_a=self.config.long_exchange,
                symbol_a=self.config.instrument_a,
                exchange_b=self.config.short_exchange,
                symbol_b=self.config.instrument_b,
                poll_interval_s=self.config.funding_poll_interval_s,
                v4_enabled=self.config.fn_opt_funding_history,
                v4_api_url=self.config.fn_opt_funding_api_url,
                v4_min_consistency=self.config.fn_opt_min_funding_consistency,
            )
            await self._funding_monitor.start()
            # Re-wire risk manager with new data layer
            if self._risk_manager:
                await self._risk_manager.stop()
                self._risk_manager = RiskManager(
                    data_layer=self._data_layer,
                    clients=self._clients,
                    delta_max_usd=self.config.delta_max_usd,
                    circuit_breaker_loss_usd=self.config.circuit_breaker_loss_usd,
                    max_spread_pct=self.config.max_spread_pct,
                )
                await self._risk_manager.start()
            # Restart fill WS subscriptions with new symbols
            if self._state_machine:
                await self._state_machine.start_fill_subscriptions(self._symbols_map)
            logger.info("Data feeds restarted for %s", self._symbols_map)
        return {"status": "ok", "feeds_restarted": bool(needs_restart)}

    # ── Internal ──────────────────────────────────────────────────────

    def _get_symbol(self, exchange: str) -> str:
        """Map exchange name to its configured instrument symbol."""
        if exchange == self.config.long_exchange:
            return self.config.instrument_a
        elif exchange == self.config.short_exchange:
            return self.config.instrument_b
        raise ValueError(f"Unknown exchange for symbol lookup: {exchange}")

    def _log_trade(self, action: str, result: ExecutionResult) -> None:
        """Append a trade entry to the log."""
        entry = {
            "action": action,
            "timestamp": time.time(),
            "success": result.success,
            "error": result.error,
            "total_maker_qty": result.total_maker_qty,
            "total_taker_qty": result.total_taker_qty,
            "num_chunks": len(result.chunks),
            "duration_s": result.end_ts - result.start_ts,
            "chunks": [
                {
                    "index": c.chunk_index,
                    "maker_qty": c.maker_filled_qty,
                    "taker_qty": c.taker_filled_qty,
                    "maker_price": c.maker_price,
                    "taker_price": c.taker_price,
                    "error": c.error,
                }
                for c in result.chunks
            ],
        }
        self._trade_log.append(entry)
        logger.info("Trade logged: %s success=%s maker=%.6f taker=%.6f",
                     action, result.success, result.total_maker_qty, result.total_taker_qty)
