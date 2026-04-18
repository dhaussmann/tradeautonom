"""Activity Log Forwarder — pushes bot activity events to Cloudflare Workers
Analytics Engine via the CF Worker ingest endpoint.

Fire-and-forget: failures are logged but never affect bot operation.
Events are batched and flushed every few seconds or when the batch is full.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger("tradeautonom.activity_forwarder")

_FLUSH_INTERVAL_S = 5
_MAX_BATCH_SIZE = 50


class ActivityLogForwarder:
    """Singleton-style forwarder that batches activity events and POSTs them."""

    def __init__(self, ingest_url: str, ingest_token: str) -> None:
        self._ingest_url = ingest_url
        self._ingest_token = ingest_token
        self._container = os.environ.get("HOSTNAME", "unknown")
        self._port = os.environ.get("APP_PORT", "0")
        self._user_id = os.environ.get("USER_ID", "")
        self._buffer: list[dict[str, Any]] = []
        self._task: asyncio.Task | None = None
        self._running = False

    def start(self) -> None:
        """Start the background flush loop."""
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._flush_loop())
        logger.info(
            "ActivityLogForwarder started (url=%s, container=%s, port=%s)",
            self._ingest_url, self._container, self._port,
        )

    def stop(self) -> None:
        """Stop the background flush loop and flush remaining events."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    def forward(self, event: str, message: str, bot_type: str, bot_id: str) -> None:
        """Queue an activity event for forwarding. Non-blocking."""
        entry = {
            "ts": time.time(),
            "event": event,
            "message": message,
            "bot_type": bot_type,
            "bot_id": bot_id,
            "container": self._container,
            "port": self._port,
            "user_id": self._user_id,
        }
        self._buffer.append(entry)

        # Flush immediately if batch is full
        if len(self._buffer) >= _MAX_BATCH_SIZE and self._running:
            asyncio.create_task(self._flush())

    async def _flush_loop(self) -> None:
        """Periodically flush buffered events."""
        while self._running:
            try:
                await asyncio.sleep(_FLUSH_INTERVAL_S)
                await self._flush()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Activity flush loop error: %s", exc)

        # Final flush on shutdown
        await self._flush()

    async def _flush(self) -> None:
        """Send buffered events to the CF Worker."""
        if not self._buffer:
            return

        batch = self._buffer[:_MAX_BATCH_SIZE]
        self._buffer = self._buffer[_MAX_BATCH_SIZE:]

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(
                    self._ingest_url,
                    json={"events": batch},
                    headers={"Authorization": f"Bearer {self._ingest_token}"},
                )
                if resp.status_code != 200:
                    logger.debug("Activity ingest HTTP %s: %s", resp.status_code, resp.text[:200])
        except Exception as exc:
            logger.debug("Activity ingest failed: %s", exc)
            # Re-queue failed batch (up to a limit to avoid memory leak)
            if len(self._buffer) < 500:
                self._buffer = batch + self._buffer
