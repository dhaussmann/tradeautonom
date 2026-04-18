"""Execution Logger — captures per-chunk orderbook snapshots + fill results
and pushes them to Cloudflare D1 for AI training data.

Each TWAP chunk produces one row: the market state at decision time (before
order placement) merged with the actual execution outcome (after fill).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
import uuid
from datetime import datetime, timezone
from typing import Any, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from app.data_layer import DataLayer

logger = logging.getLogger("tradeautonom.execution_logger")

# BTC volatility cache
_btc_vol_cache: dict[str, Any] = {"value": None, "fetched_at": 0.0}
_BTC_VOL_TTL_S = 300  # 5 minutes


def depth_within_bps(book: dict, bps: float) -> float:
    """Compute total USD liquidity within `bps` basis points of mid price.

    book = {"bids": [[price, qty], ...], "asks": [[price, qty], ...]}
    """
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return 0.0
    best_bid = float(bids[0][0])
    best_ask = float(asks[0][0])
    if best_bid <= 0 or best_ask <= 0:
        return 0.0
    mid = (best_bid + best_ask) / 2.0
    threshold = mid * bps / 10000.0
    total = 0.0
    for price_s, qty_s in bids:
        price = float(price_s)
        if price >= mid - threshold:
            total += price * float(qty_s)
        else:
            break
    for price_s, qty_s in asks:
        price = float(price_s)
        if price <= mid + threshold:
            total += price * float(qty_s)
        else:
            break
    return total


async def _fetch_btc_vol_1h() -> float | None:
    """Fetch BTC 1h realized volatility from CoinGecko (free, no key).

    Returns annualized volatility as a decimal (e.g. 0.45 = 45%).
    Cached for 5 minutes.
    """
    now = time.time()
    if _btc_vol_cache["value"] is not None and (now - _btc_vol_cache["fetched_at"]) < _BTC_VOL_TTL_S:
        return _btc_vol_cache["value"]

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart",
                params={"vs_currency": "usd", "days": "1"},
            )
            resp.raise_for_status()
            data = resp.json()

        prices = data.get("prices", [])
        if len(prices) < 10:
            return None

        # Use last 60 data points (~1h, CoinGecko returns ~5min intervals)
        recent = prices[-60:]
        log_returns = []
        for i in range(1, len(recent)):
            p0 = recent[i - 1][1]
            p1 = recent[i][1]
            if p0 > 0 and p1 > 0:
                log_returns.append(math.log(p1 / p0))

        if len(log_returns) < 5:
            return None

        mean = sum(log_returns) / len(log_returns)
        variance = sum((r - mean) ** 2 for r in log_returns) / len(log_returns)
        std = math.sqrt(variance)

        # Annualize: ~288 five-minute periods per day, sqrt(365) for yearly
        annualized = std * math.sqrt(288 * 365)

        _btc_vol_cache["value"] = round(annualized, 4)
        _btc_vol_cache["fetched_at"] = now
        return _btc_vol_cache["value"]

    except Exception as exc:
        logger.debug("BTC vol fetch failed: %s", exc)
        return _btc_vol_cache.get("value")  # return stale if available


def new_execution_id() -> str:
    """Generate a unique execution ID for grouping chunks."""
    return str(uuid.uuid4())


class ExecutionLogger:
    """Captures per-chunk snapshots and pushes them to D1 in batches."""

    def __init__(
        self,
        ingest_url: str,
        ingest_token: str,
        bot_id: str = "",
        enabled: bool = True,
    ) -> None:
        self._ingest_url = ingest_url.rstrip("/")
        if "/api/execution-log/ingest" not in self._ingest_url:
            # Derive execution-log URL from history ingest URL
            self._ingest_url = self._ingest_url.replace(
                "/api/history/ingest", "/api/execution-log/ingest"
            )
        self._ingest_token = ingest_token
        self._bot_id = bot_id
        self._enabled = enabled
        self._buffer: list[dict] = []
        self._flush_task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        """Start the background flush loop."""
        if not self._enabled or self._running:
            return
        self._running = True
        self._flush_task = asyncio.create_task(self._flush_loop())
        logger.info("ExecutionLogger started (url=%s)", self._ingest_url)

    async def stop(self) -> None:
        """Flush remaining entries and stop."""
        self._running = False
        if self._buffer:
            await self._flush()
        if self._flush_task and not self._flush_task.done():
            self._flush_task.cancel()

    def capture_snapshot(
        self,
        data_layer: "DataLayer",
        maker_exchange: str,
        maker_symbol: str,
        taker_exchange: str,
        taker_symbol: str,
        funding_monitor: Any = None,
    ) -> dict:
        """Capture orderbook + market state at decision time (before order placement).

        Returns a dict to be merged with fill results later.
        """
        snapshot: dict[str, Any] = {"timestamp_ms": time.time() * 1000}

        # Maker orderbook
        try:
            m_snap = data_layer.get_orderbook(maker_exchange, maker_symbol)
            if m_snap.bids and m_snap.asks:
                bb_m = float(m_snap.bids[0][0])
                ba_m = float(m_snap.asks[0][0])
                mid_m = (bb_m + ba_m) / 2.0
                snapshot["snapshot_mid_maker"] = round(mid_m, 6)
                snapshot["snapshot_best_bid_maker"] = round(bb_m, 6)
                snapshot["snapshot_best_ask_maker"] = round(ba_m, 6)
                spread_m = (ba_m - bb_m) / mid_m * 10000 if mid_m > 0 else 0
                snapshot["snapshot_bid_ask_spread_maker_bps"] = round(spread_m, 2)
                m_book = {"bids": m_snap.bids, "asks": m_snap.asks}
                snapshot["snapshot_depth_5bps_maker"] = round(depth_within_bps(m_book, 5), 2)
                snapshot["snapshot_depth_20bps_maker"] = round(depth_within_bps(m_book, 20), 2)
        except Exception:
            pass

        # Taker orderbook
        try:
            t_snap = data_layer.get_orderbook(taker_exchange, taker_symbol)
            if t_snap.bids and t_snap.asks:
                bb_t = float(t_snap.bids[0][0])
                ba_t = float(t_snap.asks[0][0])
                mid_t = (bb_t + ba_t) / 2.0
                snapshot["snapshot_mid_taker"] = round(mid_t, 6)
                snapshot["snapshot_best_bid_taker"] = round(bb_t, 6)
                snapshot["snapshot_best_ask_taker"] = round(ba_t, 6)
                spread_t = (ba_t - bb_t) / mid_t * 10000 if mid_t > 0 else 0
                snapshot["snapshot_bid_ask_spread_taker_bps"] = round(spread_t, 2)
                t_book = {"bids": t_snap.bids, "asks": t_snap.asks}
                snapshot["snapshot_depth_5bps_taker"] = round(depth_within_bps(t_book, 5), 2)
                snapshot["snapshot_depth_20bps_taker"] = round(depth_within_bps(t_book, 20), 2)
        except Exception:
            pass

        # Cross-venue spread
        mid_m = snapshot.get("snapshot_mid_maker")
        mid_t = snapshot.get("snapshot_mid_taker")
        if mid_m and mid_t and mid_m > 0:
            snapshot["snapshot_spread_bps"] = round((mid_t - mid_m) / mid_m * 10000, 2)

        # OHI
        try:
            ohi_m = data_layer.get_orderbook_health(maker_exchange, maker_symbol)
            snapshot["snapshot_ohi_maker"] = ohi_m.get("ohi", 0) if ohi_m else None
        except Exception:
            pass
        try:
            ohi_t = data_layer.get_orderbook_health(taker_exchange, taker_symbol)
            snapshot["snapshot_ohi_taker"] = ohi_t.get("ohi", 0) if ohi_t else None
        except Exception:
            pass

        # Funding rates
        if funding_monitor:
            try:
                rates = funding_monitor.get_rates()
                # Determine which is long and which is short
                long_exch = rates.get("recommended_long", "")
                if long_exch == maker_exchange:
                    snapshot["funding_rate_long"] = rates.get(maker_exchange, {}).get("rate")
                    snapshot["funding_rate_short"] = rates.get(taker_exchange, {}).get("rate")
                else:
                    snapshot["funding_rate_long"] = rates.get(taker_exchange, {}).get("rate")
                    snapshot["funding_rate_short"] = rates.get(maker_exchange, {}).get("rate")
                snapshot["funding_spread"] = rates.get("spread")
            except Exception:
                pass

            # V4 data
            try:
                v4 = funding_monitor.get_v4_data()
                if v4:
                    snapshot["v4_spread_consistency"] = v4.get("spread_consistency")
                    snapshot["v4_confidence_score"] = v4.get("confidence_score")
            except Exception:
                pass

        # Time context
        now_utc = datetime.now(timezone.utc)
        snapshot["hour_of_day"] = now_utc.hour
        snapshot["day_of_week"] = now_utc.weekday()

        return snapshot

    async def capture_btc_volatility(self, snapshot: dict) -> None:
        """Fetch BTC vol async and add to snapshot. Call after capture_snapshot."""
        try:
            vol = await _fetch_btc_vol_1h()
            snapshot["btc_volatility_1h"] = vol
        except Exception:
            pass

    def record_chunk(
        self,
        execution_id: str,
        action: str,
        chunk_index: int,
        snapshot: dict,
        chunk_result: Any,
        config: Any,
        chase_rounds: int = 0,
        pair: str = "",
    ) -> None:
        """Merge snapshot with fill result and queue for push."""
        if not self._enabled:
            return

        mid_m = snapshot.get("snapshot_mid_maker") or 0
        mid_t = snapshot.get("snapshot_mid_taker") or 0
        fill_maker = chunk_result.maker_price if chunk_result else 0
        fill_taker = chunk_result.taker_price if chunk_result else 0

        slip_maker = None
        if mid_m > 0 and fill_maker > 0:
            slip_maker = round((fill_maker - mid_m) / mid_m * 10000, 2)
        slip_taker = None
        if mid_t > 0 and fill_taker > 0:
            slip_taker = round((fill_taker - mid_t) / mid_t * 10000, 2)

        entry: dict[str, Any] = {
            # Identity
            "execution_id": execution_id,
            "chunk_index": chunk_index,
            "action": action,
            "timestamp_ms": snapshot.get("timestamp_ms", time.time() * 1000),
            "bot_id": self._bot_id,
            "pair": pair,
            "exchange_maker": config.maker_exchange if config else "",
            "exchange_taker": config.taker_exchange if config else "",
            "instrument_maker": config.maker_symbol if config else "",
            "instrument_taker": config.taker_symbol if config else "",
            "maker_side": config.maker_side if config else "",
            # Snapshot (from capture_snapshot)
            **{k: v for k, v in snapshot.items() if k.startswith("snapshot_")},
            # Funding + context
            "funding_rate_long": snapshot.get("funding_rate_long"),
            "funding_rate_short": snapshot.get("funding_rate_short"),
            "funding_spread": snapshot.get("funding_spread"),
            "v4_spread_consistency": snapshot.get("v4_spread_consistency"),
            "v4_confidence_score": snapshot.get("v4_confidence_score"),
            "hour_of_day": snapshot.get("hour_of_day"),
            "day_of_week": snapshot.get("day_of_week"),
            "btc_volatility_1h": snapshot.get("btc_volatility_1h"),
            # Fill result
            "target_qty": float(chunk_result.maker_filled_qty + chunk_result.taker_filled_qty) / 2 if chunk_result else None,
            "filled_qty_maker": chunk_result.maker_filled_qty if chunk_result else None,
            "filled_qty_taker": chunk_result.taker_filled_qty if chunk_result else None,
            "fill_price_maker": fill_maker if fill_maker else None,
            "fill_price_taker": fill_taker if fill_taker else None,
            "realized_slippage_maker_bps": slip_maker,
            "realized_slippage_taker_bps": slip_taker,
            "chase_rounds": chase_rounds,
            "chunk_duration_s": round(chunk_result.end_ts - chunk_result.start_ts, 2) if chunk_result and chunk_result.end_ts else None,
            "success": 1 if chunk_result and not chunk_result.error else 0,
            "error": chunk_result.error if chunk_result else None,
            # Config
            "use_depth_spread": 1 if config and config.use_depth_spread else 0,
            "taker_drift_guard": 1 if config and config.taker_drift_guard else 0,
            "max_slippage_bps_cfg": config.max_slippage_bps if config else None,
            "maker_timeout_ms": config.maker_timeout_ms if config else None,
            "reduce_only": 1 if config and config.reduce_only else 0,
            "simulation": 1 if config and config.simulation else 0,
        }

        self._buffer.append(entry)
        logger.debug("Execution log queued: exec=%s chunk=%d action=%s", execution_id, chunk_index, action)

        # Auto-flush if buffer is large
        if len(self._buffer) >= 10:
            asyncio.create_task(self._flush())

    async def _flush_loop(self) -> None:
        """Periodically flush buffered entries."""
        while self._running:
            try:
                await asyncio.sleep(30)
                if self._buffer:
                    await self._flush()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("ExecutionLogger flush loop error: %s", exc)

    async def _flush(self) -> None:
        """POST buffered entries to CF Worker."""
        if not self._buffer:
            return

        entries = self._buffer[:]
        self._buffer.clear()

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    self._ingest_url,
                    json={"entries": entries},
                    headers={"Authorization": f"Bearer {self._ingest_token}"},
                )
                if resp.status_code == 200:
                    logger.info("Execution log pushed: %d entries", len(entries))
                else:
                    logger.warning("Execution log push failed: %d %s", resp.status_code, resp.text[:200])
                    # Re-add failed entries to buffer (with limit)
                    if len(self._buffer) < 100:
                        self._buffer.extend(entries)
        except Exception as exc:
            logger.warning("Execution log push error: %s", exc)
            if len(self._buffer) < 100:
                self._buffer.extend(entries)
