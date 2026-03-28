"""Multi-job arbitrage manager.

Each ArbJob wraps an ArbitrageEngine with schedule-based exit logic,
trade logging with real fill prices, and independent lifecycle management.
"""

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.arbitrage import ArbitrageEngine, ArbCheckResult, ArbExecutionResult, SpreadSnapshot
from app.config import Settings
from app.executor import TradeExecutor, TradeResult
from app.exchange import ExchangeClient
from app.ws_feeds import OrderbookFeedManager

logger = logging.getLogger("tradeautonom.jobs")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class JobSchedule:
    """Exit schedule for an arb job."""
    hold_duration_h: float | None = None   # max hours before scheduled exit (None = no limit)
    min_exit_spread: float = 0.05          # exit spread after hold_duration expires


@dataclass
class TradeLogEntry:
    """Record of a single entry or exit execution with real fill prices."""
    timestamp: str                    # ISO-8601
    job_id: str
    action: str                       # "ENTRY" or "EXIT"
    leg_a_instrument: str
    leg_b_instrument: str
    leg_a_exchange: str
    leg_b_exchange: str
    leg_a_side: str                   # "buy" or "sell"
    leg_b_side: str
    leg_a_fill_price: float | None    # from order response
    leg_b_fill_price: float | None    # from order response
    spread_at_execution: float
    quantity: float
    success: bool
    error: str | None = None


def _extract_fill_price(result: TradeResult | None) -> float | None:
    """Extract actual fill price from a TradeResult's order response."""
    if result is None or result.order_response is None:
        return None
    resp = result.order_response
    # Simulation mode
    if resp.get("simulated"):
        return resp.get("price")
    # GRVT: state.traded_price or average from traded_size
    state = resp.get("state", {}) if isinstance(resp, dict) else {}
    if state.get("traded_price"):
        try:
            return float(state["traded_price"])
        except (ValueError, TypeError):
            pass
    # Extended: price field or avg_price
    for key in ("price", "avg_price", "avgPrice", "average_price"):
        if resp.get(key):
            try:
                return float(resp[key])
            except (ValueError, TypeError):
                pass
    # Slippage result estimated fill
    if result.slippage and result.slippage.estimated_fill_price:
        return result.slippage.estimated_fill_price
    return None


@dataclass
class ArbJob:
    """A single arbitrage job: one token pair with its own engine + schedule."""
    job_id: str
    name: str                                      # display name
    engine: ArbitrageEngine
    schedule: JobSchedule
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    entry_time: str | None = None                  # ISO-8601, set on successful entry
    status: str = "idle"                           # idle / monitoring / holding / awaiting_exit / exited
    trade_log: list[TradeLogEntry] = field(default_factory=list)
    auto_trade: bool = False                       # whether auto-trading is enabled for this job
    # Cached last spread for SSE / dashboard
    _last_spread: SpreadSnapshot | None = field(default=None, repr=False)

    def to_summary(self) -> dict:
        """Lightweight summary for job listing."""
        pi = self.engine.position_info
        return {
            "job_id": self.job_id,
            "name": self.name,
            "status": self.status,
            "auto_trade": self.auto_trade,
            "instrument_a": self.engine.instrument_a,
            "instrument_b": self.engine.instrument_b,
            "leg_a_exchange": self.engine.leg_a_exchange,
            "leg_b_exchange": self.engine.leg_b_exchange,
            "has_position": pi["has_position"],
            "entry_time": self.entry_time,
            "created_at": self.created_at,
            "schedule": {
                "hold_duration_h": self.schedule.hold_duration_h,
                "min_exit_spread": self.schedule.min_exit_spread,
            },
        }


# ---------------------------------------------------------------------------
# JobManager
# ---------------------------------------------------------------------------

