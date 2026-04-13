"""Funding rate monitor — display-only module with direction suggestion.

This module does NOT trigger any trades. It:
  1. Periodically reads funding rates from the DataLayer for both DEXs.
  2. Computes the funding spread.
  3. Generates a suggestion (recommended long/short side).
  4. Exposes the suggestion for the dashboard to display.

The user decides whether to follow or ignore the suggestion.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.data_layer import DataLayer

logger = logging.getLogger("tradeautonom.funding_monitor")


@dataclass
class FundingSuggestion:
    """Informational funding rate analysis — no trading action implied."""
    recommended_long_exchange: str = ""
    recommended_short_exchange: str = ""
    funding_rate_a: float = 0.0
    funding_rate_b: float = 0.0
    exchange_a: str = ""
    exchange_b: str = ""
    symbol_a: str = ""
    symbol_b: str = ""
    funding_spread: float = 0.0        # rate_a - rate_b
    funding_spread_annualised: float = 0.0
    reason: str = ""
    timestamp_ms: float = 0.0


class FundingMonitor:
    """Lightweight funding rate display + direction suggestion.

    No auto-trade signals. No entry/exit decisions.
    """

    def __init__(
        self,
        data_layer: DataLayer,
        exchange_a: str,
        symbol_a: str,
        exchange_b: str,
        symbol_b: str,
        poll_interval_s: float = 60.0,
    ) -> None:
        self._data_layer = data_layer
        self._exchange_a = exchange_a
        self._symbol_a = symbol_a
        self._exchange_b = exchange_b
        self._symbol_b = symbol_b
        self._poll_interval_s = poll_interval_s

        self._suggestion = FundingSuggestion(
            exchange_a=exchange_a,
            exchange_b=exchange_b,
            symbol_a=symbol_a,
            symbol_b=symbol_b,
        )
        self._task: asyncio.Task | None = None
        self._running = False

    # ── Public API ────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the background polling loop."""
        self._running = True
        self._task = asyncio.create_task(
            self._poll_loop(),
            name=f"funding-monitor-{self._exchange_a}-{self._exchange_b}",
        )
        logger.info(
            "FundingMonitor started: %s:%s vs %s:%s (poll=%.0fs)",
            self._exchange_a, self._symbol_a,
            self._exchange_b, self._symbol_b,
            self._poll_interval_s,
        )

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("FundingMonitor stopped")

    def get_suggestion(self) -> FundingSuggestion:
        """Return the latest funding suggestion (lock-free read)."""
        return self._suggestion

    def get_rates(self) -> dict:
        """Return current funding rates for both exchanges."""
        return {
            self._exchange_a: {
                "symbol": self._symbol_a,
                "rate": self._suggestion.funding_rate_a,
            },
            self._exchange_b: {
                "symbol": self._symbol_b,
                "rate": self._suggestion.funding_rate_b,
            },
            "spread": self._suggestion.funding_spread,
            "spread_annualised": self._suggestion.funding_spread_annualised,
            "recommended_long": self._suggestion.recommended_long_exchange,
            "recommended_short": self._suggestion.recommended_short_exchange,
            "reason": self._suggestion.reason,
            "age_ms": round((time.time() * 1000) - self._suggestion.timestamp_ms) if self._suggestion.timestamp_ms else None,
        }

    # ── Internal ──────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        """Periodically refresh suggestion from DataLayer funding rates.

        Uses a shorter interval (10s) until both rates are non-zero,
        then switches to the configured poll interval.
        """
        while self._running:
            try:
                self._update_suggestion()
            except Exception as exc:
                logger.warning("FundingMonitor poll error: %s", exc)
            # Fast-poll (10s) until we have real data, then slow down
            has_data = self._suggestion.funding_rate_a != 0.0 or self._suggestion.funding_rate_b != 0.0
            interval = self._poll_interval_s if has_data else 10.0
            await asyncio.sleep(interval)

    def _update_suggestion(self) -> None:
        """Read latest rates from DataLayer and compute suggestion."""
        snap_a = self._data_layer.get_funding_rate(self._exchange_a, self._symbol_a)
        snap_b = self._data_layer.get_funding_rate(self._exchange_b, self._symbol_b)

        rate_a = snap_a.funding_rate
        rate_b = snap_b.funding_rate

        spread = rate_a - rate_b
        # Annualise: assuming hourly funding × 8760 hours/year
        spread_annual = spread * 8760

        # Suggestion logic:
        # Long the exchange with the LOWER funding rate (we receive more / pay less)
        # Short the exchange with the HIGHER funding rate
        if rate_a < rate_b:
            rec_long = self._exchange_a
            rec_short = self._exchange_b
            reason = (
                f"{self._exchange_a} rate ({rate_a:+.6f}) < {self._exchange_b} rate ({rate_b:+.6f}) → "
                f"Long {self._exchange_a}, Short {self._exchange_b}"
            )
        elif rate_b < rate_a:
            rec_long = self._exchange_b
            rec_short = self._exchange_a
            reason = (
                f"{self._exchange_b} rate ({rate_b:+.6f}) < {self._exchange_a} rate ({rate_a:+.6f}) → "
                f"Long {self._exchange_b}, Short {self._exchange_a}"
            )
        else:
            rec_long = ""
            rec_short = ""
            reason = f"Rates equal ({rate_a:+.6f}) — no directional edge"

        self._suggestion = FundingSuggestion(
            recommended_long_exchange=rec_long,
            recommended_short_exchange=rec_short,
            funding_rate_a=rate_a,
            funding_rate_b=rate_b,
            exchange_a=self._exchange_a,
            exchange_b=self._exchange_b,
            symbol_a=self._symbol_a,
            symbol_b=self._symbol_b,
            funding_spread=spread,
            funding_spread_annualised=spread_annual,
            reason=reason,
            timestamp_ms=time.time() * 1000,
        )

        logger.debug(
            "FundingMonitor: %s=%.6f %s=%.6f spread=%.6f ann=%.4f%% → long=%s short=%s",
            self._exchange_a, rate_a, self._exchange_b, rate_b,
            spread, spread_annual * 100,
            rec_long, rec_short,
        )
