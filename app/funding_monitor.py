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
import re
import time
from dataclasses import dataclass, field

import httpx

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
        *,
        v4_enabled: bool = False,
        v4_api_url: str = "https://api.fundingrate.de",
        v4_min_consistency: float = 0.3,
    ) -> None:
        self._data_layer = data_layer
        self._exchange_a = exchange_a
        self._symbol_a = symbol_a
        self._exchange_b = exchange_b
        self._symbol_b = symbol_b
        self._poll_interval_s = poll_interval_s

        # V4 API settings
        self._v4_enabled = v4_enabled
        self._v4_api_url = v4_api_url.rstrip("/")
        self._v4_min_consistency = v4_min_consistency
        self._v4_data: dict = {}

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

    def get_v4_data(self) -> dict:
        """Return latest V4 historical funding data (lock-free read)."""
        return self._v4_data

    def is_v4_consistent(self) -> bool:
        """Check if V4 spread_consistency meets the minimum threshold.

        Returns True (pass) if V4 is disabled or data unavailable (fail-open).
        """
        if not self._v4_enabled or not self._v4_data:
            return True
        consistency = self._v4_data.get("spread_consistency", 0.0)
        return consistency >= self._v4_min_consistency

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

            # V4 API fetch (runs alongside local rate polling)
            if self._v4_enabled:
                try:
                    await self._fetch_v4_data()
                except Exception as exc:
                    logger.warning("FundingMonitor V4 fetch error: %s", exc)

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

    @staticmethod
    def _extract_base_symbol(instrument: str) -> str:
        """Extract the base ticker from an instrument string.

        Examples: 'SOL-USD' → 'SOL', 'SOL_USDT_Perp' → 'SOL',
                  'P-SUI-USDC-3600' → 'SUI', 'ETHPERP' → 'ETH'
        """
        # Remove common suffixes/patterns
        s = instrument.upper()
        # Variational: P-SUI-USDC-3600
        if s.startswith("P-"):
            parts = s.split("-")
            if len(parts) >= 2:
                return parts[1]
        # Perp suffix: ETHPERP, BTC-PERP
        s = re.sub(r"[-_]?PERP$", "", s)
        # Split on common delimiters and take first part
        parts = re.split(r"[-_/]", s)
        return parts[0] if parts else instrument.upper()

    async def _fetch_v4_data(self) -> None:
        """Fetch analysis data from Funding Rate V4 API.

        Uses /api/v4/analysis/{symbol} to get:
        - Per-exchange funding rates + MAs
        - Arbitrage pair confidence scores
        - spread_consistency for our specific exchange pair
        """
        base_symbol = self._extract_base_symbol(self._symbol_a)
        url = f"{self._v4_api_url}/api/v4/analysis/{base_symbol}"

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()

        if not data.get("success"):
            logger.warning("V4 API returned success=false for %s", base_symbol)
            return

        # Find the arbitrage pair matching our exchanges
        arb_pairs = data.get("arbitrage_pairs", [])
        exch_a_lower = self._exchange_a.lower()
        exch_b_lower = self._exchange_b.lower()

        matched_pair = None
        for pair in arb_pairs:
            short_ex = pair.get("short_exchange", "").lower()
            long_ex = pair.get("long_exchange", "").lower()
            if {short_ex, long_ex} == {exch_a_lower, exch_b_lower}:
                matched_pair = pair
                break

        # Extract per-exchange MA data
        exchanges_data = data.get("exchanges", [])
        exchange_ma = {}
        for exch_info in exchanges_data:
            ex_key = exch_info.get("exchange", "").lower()
            if ex_key in (exch_a_lower, exch_b_lower):
                exchange_ma[ex_key] = {
                    "funding_rate_apr": exch_info.get("funding_rate_apr", 0),
                    "ma": exch_info.get("ma", {}),
                }

        confidence = matched_pair.get("confidence", {}) if matched_pair else {}
        self._v4_data = {
            "symbol": base_symbol,
            "pair_found": matched_pair is not None,
            "spread_apr": matched_pair.get("spread_apr", 0) if matched_pair else 0,
            "confidence_score": matched_pair.get("confidence_score", 0) if matched_pair else 0,
            "spread_consistency": confidence.get("spread_consistency", 0),
            "volume_depth": confidence.get("volume_depth", 0),
            "rate_stability": confidence.get("rate_stability", 0),
            "historical_edge": confidence.get("historical_edge", 0),
            "exchange_ma": exchange_ma,
            "summary": data.get("summary", {}),
            "timestamp_ms": time.time() * 1000,
        }

        logger.info(
            "V4 API %s: pair_found=%s consistency=%.2f score=%d spread_apr=%.4f",
            base_symbol, self._v4_data["pair_found"],
            self._v4_data["spread_consistency"],
            self._v4_data["confidence_score"],
            self._v4_data["spread_apr"],
        )