class JobManager:
    """Manages multiple ArbJob instances with shared resources."""

    def __init__(
        self,
        exchange_clients: dict[str, ExchangeClient],
        executor: TradeExecutor,
        settings: Settings,
        feed_manager: OrderbookFeedManager | None = None,
    ):
        self._clients = exchange_clients
        self._executor = executor
        self._settings = settings
        self._feed_manager = feed_manager
        self._jobs: dict[str, ArbJob] = {}

    @property
    def feed_manager(self) -> OrderbookFeedManager | None:
        return self._feed_manager

    @feed_manager.setter
    def feed_manager(self, mgr: OrderbookFeedManager | None):
        self._feed_manager = mgr
        # Attach to all existing engines
        for job in self._jobs.values():
            if mgr is not None:
                job.engine.set_feed_manager(mgr)

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def create_job(self, config: dict) -> ArbJob:
        """Create a new arb job from a config dict."""
        job_id = config.get("job_id") or str(uuid.uuid4())[:8]
        if job_id in self._jobs:
            raise ValueError(f"Job {job_id!r} already exists")

        # Build a Settings-like object for the engine by overlaying config on defaults
        engine_settings = self._build_engine_settings(config)
        engine = ArbitrageEngine(self._clients, self._executor, engine_settings)
        engine.sync_position_from_exchange()

        if self._feed_manager is not None:
            engine.set_feed_manager(self._feed_manager)
            # Add WS feeds for this pair
            leg_a_ex = config.get("leg_a_exchange", self._settings.arb_leg_a_exchange)
            inst_a = config.get("instrument_a", self._settings.arb_xau_instrument)
            leg_b_ex = config.get("leg_b_exchange", self._settings.arb_leg_b_exchange)
            inst_b = config.get("instrument_b", self._settings.arb_paxg_instrument)
            self._feed_manager.add_feed(leg_a_ex, inst_a)
            self._feed_manager.add_feed(leg_b_ex, inst_b)

        schedule = JobSchedule(
            hold_duration_h=config.get("hold_duration_h"),
            min_exit_spread=config.get("min_exit_spread", 0.05),
        )

        name = config.get("name") or f"{engine.instrument_a} / {engine.instrument_b}"

        job = ArbJob(
            job_id=job_id,
            name=name,
            engine=engine,
            schedule=schedule,
            auto_trade=config.get("auto_trade", False),
        )

        # If engine already has a position (synced from exchange), mark as holding
        if engine._has_position:
            job.status = "holding"

        # Load persisted trade log from disk
        job.trade_log = self._load_trade_log(job_id)

        self._jobs[job_id] = job
        logger.info("Job created: %s (%s) — %s@%s / %s@%s",
                     job_id, name, engine.instrument_a, engine.leg_a_exchange,
                     engine.instrument_b, engine.leg_b_exchange)
        return job

    def delete_job(self, job_id: str) -> None:
        """Delete a job and optionally remove its WS feeds."""
        job = self._jobs.pop(job_id, None)
        if job is None:
            raise ValueError(f"Job {job_id!r} not found")
        # Remove WS feeds if no other job uses the same instruments
        if self._feed_manager is not None:
            self._maybe_remove_feed(job.engine.leg_a_exchange, job.engine.instrument_a)
            self._maybe_remove_feed(job.engine.leg_b_exchange, job.engine.instrument_b)
        logger.info("Job deleted: %s", job_id)

    def get_job(self, job_id: str) -> ArbJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise ValueError(f"Job {job_id!r} not found")
        return job

    def list_jobs(self) -> list[dict]:
        return [j.to_summary() for j in self._jobs.values()]

    def update_job_config(self, job_id: str, config: dict) -> ArbJob:
        """Update an existing job's configuration."""
        job = self.get_job(job_id)
        engine = job.engine

        old_inst_a = engine.instrument_a
        old_inst_b = engine.instrument_b
        old_ex_a = engine.leg_a_exchange
        old_ex_b = engine.leg_b_exchange

        # Update engine params
        if config.get("instrument_a") is not None:
            engine.instrument_a = config["instrument_a"]
            engine.xau_instrument = config["instrument_a"]
        if config.get("instrument_b") is not None:
            engine.instrument_b = config["instrument_b"]
            engine.paxg_instrument = config["instrument_b"]
        if config.get("leg_a_exchange") is not None:
            engine.leg_a_exchange = config["leg_a_exchange"]
        if config.get("leg_b_exchange") is not None:
            engine.leg_b_exchange = config["leg_b_exchange"]
        if config.get("spread_entry_low") is not None:
            engine.spread_entry_low = config["spread_entry_low"]
        if config.get("spread_exit_high") is not None:
            engine.spread_exit_high = config["spread_exit_high"]
        if config.get("max_exec_spread") is not None:
            engine.max_exec_spread = config["max_exec_spread"]
        if config.get("quantity") is not None:
            engine.quantity = Decimal(str(config["quantity"]))
        if config.get("simulation_mode") is not None:
            engine.simulation_mode = config["simulation_mode"]
        if config.get("order_type") is not None:
            engine.order_type = config["order_type"]
        if config.get("limit_offset_ticks") is not None:
            engine.limit_offset_ticks = config["limit_offset_ticks"]
        if config.get("min_profit") is not None:
            engine.min_profit = config["min_profit"]
        if config.get("fill_timeout_ms") is not None:
            engine.fill_timeout_ms = config["fill_timeout_ms"]
        if config.get("chunk_size") is not None:
            engine.chunk_size = Decimal(str(config["chunk_size"]))
        if config.get("chunk_delay_ms") is not None:
            engine.chunk_delay_ms = config["chunk_delay_ms"]
        if config.get("liquidity_multiplier") is not None:
            engine.liquidity_multiplier = config["liquidity_multiplier"]

        # Update schedule
        if config.get("hold_duration_h") is not None:
            job.schedule.hold_duration_h = config["hold_duration_h"] if config["hold_duration_h"] != 0 else None
        if config.get("min_exit_spread") is not None:
            job.schedule.min_exit_spread = config["min_exit_spread"]

        if config.get("auto_trade") is not None:
            job.auto_trade = config["auto_trade"]
        if config.get("name") is not None:
            job.name = config["name"]

        # Handle instrument changes: update WS feeds
        instruments_changed = (
            engine.instrument_a != old_inst_a or engine.instrument_b != old_inst_b
            or engine.leg_a_exchange != old_ex_a or engine.leg_b_exchange != old_ex_b
        )
        if instruments_changed and self._feed_manager is not None:
            self._maybe_remove_feed(old_ex_a, old_inst_a)
            self._maybe_remove_feed(old_ex_b, old_inst_b)
            self._feed_manager.add_feed(engine.leg_a_exchange, engine.instrument_a)
            self._feed_manager.add_feed(engine.leg_b_exchange, engine.instrument_b)
            # Reset position state
            engine._has_position = False
            engine._long_sym = None
            engine._short_sym = None
            engine._entry_spread_actual = None
            job.entry_time = None
            job.status = "idle"

        logger.info("Job %s config updated", job_id)
        return job

    # ------------------------------------------------------------------
    # Tick — called periodically by the auto-trade loop
    # ------------------------------------------------------------------

    def tick_all(self) -> dict[str, dict]:
        """Evaluate all jobs and execute trades where conditions are met.

        Returns a dict of {job_id: {"action": ..., "result": ...}} for jobs that acted.
        """
        results = {}
        for job_id, job in list(self._jobs.items()):
            try:
                result = self._tick_job(job)
                if result is not None:
                    results[job_id] = result
            except Exception as exc:
                logger.error("Error ticking job %s: %s", job_id, exc, exc_info=True)
        return results

    def _tick_job(self, job: ArbJob) -> dict | None:
        """Evaluate a single job's schedule and execute if conditions met."""
        engine = job.engine

        try:
            snapshot = engine.get_spread_snapshot()
            job._last_spread = snapshot
        except Exception as exc:
            logger.warning("Job %s: failed to get spread: %s", job.job_id, exc)
            return None

        check = engine.evaluate(snapshot)

        # Update status
        if not engine._has_position:
            job.status = "monitoring" if job.auto_trade else "idle"
        elif job.entry_time and job.schedule.hold_duration_h is not None:
            elapsed_h = (datetime.now(timezone.utc) - datetime.fromisoformat(job.entry_time)).total_seconds() / 3600
            if elapsed_h >= job.schedule.hold_duration_h:
                job.status = "awaiting_exit"
            else:
                job.status = "holding"
        elif engine._has_position:
            job.status = "holding"

        if not job.auto_trade:
            return None

        # --- SCHEDULE-BASED EXIT LOGIC ---
        if engine._has_position:
            # One-sided position (synced from exchange): exit will fail because
            # execute_exit needs both _long_sym and _short_sym to determine direction.
            if not engine._long_sym or not engine._short_sym:
                logger.warning(
                    "Job %s: one-sided position (long=%s short=%s) — "
                    "auto-exit skipped, manual close required.",
                    job.job_id, engine._long_sym, engine._short_sym,
                )
                job.status = "awaiting_exit"
                return None

            exec_abs = abs(snapshot.exec_spread)
            should_exit = False
            reason = ""

            # 1. Scheduled exit: after hold_duration, if exec spread <= min_exit_spread
            if job.entry_time and job.schedule.hold_duration_h is not None:
                elapsed_h = (datetime.now(timezone.utc) - datetime.fromisoformat(job.entry_time)).total_seconds() / 3600
                if elapsed_h >= job.schedule.hold_duration_h and exec_abs <= job.schedule.min_exit_spread:
                    should_exit = True
                    reason = (
                        f"Scheduled exit: {elapsed_h:.1f}h >= {job.schedule.hold_duration_h}h "
                        f"and exec spread ${snapshot.exec_spread:.4f} (abs ${exec_abs:.4f}) <= min_exit ${job.schedule.min_exit_spread:.4f}"
                    )

            # 2. Original threshold exit (spread_exit_high from engine config)
            elif check.action == "EXIT":
                should_exit = True
                reason = check.reason

            if should_exit:
                logger.info("Job %s: EXIT triggered — %s", job.job_id, reason)
                result = engine.execute_exit(snapshot)
                self._log_trade(job, "EXIT", result, snapshot)
                if result.success:
                    job.entry_time = None
                    job.status = "monitoring" if job.auto_trade else "idle"
                return {"action": "EXIT", "success": result.success, "reason": reason, "error": result.error}

        # --- ENTRY LOGIC ---
        elif check.action == "ENTRY":
            logger.info("Job %s: ENTRY triggered — %s", job.job_id, check.reason)
            result = engine.execute_entry(snapshot)
            self._log_trade(job, "ENTRY", result, snapshot)
            if result.success:
                job.entry_time = datetime.now(timezone.utc).isoformat()
                job.status = "holding"
            return {"action": "ENTRY", "success": result.success, "reason": check.reason, "error": result.error}

        return None

    # ------------------------------------------------------------------
    # Trade logging
    # ------------------------------------------------------------------

    def _log_trade(self, job: ArbJob, action: str, result: ArbExecutionResult, snapshot: SpreadSnapshot) -> None:
        """Record an entry/exit with real fill prices."""
        engine = job.engine

        if action == "ENTRY":
            leg_a_side = "buy" if snapshot.a_is_cheaper else "sell"
            leg_b_side = "sell" if snapshot.a_is_cheaper else "buy"
        else:
            # EXIT: reverse of entry direction
            leg_a_side = "sell" if engine._long_sym == engine.instrument_a else "buy"
            leg_b_side = "sell" if engine._long_sym == engine.instrument_b else "buy"

        entry = TradeLogEntry(
            timestamp=datetime.now(timezone.utc).isoformat(),
            job_id=job.job_id,
            action=action,
            leg_a_instrument=engine.instrument_a,
            leg_b_instrument=engine.instrument_b,
            leg_a_exchange=engine.leg_a_exchange,
            leg_b_exchange=engine.leg_b_exchange,
            leg_a_side=leg_a_side,
            leg_b_side=leg_b_side,
            leg_a_fill_price=_extract_fill_price(result.leg_a),
            leg_b_fill_price=_extract_fill_price(result.leg_b),
            spread_at_execution=snapshot.exec_spread,
            quantity=float(engine.quantity),
            success=result.success,
            error=result.error,
        )
        job.trade_log.append(entry)
        if len(job.trade_log) > 500:
            job.trade_log = job.trade_log[-500:]

        # Persist to disk
        self._persist_trade_log_entry(entry)

        logger.info(
            "TRADE LOG [%s] %s %s: success=%s spread=$%.4f "
            "leg_a=%s@%s fill=$%s | leg_b=%s@%s fill=$%s",
            job.job_id, action, "OK" if result.success else "FAIL",
            result.success, snapshot.spread_abs,
            engine.instrument_a, engine.leg_a_exchange,
            entry.leg_a_fill_price or "?",
            engine.instrument_b, engine.leg_b_exchange,
            entry.leg_b_fill_price or "?",
        )

    # ------------------------------------------------------------------
    # Trade log persistence
    # ------------------------------------------------------------------

    _TRADE_LOG_DIR = Path("data/trade_logs")

    def _persist_trade_log_entry(self, entry: TradeLogEntry) -> None:
        """Append a single trade log entry to a per-job JSONL file."""
        try:
            self._TRADE_LOG_DIR.mkdir(parents=True, exist_ok=True)
            path = self._TRADE_LOG_DIR / f"{entry.job_id}.jsonl"
            with open(path, "a") as fh:
                fh.write(json.dumps(asdict(entry)) + "\n")
        except Exception as exc:
            logger.warning("Failed to persist trade log entry: %s", exc)

    def _load_trade_log(self, job_id: str) -> list[TradeLogEntry]:
        """Load trade log from disk for a given job."""
        path = self._TRADE_LOG_DIR / f"{job_id}.jsonl"
        if not path.exists():
            return []
        entries = []
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    d = json.loads(line)
                    entries.append(TradeLogEntry(**d))
        except Exception as exc:
            logger.warning("Failed to load trade log for %s: %s", job_id, exc)
        # Keep only last 500
        return entries[-500:]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_engine_settings(self, config: dict) -> Settings:
        """Build a Settings object for a new engine, overlaying config on defaults."""
        # Start with a copy of the current settings
        data = {}
        for fld in self._settings.model_fields:
            data[fld] = getattr(self._settings, fld)

        # Map job config keys to settings keys
        mapping = {
            "instrument_a": "arb_xau_instrument",
            "instrument_b": "arb_paxg_instrument",
            "leg_a_exchange": "arb_leg_a_exchange",
            "leg_b_exchange": "arb_leg_b_exchange",
            "spread_entry_low": "arb_spread_entry_low",
            "spread_exit_high": "arb_spread_exit_high",
            "max_exec_spread": "arb_max_exec_spread",
            "quantity": "arb_quantity",
            "simulation_mode": "arb_simulation_mode",
            "order_type": "arb_order_type",
            "limit_offset_ticks": "arb_limit_offset_ticks",
            "min_profit": "arb_min_profit",
            "fill_timeout_ms": "arb_fill_timeout_ms",
            "chunk_size": "arb_chunk_size",
            "chunk_delay_ms": "arb_chunk_delay_ms",
            "liquidity_multiplier": "arb_liquidity_multiplier",
            "ws_enabled": "arb_ws_enabled",
            "ws_stale_ms": "arb_ws_stale_ms",
        }
        for cfg_key, settings_key in mapping.items():
            if cfg_key in config and config[cfg_key] is not None:
                data[settings_key] = config[cfg_key]

        return Settings(**data)

    def _maybe_remove_feed(self, exchange: str, instrument: str) -> None:
        """Remove a WS feed only if no other job uses the same exchange:instrument."""
        for job in self._jobs.values():
            eng = job.engine
            if (eng.leg_a_exchange == exchange and eng.instrument_a == instrument):
                return
            if (eng.leg_b_exchange == exchange and eng.instrument_b == instrument):
                return
        if self._feed_manager is not None:
            self._feed_manager.remove_feed(exchange, instrument)

    def get_spread_snapshot(self, job_id: str) -> SpreadSnapshot:
        """Get current spread for a specific job."""
        job = self.get_job(job_id)
        snapshot = job.engine.get_spread_snapshot()
        job._last_spread = snapshot
        return snapshot
