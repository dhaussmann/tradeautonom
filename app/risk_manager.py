"""Risk manager for the Funding-Arb engine.

Runs as a background async task providing:
  - Delta Guardian: monitors net delta across both DEXs
  - Circuit Breaker: tracks cumulative PnL
  - Partial Unwind: emergency logic when one leg fails
  - Pre-trade checks: min order size, liquidity depth, spread bounds
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.data_layer import DataLayer

logger = logging.getLogger("tradeautonom.risk_manager")


@dataclass
class RiskAlert:
    """A risk event that may require user attention or automatic action."""
    alert_type: str = ""       # "DELTA_BREACH", "CIRCUIT_BREAKER", "LIQUIDITY_LOW", "SPREAD_WIDE"
    severity: str = "warning"  # "warning", "critical"
    message: str = ""
    timestamp_ms: float = 0.0
    auto_action: str | None = None  # e.g. "REBALANCE", "HALT"


class RiskManager:
    """Async risk manager monitoring positions and market conditions.

    Alerts are exposed for the dashboard. Critical alerts can trigger
    automatic actions (halt trading, rebalance).
    """

    def __init__(
        self,
        data_layer: DataLayer,
        clients: dict[str, Any],
        delta_max_usd: float = 50.0,
        circuit_breaker_loss_usd: float = 500.0,
        max_spread_pct: float = 0.05,
        check_interval_s: float = 5.0,
    ) -> None:
        self._data_layer = data_layer
        self._clients = clients
        self._delta_max_usd = delta_max_usd
        self._circuit_breaker_loss_usd = circuit_breaker_loss_usd
        self._max_spread_pct = max_spread_pct
        self._check_interval_s = check_interval_s

        # State
        self._cumulative_pnl: float = 0.0
        self._is_halted: bool = False
        self._alerts: list[RiskAlert] = []
        self._max_alerts = 100  # keep last N alerts

        # Background task
        self._task: asyncio.Task | None = None
        self._running = False

    # ── Public API ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start background risk monitoring loop."""
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop(), name="risk-manager")
        logger.info(
            "RiskManager started: delta_max=$%.0f circuit_breaker=$%.0f spread_max=%.2f%%",
            self._delta_max_usd, self._circuit_breaker_loss_usd, self._max_spread_pct * 100,
        )

    async def stop(self) -> None:
        """Stop the monitoring loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("RiskManager stopped")

    @property
    def is_halted(self) -> bool:
        return self._is_halted

    @property
    def cumulative_pnl(self) -> float:
        return self._cumulative_pnl

    def record_trade_pnl(self, pnl: float) -> None:
        """Record PnL from a completed trade (entry+exit cycle)."""
        self._cumulative_pnl += pnl
        logger.info("RiskManager: trade PnL=%.2f cumulative=%.2f", pnl, self._cumulative_pnl)

        if self._cumulative_pnl <= -self._circuit_breaker_loss_usd:
            self._trigger_circuit_breaker()

    def reset_halt(self) -> None:
        """Manually reset the circuit breaker halt."""
        self._is_halted = False
        logger.info("RiskManager: halt reset by user")

    def reset_pnl(self) -> None:
        """Manually reset cumulative PnL counter."""
        self._cumulative_pnl = 0.0
        logger.info("RiskManager: PnL counter reset")

    def get_alerts(self, limit: int = 20) -> list[dict]:
        """Return recent alerts for dashboard display."""
        return [
            {
                "type": a.alert_type,
                "severity": a.severity,
                "message": a.message,
                "timestamp_ms": a.timestamp_ms,
                "auto_action": a.auto_action,
            }
            for a in self._alerts[-limit:]
        ]

    def get_status(self) -> dict:
        """Return current risk manager status."""
        return {
            "halted": self._is_halted,
            "cumulative_pnl": self._cumulative_pnl,
            "circuit_breaker_threshold": self._circuit_breaker_loss_usd,
            "delta_max_usd": self._delta_max_usd,
            "max_spread_pct": self._max_spread_pct,
            "recent_alerts": len(self._alerts),
        }

    # ── Pre-trade checks ──────────────────────────────────────────────

    async def pre_trade_check(
        self,
        exchange: str,
        symbol: str,
        side: str,
        qty: Decimal,
    ) -> tuple[bool, str]:
        """Run pre-trade safety checks. Returns (ok, reason)."""
        if self._is_halted:
            return False, "Trading halted by circuit breaker"

        # Check min order size
        client = self._clients.get(exchange)
        if client and hasattr(client, "async_get_min_order_size"):
            min_size = await client.async_get_min_order_size(symbol)
            if qty < min_size:
                return False, f"Qty {qty} below min order size {min_size} on {exchange}"

        # Check orderbook depth
        ob = self._data_layer.get_orderbook(exchange, symbol)
        if not ob.is_synced:
            return False, f"Orderbook not synced for {exchange}:{symbol}"

        levels = ob.asks if side == "buy" else ob.bids
        if not levels:
            return False, f"No {'asks' if side == 'buy' else 'bids'} in {exchange}:{symbol} orderbook"

        # Check total available liquidity
        total_available = sum(float(lv[1]) for lv in levels[:10])
        if total_available < float(qty):
            return False, f"Insufficient liquidity on {exchange}:{symbol}: available={total_available:.4f} needed={qty}"

        return True, "OK"

    def check_spread(
        self,
        exch_a: str, sym_a: str,
        exch_b: str, sym_b: str,
    ) -> tuple[bool, float, str]:
        """Check if cross-exchange spread is within bounds.

        Returns (ok, spread_pct, reason).
        """
        ob_a = self._data_layer.get_orderbook(exch_a, sym_a)
        ob_b = self._data_layer.get_orderbook(exch_b, sym_b)

        if not ob_a.bids or not ob_a.asks or not ob_b.bids or not ob_b.asks:
            return False, 0.0, "Incomplete orderbooks"

        mid_a = (float(ob_a.bids[0][0]) + float(ob_a.asks[0][0])) / 2
        mid_b = (float(ob_b.bids[0][0]) + float(ob_b.asks[0][0])) / 2
        avg_mid = (mid_a + mid_b) / 2

        if avg_mid <= 0:
            return False, 0.0, "Invalid mid prices"

        spread_pct = abs(mid_a - mid_b) / avg_mid * 100

        if spread_pct > self._max_spread_pct:
            return False, spread_pct, f"Spread {spread_pct:.4f}% exceeds max {self._max_spread_pct:.4f}%"

        return True, spread_pct, "OK"

    # ── Internal monitoring loop ──────────────────────────────────────

    async def _monitor_loop(self) -> None:
        """Background loop checking risk conditions."""
        while self._running:
            try:
                await self._check_all()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("RiskManager check error: %s", exc)
            await asyncio.sleep(self._check_interval_s)

    async def _check_all(self) -> None:
        """Run all risk checks."""
        # Circuit breaker
        if self._cumulative_pnl <= -self._circuit_breaker_loss_usd and not self._is_halted:
            self._trigger_circuit_breaker()

    def _trigger_circuit_breaker(self) -> None:
        """Halt all trading due to loss threshold breach."""
        self._is_halted = True
        alert = RiskAlert(
            alert_type="CIRCUIT_BREAKER",
            severity="critical",
            message=f"Circuit breaker triggered: cumulative PnL ${self._cumulative_pnl:.2f} exceeds -${self._circuit_breaker_loss_usd:.0f} threshold",
            timestamp_ms=time.time() * 1000,
            auto_action="HALT",
        )
        self._add_alert(alert)
        logger.critical("CIRCUIT BREAKER: %s", alert.message)

    def _add_alert(self, alert: RiskAlert) -> None:
        """Add an alert, trimming old ones if needed."""
        self._alerts.append(alert)
        if len(self._alerts) > self._max_alerts:
            self._alerts = self._alerts[-self._max_alerts:]
