"""Journal Collector — background task that fetches order/fill/funding/points
history from all exchanges and pushes them to the Cloudflare D1 database
via the CF Worker journal ingest endpoint.

Runs alongside the existing _history_ingest_loop (equity/position snapshots).
Bot-assignment is done by matching orders to running bots via time-window + instrument.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger("tradeautonom.journal_collector")

# How often to poll exchanges for new data (seconds)
_DEFAULT_INTERVAL_S = 300  # 5 minutes

# Backfill start: April 1st 2025 00:00 UTC
_BACKFILL_SINCE_MS = 1743465600000
_BACKFILL_LIMIT = 5000


class JournalCollector:
    """Periodically collects order/fill/funding/points data and ingests to D1."""

    def __init__(
        self,
        exchange_clients: dict[str, Any],
        bot_registry: Any | None,
        ingest_url: str,
        ingest_token: str,
        interval_s: int = _DEFAULT_INTERVAL_S,
    ) -> None:
        self._clients = exchange_clients
        self._bot_registry = bot_registry
        self._ingest_url = ingest_url
        self._ingest_token = ingest_token
        self._interval_s = interval_s
        self._task: asyncio.Task | None = None

        # Track last-synced timestamp per exchange per data type (ms)
        self._last_sync: dict[str, dict[str, int]] = {}

    def start(self) -> None:
        """Start the background collection task."""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run_loop())
        logger.info("JournalCollector started (interval=%ds)", self._interval_s)

    async def stop(self) -> None:
        """Stop the background collection task."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("JournalCollector stopped")

    def update_bot_registry(self, registry: Any) -> None:
        """Update the bot registry reference (may change after vault re-unlock)."""
        self._bot_registry = registry

    # ── Main loop ──────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        async with httpx.AsyncClient(timeout=60) as http:
            # One-time backfill on first startup
            await self._maybe_backfill(http)

            while True:
                try:
                    await asyncio.sleep(self._interval_s)
                    await self._collect_and_ingest(http)
                except asyncio.CancelledError:
                    logger.info("JournalCollector task cancelled")
                    return
                except Exception as exc:
                    logger.warning("JournalCollector error: %s", exc, exc_info=True)
                    await asyncio.sleep(30)

    async def _maybe_backfill(self, http: httpx.AsyncClient) -> None:
        """Run a one-time backfill from April 1st if _last_sync is empty (first startup)."""
        if self._last_sync:
            return
        if not self._clients:
            return

        logger.info("JournalCollector: starting initial backfill from %s", _BACKFILL_SINCE_MS)

        user_id = os.environ.get("USER_ID", "")
        all_orders: list[dict] = []
        all_fills: list[dict] = []
        all_funding: list[dict] = []
        all_points: list[dict] = []

        for exchange_name, client in self._clients.items():
            try:
                orders, fills, funding, points = await self._fetch_exchange_history(
                    exchange_name, client, since_ms=_BACKFILL_SINCE_MS, limit=_BACKFILL_LIMIT,
                )
                orders = self._filter_orders(orders)

                bot_map = self._build_bot_time_map()
                for o in orders:
                    o["bot_id"] = self._match_bot(bot_map, o)
                for f in fills:
                    f["bot_id"] = self._match_bot(bot_map, f)
                for fp in funding:
                    fp["bot_id"] = self._match_bot(bot_map, fp)

                all_orders.extend(orders)
                all_fills.extend(fills)
                all_funding.extend(funding)
                all_points.extend(points)

                logger.info(
                    "JournalCollector backfill %s: orders=%d fills=%d funding=%d points=%d",
                    exchange_name, len(orders), len(fills), len(funding), len(points),
                )

                # Mark synced so regular polling takes over
                now_ms = int(time.time() * 1000)
                self._set_last_sync(exchange_name, "orders", now_ms)

            except Exception as exc:
                logger.warning("JournalCollector backfill %s error: %s", exchange_name, exc, exc_info=True)

        # Push to D1
        if all_orders or all_fills or all_funding or all_points:
            payload = {
                "user_id": user_id,
                "orders": all_orders,
                "fills": all_fills,
                "funding_payments": all_funding,
                "points": all_points,
                "timestamp": time.time(),
            }
            try:
                resp = await http.post(
                    self._ingest_url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._ingest_token}"},
                )
                if resp.status_code == 200:
                    result = resp.json()
                    logger.info(
                        "JournalCollector backfill ingest OK: orders=%s fills=%s funding=%s points=%s",
                        result.get("orders_upserted", 0),
                        result.get("fills_upserted", 0),
                        result.get("funding_upserted", 0),
                        result.get("points_upserted", 0),
                    )
                else:
                    logger.warning("JournalCollector backfill ingest failed: %s %s", resp.status_code, resp.text[:300])
            except Exception as exc:
                logger.warning("JournalCollector backfill ingest HTTP error: %s", exc)
        else:
            logger.info("JournalCollector backfill: no data found from any exchange")

    async def _collect_and_ingest(self, http: httpx.AsyncClient) -> None:
        """Fetch new data from all exchanges and push to D1."""
        if not self._clients:
            return

        user_id = os.environ.get("USER_ID", "")
        all_orders: list[dict] = []
        all_fills: list[dict] = []
        all_funding: list[dict] = []
        all_points: list[dict] = []

        for exchange_name, client in self._clients.items():
            since_ms = self._get_last_sync(exchange_name, "orders")

            try:
                orders, fills, funding, points = await self._fetch_exchange_history(
                    exchange_name, client, since_ms,
                )
                orders = self._filter_orders(orders)

                # Assign bot_ids based on time + instrument matching
                bot_map = self._build_bot_time_map()
                for o in orders:
                    o["bot_id"] = self._match_bot(bot_map, o)
                for f in fills:
                    f["bot_id"] = self._match_bot(bot_map, f)
                for fp in funding:
                    fp["bot_id"] = self._match_bot(bot_map, fp)

                all_orders.extend(orders)
                all_fills.extend(fills)
                all_funding.extend(funding)
                all_points.extend(points)

                # Update last sync timestamp
                if orders or fills or funding:
                    now_ms = int(time.time() * 1000)
                    self._set_last_sync(exchange_name, "orders", now_ms)

            except Exception as exc:
                logger.warning("JournalCollector: %s fetch error: %s", exchange_name, exc)

        # Push to D1 via CF Worker
        if all_orders or all_fills or all_funding or all_points:
            payload = {
                "user_id": user_id,
                "orders": all_orders,
                "fills": all_fills,
                "funding_payments": all_funding,
                "points": all_points,
                "timestamp": time.time(),
            }
            try:
                resp = await http.post(
                    self._ingest_url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._ingest_token}"},
                )
                if resp.status_code == 200:
                    result = resp.json()
                    logger.info(
                        "Journal ingest OK: orders=%s fills=%s funding=%s points=%s",
                        result.get("orders_upserted", 0),
                        result.get("fills_upserted", 0),
                        result.get("funding_upserted", 0),
                        result.get("points_upserted", 0),
                    )
                else:
                    logger.warning("Journal ingest failed: %s %s", resp.status_code, resp.text[:300])
            except Exception as exc:
                logger.warning("Journal ingest HTTP error: %s", exc)

    # ── Exchange-specific fetching ─────────────────────────────────────

    async def _fetch_exchange_history(
        self, exchange_name: str, client: Any, since_ms: int | None,
        limit: int = 500,
    ) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
        """Fetch orders, fills, funding, and points from a single exchange.

        Returns: (orders, fills, funding_payments, points)
        """
        orders: list[dict] = []
        fills: list[dict] = []
        funding: list[dict] = []
        points: list[dict] = []

        if exchange_name == "extended":
            orders = await self._safe_call(client.async_fetch_order_history, since_ms=since_ms, limit=limit)
            fills = await self._safe_call(client.async_fetch_trade_history, since_ms=since_ms, limit=limit)
            funding = await self._safe_call(client.async_fetch_funding_payments, since_ms=since_ms, limit=limit)
            points = await self._safe_call(client.async_fetch_points)

        elif exchange_name == "grvt":
            orders = await self._safe_call(client.async_fetch_order_history, since_ms=since_ms, limit=limit)
            fills = await self._safe_call(client.async_fetch_fill_history, since_ms=since_ms, limit=limit)
            funding = await self._safe_call(client.async_fetch_funding_payments, since_ms=since_ms, limit=limit)

        elif exchange_name == "variational":
            # Skip — reduce Cloudflare 403 trigger rate
            pass

        else:
            logger.debug("JournalCollector: no history support for %s", exchange_name)

        return orders, fills, funding, points

    @staticmethod
    async def _safe_call(fn, **kwargs) -> list[dict]:
        """Call an async method safely, returning [] on error."""
        try:
            return await fn(**kwargs)
        except Exception as exc:
            logger.warning("JournalCollector safe_call error (%s): %s", fn.__name__, exc)
            return []

    _ACCEPTED_ORDER_STATUSES = {"FILLED", "PARTIALLY_FILLED", "PARTIAL_FILL"}

    @classmethod
    def _filter_orders(cls, orders: list[dict]) -> list[dict]:
        """Keep only filled orders — drop CANCELLED, OPEN, REJECTED, etc."""
        filtered = [o for o in orders if o.get("status", "") in cls._ACCEPTED_ORDER_STATUSES]
        dropped = len(orders) - len(filtered)
        if dropped:
            logger.info("Filtered out %d non-filled orders (kept %d)", dropped, len(filtered))
        return filtered

    # ── Bot matching (time-window + instrument) ────────────────────────

    def _build_bot_time_map(self) -> list[dict]:
        """Build a list of bot time-windows for matching.

        Each entry: {bot_id, instruments: set[str], started_at_ms, stopped_at_ms}
        """
        if not self._bot_registry:
            return []

        bot_map: list[dict] = []
        try:
            for bot_id in self._bot_registry.bot_ids:
                engine = self._bot_registry.get_bot(bot_id)
                config = engine._config
                sm = engine._state_machine

                instruments = set()
                if config.instrument_a:
                    instruments.add(config.instrument_a)
                if config.instrument_b:
                    instruments.add(config.instrument_b)

                started_at = 0
                stopped_at = int(time.time() * 1000)
                if sm:
                    started_at = int(getattr(sm, '_started_at', 0) * 1000) if getattr(sm, '_started_at', 0) else 0
                    if hasattr(sm, '_stopped_at') and sm._stopped_at:
                        stopped_at = int(sm._stopped_at * 1000)

                bot_map.append({
                    "bot_id": bot_id,
                    "instruments": instruments,
                    "started_at_ms": started_at,
                    "stopped_at_ms": stopped_at,
                })
        except Exception as exc:
            logger.debug("JournalCollector: bot_map build error: %s", exc)

        return bot_map

    @staticmethod
    def _match_bot(bot_map: list[dict], record: dict) -> str | None:
        """Match an order/fill/funding record to a bot by instrument + time overlap."""
        instrument = record.get("instrument", "")
        ts = record.get("created_at", record.get("paid_at", 0))

        if not instrument or not ts:
            return None

        for bot in bot_map:
            if instrument in bot["instruments"]:
                if bot["started_at_ms"] <= ts <= bot["stopped_at_ms"]:
                    return bot["bot_id"]
        return None

    # ── Sync tracking ──────────────────────────────────────────────────

    def _get_last_sync(self, exchange: str, data_type: str) -> int | None:
        return self._last_sync.get(exchange, {}).get(data_type)

    def _set_last_sync(self, exchange: str, data_type: str, ts_ms: int) -> None:
        if exchange not in self._last_sync:
            self._last_sync[exchange] = {}
        self._last_sync[exchange][data_type] = ts_ms
