"""Unified async data ingestion layer for the Funding-Arb engine.

Manages real-time feeds for:
  - Orderbook snapshots (per exchange + symbol) via WebSocket
  - Funding rate snapshots (per exchange + symbol) via WebSocket

All data is cached in-memory with asyncio.Lock for safe concurrent access.
Stale detection + auto-reconnect is handled by the subscription tasks.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import ssl as _ssl
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

logger = logging.getLogger("tradeautonom.data_layer")

# ── SSL context (shared, certs not verified for exchange WS) ──────────
_SSL_CTX = _ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = _ssl.CERT_NONE

# ── GRVT WS endpoints ────────────────────────────────────────────────
_GRVT_WS_ENDPOINTS = {
    "dev":     "wss://market-data.dev.gravitymarkets.io/ws/full",
    "staging": "wss://market-data.stg.gravitymarkets.io/ws/full",
    "testnet": "wss://market-data.testnet.grvt.io/ws/full",
    "prod":    "wss://market-data.grvt.io/ws/full",
}

_X18_FLOAT = 1e18


@dataclass
class OrderbookSnapshot:
    """Cached orderbook state for one exchange + symbol."""
    bids: list[list] = field(default_factory=list)  # [[price, qty], ...]
    asks: list[list] = field(default_factory=list)
    timestamp_ms: float = 0.0
    is_synced: bool = False
    connected: bool = False
    last_seq: int = -1
    update_count: int = 0


@dataclass
class FundingRateSnapshot:
    """Cached funding rate for one exchange + symbol."""
    funding_rate: float = 0.0
    timestamp: str = ""
    update_time_ms: float = 0.0


@dataclass
class PositionSnapshot:
    """Cached position state for one exchange + symbol."""
    size: float = 0.0          # Absolute position size (always >= 0)
    side: str = ""             # "long", "short", or "" if no position
    entry_price: float = 0.0
    unrealized_pnl: float = 0.0
    timestamp_ms: float = 0.0  # When this snapshot was last updated
    connected: bool = False
    update_count: int = 0


class DataLayer:
    """Single async manager for all real-time data feeds.

    Usage:
        dl = DataLayer()
        await dl.start(clients, symbols_map)
        # ... use dl.get_orderbook(), dl.get_funding_rate(), etc.
        await dl.stop()
    """

    def __init__(self, stale_ms: int = 5000, shared_monitor_url: str = "") -> None:
        self._stale_ms = stale_ms
        self._shared_monitor_url = shared_monitor_url.rstrip("/") if shared_monitor_url else ""

        # Caches keyed by (exchange_name, symbol)
        self._orderbooks: dict[tuple[str, str], OrderbookSnapshot] = {}
        self._funding_rates: dict[tuple[str, str], FundingRateSnapshot] = {}
        self._positions: dict[tuple[str, str], PositionSnapshot] = {}

        # Locks for safe concurrent access
        self._ob_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._fr_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._pos_locks: dict[tuple[str, str], asyncio.Lock] = {}

        # Background subscription tasks
        self._tasks: list[asyncio.Task] = []
        self._running = False

        # Notification events for real-time subscribers (e.g. WebSocket endpoints)
        self._ob_changed: asyncio.Event = asyncio.Event()
        self._pos_changed: asyncio.Event = asyncio.Event()

        # Clients + symbols stored for WS subscriptions
        self._clients: dict[str, Any] = {}
        self._symbols_map: dict[str, str] = {}  # Deprecated: use _symbols_list for multiple symbols per exchange
        self._symbols_list: list[tuple[str, str]] = []  # [(exchange, symbol), ...]

        # OMS WebSocket state (shared connection for all symbols)
        self._oms_ws_active = False  # True while OMS WS is connected
        self._oms_ws_task: asyncio.Task | None = None
        self._oms_ws: Any | None = None  # WebSocket client protocol
        
        # Dynamic subscription tracking
        self._pending_subscriptions: dict[str, asyncio.Event] = {}  # symbol_key -> Event
        self._subscription_timeout: float = 5.0  # seconds to wait for ack

    # ── Public API ────────────────────────────────────────────────────

    async def start(
        self,
        clients: dict[str, Any],
        symbols_map: dict[str, str],
    ) -> None:
        """Start all data feed subscriptions.

        Args:
            clients: {exchange_name: client_instance} — must implement AsyncExchangeClient
            symbols_map: {exchange_name: symbol} — which symbol to track per exchange
        """
        self._running = True
        self._clients = clients
        self._symbols_map = symbols_map
        # Convert dict to list for internal use (supports multiple symbols per exchange)
        self._symbols_list = list(symbols_map.items())

        for exch_name, symbol in self._symbols_list:
            client = clients.get(exch_name)
            if client is None:
                logger.warning("DataLayer: no client for exchange '%s', skipping", exch_name)
                continue

            key = (exch_name, symbol)
            self._orderbooks[key] = OrderbookSnapshot()
            self._funding_rates[key] = FundingRateSnapshot()
            self._positions[key] = PositionSnapshot()
            self._ob_locks[key] = asyncio.Lock()
            self._fr_locks[key] = asyncio.Lock()
            self._pos_locks[key] = asyncio.Lock()

            # Start funding rate WS subscription
            if hasattr(client, "async_subscribe_funding_rate"):
                task = asyncio.create_task(
                    self._run_funding_subscription(client, exch_name, symbol),
                    name=f"funding-{exch_name}-{symbol}",
                )
                self._tasks.append(task)
                logger.info("DataLayer: started funding rate feed for %s:%s", exch_name, symbol)

            # Start position WS/poll subscription
            task = asyncio.create_task(
                self._run_position_subscription(client, exch_name, symbol),
                name=f"pos-{exch_name}-{symbol}",
            )
            self._tasks.append(task)
            logger.info("DataLayer: started position feed for %s:%s", exch_name, symbol)

        # Start OMS WS (shared connection) or per-symbol orderbook feeds
        if self._shared_monitor_url:
            self._oms_ws_task = asyncio.create_task(
                self._run_oms_ws(), name="oms-ws-shared",
            )
            self._tasks.append(self._oms_ws_task)
            logger.info("DataLayer: started OMS WS feed for %d symbols", len(symbols_map))
        else:
            for exch_name, symbol in symbols_map.items():
                client = clients.get(exch_name)
                if client is None:
                    continue
                task = asyncio.create_task(
                    self._run_orderbook_ws(client, exch_name, symbol),
                    name=f"ob-ws-{exch_name}-{symbol}",
                )
                self._tasks.append(task)
                logger.info("DataLayer: started orderbook WS feed for %s:%s", exch_name, symbol)

        logger.info("DataLayer started: %d feeds", len(self._tasks))

    async def stop(self) -> None:
        """Cancel all background tasks."""
        self._running = False
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("DataLayer stopped")

    def get_orderbook(self, exchange: str, symbol: str) -> OrderbookSnapshot:
        """Return the cached orderbook snapshot (non-async, lock-free read)."""
        return self._orderbooks.get((exchange, symbol), OrderbookSnapshot())

    def get_funding_rate(self, exchange: str, symbol: str) -> FundingRateSnapshot:
        """Return the cached funding rate snapshot (non-async, lock-free read)."""
        return self._funding_rates.get((exchange, symbol), FundingRateSnapshot())

    def get_books_atomic(
        self,
        exch_a: str, sym_a: str,
        exch_b: str, sym_b: str,
    ) -> tuple[OrderbookSnapshot, OrderbookSnapshot]:
        """Return both orderbook snapshots. Lock-free for read (dataclass replace is atomic)."""
        return (
            self.get_orderbook(exch_a, sym_a),
            self.get_orderbook(exch_b, sym_b),
        )

    def is_orderbook_fresh(self, exchange: str, symbol: str) -> bool:
        """Check if the orderbook snapshot is fresh (within stale_ms)."""
        snap = self._orderbooks.get((exchange, symbol))
        if snap is None or not snap.is_synced:
            return False
        age_ms = (time.time() * 1000) - snap.timestamp_ms
        return age_ms < self._stale_ms

    def is_funding_rate_fresh(self, exchange: str, symbol: str) -> bool:
        """Check if the funding rate snapshot is fresh."""
        snap = self._funding_rates.get((exchange, symbol))
        if snap is None:
            return False
        age_ms = (time.time() * 1000) - snap.update_time_ms
        return age_ms < 120_000  # 2 minutes tolerance for funding rates

    def is_ready(self) -> bool:
        """Check if all orderbook feeds are connected, synced, and have data."""
        if not self._orderbooks:
            return False
        for snap in self._orderbooks.values():
            if not snap.connected or not snap.is_synced:
                return False
            if not snap.bids or not snap.asks:
                return False
        return True

    async def add_symbols(self, symbols_map: dict[str, str]) -> dict[str, bool]:
        """Dynamically add new symbols to the DataLayer and subscribe via OMS.
        
        Waits for OMS acknowledgment before returning.
        Returns dict of symbol_key -> success (True if subscribed or already active).
        """
        if not self._shared_monitor_url:
            logger.warning("DataLayer: Cannot add symbols - no OMS URL configured")
            return {}
        
        results: dict[str, bool] = {}
        
        for exch_name, symbol in symbols_map.items():
            key = (exch_name, symbol)
            symbol_key = f"{exch_name}:{symbol}"
            
            # Check if already subscribed and connected
            if key in self._orderbooks:
                snap = self._orderbooks[key]
                if snap.connected and snap.is_synced:
                    results[symbol_key] = True
                    continue
            
            # Initialize cache structures
            if key not in self._orderbooks:
                self._orderbooks[key] = OrderbookSnapshot()
            if key not in self._ob_locks:
                self._ob_locks[key] = asyncio.Lock()
            
            # Track in symbols list (supports multiple symbols per exchange)
            if (exch_name, symbol) not in self._symbols_list:
                self._symbols_list.append((exch_name, symbol))
            
            # Try to subscribe via OMS
            if self._oms_ws_active and self._oms_ws:
                ack_event = asyncio.Event()
                self._pending_subscriptions[symbol_key] = ack_event
                
                try:
                    sub_msg = json.dumps({
                        "action": "subscribe",
                        "exchange": exch_name,
                        "symbol": symbol
                    })
                    await self._oms_ws.send(sub_msg)
                    logger.info("DataLayer: Subscribing to %s:%s via OMS", exch_name, symbol)
                    
                    # Wait for acknowledgment
                    await asyncio.wait_for(ack_event.wait(), timeout=self._subscription_timeout)
                    results[symbol_key] = True
                    logger.info("DataLayer: OMS subscribed %s:%s", exch_name, symbol)
                except asyncio.TimeoutError:
                    results[symbol_key] = False
                    logger.warning("DataLayer: OMS subscribe timeout for %s:%s", exch_name, symbol)
                except Exception as exc:
                    results[symbol_key] = False
                    logger.error("DataLayer: OMS subscribe error for %s:%s: %s", exch_name, symbol, exc)
                finally:
                    self._pending_subscriptions.pop(symbol_key, None)
            else:
                # OMS not connected - start HTTP polling for this symbol
                logger.warning("DataLayer: OMS not connected, starting HTTP poll for %s:%s", exch_name, symbol)
                task = asyncio.create_task(
                    self._run_ob_oms_poll(exch_name, symbol),
                    name=f"ob-oms-poll-{exch_name}-{symbol}",
                )
                self._tasks.append(task)
                
                # Don't wait - let the poll run in background
                # Mark as success since poll task is running
                results[symbol_key] = True
                logger.info("DataLayer: HTTP poll started for %s:%s (running in background)", exch_name, symbol)
        
        return results

    async def remove_symbols(self, symbols_map: dict[str, str]) -> None:
        """Unsubscribe from symbols when bot is deleted."""
        if not self._shared_monitor_url or not self._oms_ws_active or not self._oms_ws:
            return
        
        for exch_name, symbol in symbols_map.items():
            key = (exch_name, symbol)
            
            # Send unsubscribe message
            try:
                unsub_msg = json.dumps({
                    "action": "unsubscribe",
                    "exchange": exch_name,
                    "symbol": symbol
                })
                await self._oms_ws.send(unsub_msg)
                logger.info("DataLayer: OMS unsubscribed %s:%s", exch_name, symbol)
            except Exception as exc:
                logger.warning("DataLayer: Failed to unsubscribe %s:%s: %s", exch_name, symbol, exc)
            
            # Remove from cache
            if key in self._orderbooks:
                del self._orderbooks[key]
            self._ob_locks.pop(key, None)
            
            # Remove from symbols list
            if (exch_name, symbol) in self._symbols_list:
                self._symbols_list.remove((exch_name, symbol))

    def get_orderbook_depth(self, exchange: str, symbol: str, depth: int = 10) -> dict:
        """Return top N orderbook levels as a serializable dict for the UI."""
        snap = self._orderbooks.get((exchange, symbol), OrderbookSnapshot())
        now_ms = time.time() * 1000
        age_ms = round(now_ms - snap.timestamp_ms) if snap.timestamp_ms else None
        return {
            "exchange": exchange,
            "instrument": symbol,
            "bids": [[lvl[0], lvl[1]] for lvl in snap.bids[:depth]],
            "asks": [[lvl[0], lvl[1]] for lvl in snap.asks[:depth]],
            "synced": snap.is_synced,
            "connected": snap.connected,
            "age_ms": age_ms,
            "updates": snap.update_count,
            "source": "ws" if snap.connected else "none",
        }

    def get_feed_status(self) -> dict[str, dict]:
        """Return per-feed connection status for the UI."""
        result = {}
        now_ms = time.time() * 1000
        for (exch, sym), snap in self._orderbooks.items():
            age_ms = round(now_ms - snap.timestamp_ms) if snap.timestamp_ms else None
            result[f"{exch}:{sym}"] = {
                "connected": snap.connected,
                "synced": snap.is_synced,
                "has_data": bool(snap.bids and snap.asks),
                "age_ms": age_ms,
                "updates": snap.update_count,
            }
        return result

    def get_orderbook_health(self, exchange: str, symbol: str) -> dict:
        """Compute Orderbook Health Index (OHI) for one exchange+symbol.

        OHI = weighted combination of:
          - Spread tightness (40%): normalized bid-ask spread
          - Depth score (30%): total liquidity within 0.5% of mid
          - Symmetry (30%): bid/ask depth ratio (1.0 = perfectly symmetric)

        Returns dict with ohi (0-1), spread_bps, depth_usd, symmetry, components.
        """
        snap = self._orderbooks.get((exchange, symbol), OrderbookSnapshot())
        if not snap.bids or not snap.asks:
            return {"ohi": 0.0, "spread_bps": 0.0, "depth_usd": 0.0, "symmetry": 0.0}

        best_bid = float(snap.bids[0][0])
        best_ask = float(snap.asks[0][0])
        if best_bid <= 0 or best_ask <= 0:
            return {"ohi": 0.0, "spread_bps": 0.0, "depth_usd": 0.0, "symmetry": 0.0}

        mid = (best_bid + best_ask) / 2.0
        spread_bps = (best_ask - best_bid) / mid * 10000

        # Depth within 0.5% of mid
        depth_range = mid * 0.005
        bid_depth = sum(float(b[0]) * float(b[1]) for b in snap.bids if float(b[0]) >= mid - depth_range)
        ask_depth = sum(float(a[0]) * float(a[1]) for a in snap.asks if float(a[0]) <= mid + depth_range)
        total_depth_usd = bid_depth + ask_depth

        # Symmetry: ratio of smaller/larger side (1.0 = perfect)
        if bid_depth > 0 and ask_depth > 0:
            symmetry = min(bid_depth, ask_depth) / max(bid_depth, ask_depth)
        else:
            symmetry = 0.0

        # Component scores (each 0-1)
        # Spread: 0 bps → 1.0, 50+ bps → 0.0
        spread_score = max(0.0, 1.0 - spread_bps / 50.0)
        # Depth: $100k+ → 1.0, $0 → 0.0 (log scale)
        depth_score = min(1.0, math.log1p(total_depth_usd) / math.log1p(100_000))
        # Symmetry already 0-1
        symmetry_score = symmetry

        ohi = 0.4 * spread_score + 0.3 * depth_score + 0.3 * symmetry_score

        return {
            "ohi": round(ohi, 4),
            "spread_bps": round(spread_bps, 2),
            "depth_usd": round(total_depth_usd, 2),
            "symmetry": round(symmetry, 4),
            "spread_score": round(spread_score, 4),
            "depth_score": round(depth_score, 4),
            "symmetry_score": round(symmetry_score, 4),
        }

    # ── Position cache API ─────────────────────────────────────────────

    def get_position(self, exchange: str, symbol: str) -> PositionSnapshot:
        """Return the cached position snapshot (non-async, lock-free read)."""
        return self._positions.get((exchange, symbol), PositionSnapshot())

    def is_position_fresh(self, exchange: str, symbol: str, max_age_ms: float = 3000) -> bool:
        """Check if the position snapshot is fresh (within max_age_ms)."""
        snap = self._positions.get((exchange, symbol))
        if snap is None or not snap.connected:
            return False
        age_ms = (time.time() * 1000) - snap.timestamp_ms
        return age_ms < max_age_ms

    # ── Internal subscription loops ───────────────────────────────────

    async def _run_funding_subscription(self, client, exch_name: str, symbol: str) -> None:
        """Run a funding rate WS subscription with auto-reconnect."""
        key = (exch_name, symbol)

        async def _on_funding_update(data: dict) -> None:
            async with self._fr_locks[key]:
                snap = self._funding_rates[key]
                snap.funding_rate = data.get("funding_rate", 0.0)
                snap.timestamp = data.get("timestamp", "")
                snap.update_time_ms = time.time() * 1000

        while self._running:
            try:
                await client.async_subscribe_funding_rate(symbol, _on_funding_update)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("DataLayer: funding sub %s:%s error: %s — retrying in 5s", exch_name, symbol, exc)
                await asyncio.sleep(5)

    # ── Orderbook WS streams ─────────────────────────────────────────

    async def _run_orderbook_ws(self, client, exch_name: str, symbol: str) -> None:
        """Route to the correct exchange-specific WS orderbook handler."""
        if exch_name == "extended":
            await self._run_ob_ws_extended(symbol)
        elif exch_name == "grvt":
            grvt_env = getattr(getattr(client, "settings", None), "grvt_env", "prod")
            await self._run_ob_ws_grvt(symbol, grvt_env)
        elif exch_name == "variational":
            logger.info("DataLayer: Variational has no WS orderbook — using REST polling for %s", symbol)
            await self._run_ob_rest_fallback(client, exch_name, symbol)
        elif exch_name == "nado":
            nado_env = getattr(client, "_env", "mainnet")
            product_id = client._get_product_id(symbol) if hasattr(client, "_get_product_id") else None
            if product_id is None:
                logger.error("DataLayer: cannot resolve NADO product_id for %s — falling back to REST", symbol)
                await self._run_ob_rest_fallback(client, exch_name, symbol)
                return
            await self._run_ob_ws_nado(symbol, product_id, nado_env)
        else:
            logger.warning("DataLayer: unknown exchange '%s' — using REST fallback", exch_name)
            await self._run_ob_rest_fallback(client, exch_name, symbol)

    async def _run_ob_rest_fallback(self, client, exch_name: str, symbol: str) -> None:
        """Fallback: poll orderbook via REST if WS not available."""
        key = (exch_name, symbol)
        while self._running:
            try:
                book = await client.async_fetch_order_book(symbol, limit=20)
                async with self._ob_locks[key]:
                    snap = self._orderbooks[key]
                    snap.bids = book.get("bids", [])
                    snap.asks = book.get("asks", [])
                    snap.timestamp_ms = time.time() * 1000
                    snap.is_synced = True
                    snap.connected = True
                    snap.update_count += 1
                    self._ob_changed.set()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("DataLayer: OB REST fallback %s:%s error: %s", exch_name, symbol, exc)
                async with self._ob_locks[key]:
                    self._orderbooks[key].is_synced = False
                    self._orderbooks[key].connected = False
            await asyncio.sleep(1.0)

    async def _run_ob_oms_poll(self, exch_name: str, symbol: str) -> None:
        """Poll orderbook from the shared Orderbook Monitor Service (OMS).

        Falls back to direct WS if OMS is unreachable for too long.
        """
        key = (exch_name, symbol)
        url = f"{self._shared_monitor_url}/book/{exch_name}/{symbol}"
        consecutive_errors = 0
        max_errors_before_fallback = 10

        logger.info("DataLayer: OMS poll starting for %s:%s from %s", exch_name, symbol, url)

        logger.info("DataLayer: OMS poll loop started for %s:%s (running=%s)", exch_name, symbol, self._running)
        first_success = False
        loop_count = 0
        while self._running:
            loop_count += 1
            if loop_count == 1:
                logger.info("DataLayer: OMS poll first iteration %s:%s", exch_name, symbol)
            try:
                logger.info("DataLayer: OMS poll fetching %s:%s from %s", exch_name, symbol, url)
                # Use urllib in thread to avoid blocking event loop
                import urllib.request
                import json
                def fetch_oms():
                    logger.info("DataLayer: OMS poll [thread] fetching %s:%s", exch_name, symbol)
                    req = urllib.request.Request(url, method='GET')
                    with urllib.request.urlopen(req, timeout=3.0) as response:
                        data = json.loads(response.read().decode('utf-8'))
                        logger.info("DataLayer: OMS poll [thread] fetched %s:%s - bids=%d", exch_name, symbol, len(data.get("bids", [])))
                        return data
                
                data = await asyncio.to_thread(fetch_oms)
                logger.info("DataLayer: OMS poll got data %s:%s", exch_name, symbol)
                if not first_success:
                    logger.info("DataLayer: OMS poll first success for %s:%s - bids=%d asks=%d", 
                               exch_name, symbol, len(data.get("bids", [])), len(data.get("asks", [])))
                    first_success = True

                async with self._ob_locks[key]:
                    snap = self._orderbooks[key]
                    snap.bids = data.get("bids", [])
                    snap.asks = data.get("asks", [])
                    logger.debug("DataLayer: OMS poll updated cache %s:%s - update_count=%d", 
                               exch_name, symbol, snap.update_count)
                    snap.timestamp_ms = data.get("timestamp_ms", time.time() * 1000)
                    snap.is_synced = True
                    snap.connected = data.get("connected", True)
                    snap.update_count += 1
                    self._ob_changed.set()
                    if snap.update_count == 1:
                        logger.info("DataLayer: OMS poll first update for %s:%s - bids=%d asks=%d", 
                                  exch_name, symbol, len(snap.bids), len(snap.asks))
                consecutive_errors = 0
            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_errors += 1
                logger.warning(
                    "DataLayer: OMS poll %s:%s error (%d/%d): %s",
                    exch_name, symbol, consecutive_errors, max_errors_before_fallback, exc,
                )
                async with self._ob_locks[key]:
                    self._orderbooks[key].is_synced = False
                    self._orderbooks[key].connected = False

                if consecutive_errors >= max_errors_before_fallback:
                    logger.warning("DataLayer: OMS unreachable for %s:%s — falling back to direct WS", exch_name, symbol)
                    # Clear shared monitor URL for this feed and fall back
                    break

            await asyncio.sleep(0.5)

        # Fallback to direct WS (re-enter the routing without OMS)
        if self._running:
            logger.info("DataLayer: OMS fallback → starting direct WS for %s:%s", exch_name, symbol)
            old_url = self._shared_monitor_url
            self._shared_monitor_url = ""  # temporarily disable to avoid recursion
            client = self._clients.get(exch_name)
            if client:
                await self._run_orderbook_ws(client, exch_name, symbol)
            self._shared_monitor_url = old_url

    # ── OMS WebSocket (shared real-time connection) ─────────────────

    async def _run_oms_ws(self) -> None:
        """Shared WebSocket connection to the OMS for real-time orderbook updates.

        Subscribes to all symbols in self._symbols_list over a single WS connection.
        Includes ping keepalive and receive timeout for connection health.
        Falls back to per-symbol HTTP polling if the WS connection fails repeatedly.
        """
        base_url = self._shared_monitor_url
        ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
        reconnect_delay = 1.0
        max_reconnect_delay = 15.0
        consecutive_failures = 0
        max_failures_before_fallback = 5
        
        # Connection health settings
        ping_interval = 30.0  # Send ping every 30 seconds
        receive_timeout = 60.0  # Max time to wait for any message

        logger.info("DataLayer: OMS WS connecting to %s (symbols: %d)", ws_url, len(self._symbols_list))

        while self._running:
            try:
                async with websockets.connect(ws_url, close_timeout=5) as ws:
                    self._oms_ws = ws
                    self._oms_ws_active = True
                    reconnect_delay = 1.0
                    consecutive_failures = 0
                    logger.info("DataLayer: OMS WS connected to %s", ws_url)

                    # Subscribe to all tracked symbols (using list to support multiple per exchange)
                    for exch_name, symbol in self._symbols_list:
                        sub_msg = json.dumps({"action": "subscribe", "exchange": exch_name, "symbol": symbol})
                        await ws.send(sub_msg)
                        logger.info("DataLayer: OMS WS subscribed %s:%s", exch_name, symbol)

                    # Create tasks for receive loop and ping keepalive
                    receive_task = asyncio.create_task(self._oms_receive_loop(ws), name="oms-receive")
                    ping_task = asyncio.create_task(self._oms_ping_loop(ws, ping_interval), name="oms-ping")

                    try:
                        # Wait for either task to complete (or fail)
                        done, pending = await asyncio.wait(
                            [receive_task, ping_task],
                            return_when=asyncio.FIRST_COMPLETED
                        )

                        # Cancel remaining tasks
                        for task in pending:
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass

                        # Check if any task raised an exception
                        for task in done:
                            try:
                                task.result()
                            except websockets.ConnectionClosed:
                                logger.warning("DataLayer: OMS WS disconnected — will reconnect")
                            except asyncio.TimeoutError:
                                logger.warning("DataLayer: OMS WS receive timeout — forcing reconnect")
                            except Exception as exc:
                                logger.warning("DataLayer: OMS WS error — %s", exc)

                    finally:
                        self._oms_ws_active = False
                        self._oms_ws = None
                        # Ensure tasks are cleaned up
                        if not receive_task.done():
                            receive_task.cancel()
                        if not ping_task.done():
                            ping_task.cancel()

            except asyncio.CancelledError:
                self._oms_ws_active = False
                return
            except Exception as exc:
                self._oms_ws_active = False
                consecutive_failures += 1
                logger.warning(
                    "DataLayer: OMS WS connection error (%d/%d): %s — retry in %.0fs",
                    consecutive_failures, max_failures_before_fallback, exc, reconnect_delay,
                )

                if consecutive_failures >= max_failures_before_fallback:
                    logger.warning("DataLayer: OMS WS failed %d times — falling back to HTTP poll", consecutive_failures)
                    break

                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, max_reconnect_delay)

        # Fallback: start per-symbol OMS HTTP poll tasks
        if self._running:
            logger.info("DataLayer: OMS WS fallback → starting HTTP poll per symbol")
            for exch_name, symbol in self._symbols_list:
                task = asyncio.create_task(
                    self._run_ob_oms_poll(exch_name, symbol),
                    name=f"ob-oms-poll-{exch_name}-{symbol}",
                )
                self._tasks.append(task)
    
    async def _oms_receive_loop(self, ws) -> None:
        """Handle incoming OMS messages with timeout detection."""
        receive_timeout = 60.0  # Max seconds without any message
        
        while self._running and self._oms_ws_active:
            try:
                # Wait for message with timeout
                raw = await asyncio.wait_for(ws.recv(), timeout=receive_timeout)
                
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")
                
                # Handle subscription acknowledgments
                if msg_type == "subscribed":
                    exch = msg.get("exchange", "")
                    sym = msg.get("symbol", "")
                    symbol_key = f"{exch}:{sym}"
                    if symbol_key in self._pending_subscriptions:
                        self._pending_subscriptions[symbol_key].set()
                        logger.debug("DataLayer: OMS ack for %s", symbol_key)
                    continue
                
                if msg_type != "book":
                    continue

                exch = msg.get("exchange", "")
                sym = msg.get("symbol", "")
                key = (exch, sym)

                if key not in self._orderbooks:
                    continue

                async with self._ob_locks[key]:
                    snap = self._orderbooks[key]
                    snap.bids = msg.get("bids", [])
                    snap.asks = msg.get("asks", [])
                    snap.timestamp_ms = msg.get("timestamp_ms", time.time() * 1000)
                    snap.is_synced = True
                    snap.connected = True
                    snap.update_count += 1
                    self._ob_changed.set()
                    
            except asyncio.TimeoutError:
                logger.warning("DataLayer: OMS WS no message for %.0fs — connection may be dead", receive_timeout)
                raise  # Propagate to trigger reconnect
            except websockets.ConnectionClosed:
                raise  # Propagate to trigger reconnect
            except Exception as exc:
                logger.warning("DataLayer: OMS receive error: %s", exc)
                # Continue loop, may recover
    
    async def _oms_ping_loop(self, ws, interval: float) -> None:
        """Send periodic pings to keep connection alive."""
        while self._running and self._oms_ws_active:
            try:
                await asyncio.sleep(interval)
                if ws.open:
                    await ws.ping()
                    logger.debug("DataLayer: OMS WS ping sent")
            except websockets.ConnectionClosed:
                logger.debug("DataLayer: OMS WS ping failed — connection closed")
                return  # Exit loop, main task will reconnect
            except Exception as exc:
                logger.debug("DataLayer: OMS WS ping error: %s", exc)

    # ── Extended WS orderbook ────────────────────────────────────────

    async def _run_ob_ws_extended(self, symbol: str) -> None:
        """Async WS orderbook for Extended exchange.

        Endpoint: wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks/{symbol}
        Receives SNAPSHOT + DELTA messages with sequence numbers.
        """
        import random
        await asyncio.sleep(random.uniform(0, 3.0))
        key = ("extended", symbol)
        ws_url = f"wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks/{symbol}"
        reconnect_delay = 1.0

        while self._running:
            try:
                logger.info("DataLayer: Extended OB WS connecting: %s", ws_url)
                async for ws in websockets.connect(ws_url, ssl=_SSL_CTX, extra_headers={"User-Agent": "tradeautonom/1.0"}, close_timeout=5):
                    async with self._ob_locks[key]:
                        self._orderbooks[key].connected = True
                    reconnect_delay = 1.0
                    logger.info("DataLayer: Extended OB WS connected: %s", symbol)

                    try:
                        async for raw in ws:
                            if not self._running:
                                return
                            self._handle_extended_message(key, raw)
                    except websockets.ConnectionClosed:
                        logger.warning("DataLayer: Extended OB WS disconnected: %s", symbol)
                    finally:
                        async with self._ob_locks[key]:
                            self._orderbooks[key].connected = False
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("DataLayer: Extended OB WS error %s: %s — retry in %.0fs", symbol, exc, reconnect_delay)
                async with self._ob_locks[key]:
                    self._orderbooks[key].connected = False
                    self._orderbooks[key].is_synced = False
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)

    def _handle_extended_message(self, key: tuple, raw: str) -> None:
        """Parse Extended WS orderbook message (SNAPSHOT or DELTA)."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        data = msg.get("data")
        if not data:
            return

        msg_type = msg.get("type", "SNAPSHOT")
        seq = msg.get("seq", -1)
        snap = self._orderbooks[key]

        # Sequence validation
        if msg_type == "SNAPSHOT":
            snap.last_seq = seq
            snap.is_synced = True
        elif seq != -1 and snap.last_seq != -1:
            if seq != snap.last_seq + 1:
                logger.warning("Extended OB seq gap: expected %d got %d", snap.last_seq + 1, seq)
                snap.is_synced = False
            snap.last_seq = seq

        bids_raw = data.get("b", [])
        asks_raw = data.get("a", [])

        if msg_type == "SNAPSHOT":
            bids = [[float(b["p"]), float(b["q"])] for b in bids_raw if "p" in b]
            asks = [[float(a["p"]), float(a["q"])] for a in asks_raw if "p" in a]
            if bids:
                snap.bids = sorted(bids, key=lambda x: -x[0])
            if asks:
                snap.asks = sorted(asks, key=lambda x: x[0])
        else:
            # DELTA: use 'c' (cumulative) field
            if bids_raw:
                _apply_delta_cumulative(snap.bids, bids_raw, reverse=True)
            if asks_raw:
                _apply_delta_cumulative(snap.asks, asks_raw, reverse=False)

        snap.timestamp_ms = time.time() * 1000
        snap.update_count += 1
        self._ob_changed.set()

    # ── GRVT WS orderbook ────────────────────────────────────────────

    async def _run_ob_ws_grvt(self, symbol: str, grvt_env: str) -> None:
        """Async WS orderbook for GRVT exchange — public, no auth required.

        Endpoint: wss://market-data.{env}.grvt.io/ws/full
        Stream:   v1.book.s — full snapshots every 500ms, 10 levels.
        """
        key = ("grvt", symbol)
        ws_url = _GRVT_WS_ENDPOINTS.get(grvt_env, _GRVT_WS_ENDPOINTS["prod"])
        reconnect_delay = 1.0

        while self._running:
            try:
                logger.info("DataLayer: GRVT OB WS connecting: %s (symbol=%s)", ws_url, symbol)
                async with websockets.connect(ws_url, ssl=_SSL_CTX, close_timeout=5) as ws:
                    sub_msg = json.dumps({
                        "jsonrpc": "2.0",
                        "method": "subscribe",
                        "params": {"stream": "v1.book.s", "selectors": [f"{symbol}@500-50"]},
                        "id": 1,
                    })
                    await ws.send(sub_msg)

                    try:
                        resp_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        resp = json.loads(resp_raw)
                        if "error" in resp:
                            raise RuntimeError(f"GRVT subscribe error: {resp['error']}")
                        logger.info("DataLayer: GRVT OB WS subscribed: %s", symbol)
                    except asyncio.TimeoutError:
                        logger.warning("DataLayer: GRVT OB WS subscribe timeout")

                    reconnect_delay = 1.0
                    async with self._ob_locks[key]:
                        self._orderbooks[key].connected = True

                    async for raw in ws:
                        if not self._running:
                            return
                        self._handle_grvt_message(key, raw)

            except asyncio.CancelledError:
                break
            except websockets.ConnectionClosed:
                logger.warning("DataLayer: GRVT OB WS disconnected: %s — reconnecting in %.0fs", symbol, reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)
            except Exception as exc:
                logger.warning("DataLayer: GRVT OB WS error %s: %s — retry in %.0fs", symbol, exc, reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)
            finally:
                async with self._ob_locks[key]:
                    self._orderbooks[key].connected = False
                    self._orderbooks[key].is_synced = False

    def _handle_grvt_message(self, key: tuple, raw: str) -> None:
        """Parse GRVT WS v1.book.s message. Accepts full and lite field names.

        Full: feed / sequence_number / prev_sequence_number, bids/asks with price+size
        Lite: f    / sn              / psn,                  b/a     with p+s
        """
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Support both full ("feed") and lite ("f") field names
        feed = msg.get("feed") or msg.get("f")
        if not feed:
            return

        seq = int(msg.get("sequence_number") or msg.get("sn") or 0)
        prev_seq = int(msg.get("prev_sequence_number") or msg.get("psn") or 0)
        snap = self._orderbooks[key]

        def _parse_levels(levels: list) -> list[list[float]]:
            result = []
            for lvl in levels:
                # Full field names: price/size; lite: p/s
                px = lvl.get("price") or lvl.get("p")
                sz = lvl.get("size") or lvl.get("s")
                if px is not None and sz is not None:
                    result.append([float(px), float(sz)])
            return result

        bids = _parse_levels(feed.get("bids") or feed.get("b") or [])
        asks = _parse_levels(feed.get("asks") or feed.get("a") or [])

        if not bids and not asks:
            return

        # v1.book.s sends full snapshots — sequence gaps are informational only
        if seq == 0 or snap.last_seq == 0:
            snap.is_synced = True
        elif prev_seq != snap.last_seq:
            logger.warning("GRVT OB seq gap: prev=%d last=%d — resyncing", prev_seq, snap.last_seq)
            snap.is_synced = True  # still accept; next snapshot is complete anyway
        else:
            snap.is_synced = True
        snap.last_seq = seq

        if bids:
            snap.bids = sorted(bids, key=lambda x: -x[0])
        if asks:
            snap.asks = sorted(asks, key=lambda x: x[0])
        snap.timestamp_ms = time.time() * 1000
        snap.update_count += 1
        self._ob_changed.set()

    # ── Nado WS orderbook ────────────────────────────────────────────

    async def _run_ob_ws_nado(self, symbol: str, product_id: int, nado_env: str) -> None:
        """Async WS orderbook for Nado exchange.

        Endpoint: wss://gateway.prod.nado.xyz/v1/subscribe
        Subscribes to book_depth stream (incremental deltas, x18 format).
        """
        key = ("nado", symbol)
        nado_ws_endpoints = {
            "mainnet": "wss://gateway.prod.nado.xyz/v1/subscribe",
            "testnet": "wss://gateway.sepolia.nado.xyz/v1/subscribe",
        }
        ws_url = nado_ws_endpoints.get(nado_env, nado_ws_endpoints["mainnet"])
        reconnect_delay = 1.0

        while self._running:
            try:
                logger.info("DataLayer: Nado OB WS connecting: %s (product=%d)", ws_url, product_id)
                async for ws in websockets.connect(ws_url, ssl=_SSL_CTX, close_timeout=5):
                    sub_msg = json.dumps({
                        "method": "subscribe",
                        "stream": {"type": "book_depth", "product_id": product_id},
                        "id": product_id,
                    })
                    await ws.send(sub_msg)

                    try:
                        resp_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                        resp = json.loads(resp_raw)
                        if resp.get("error"):
                            raise RuntimeError(f"Nado subscribe error: {resp}")
                        logger.info("DataLayer: Nado OB WS subscribed: product=%d", product_id)
                    except asyncio.TimeoutError:
                        logger.warning("DataLayer: Nado OB WS subscribe timeout")

                    async with self._ob_locks[key]:
                        self._orderbooks[key].connected = True
                    reconnect_delay = 1.0

                    try:
                        async for raw in ws:
                            if not self._running:
                                return
                            self._handle_nado_message(key, raw)
                    except websockets.ConnectionClosed:
                        logger.warning("DataLayer: Nado OB WS disconnected: %s", symbol)
                    finally:
                        async with self._ob_locks[key]:
                            self._orderbooks[key].connected = False
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("DataLayer: Nado OB WS error %s: %s — retry in %.0fs", symbol, exc, reconnect_delay)
                async with self._ob_locks[key]:
                    self._orderbooks[key].connected = False
                    self._orderbooks[key].is_synced = False
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)

    def _handle_nado_message(self, key: tuple, raw: str) -> None:
        """Parse Nado WS book_depth message (incremental deltas, x18 format)."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        data = msg.get("data") or (msg if "bids" in msg else None)
        if not data:
            return

        bids_raw = data.get("bids", [])
        asks_raw = data.get("asks", [])
        if not bids_raw and not asks_raw:
            return

        snap = self._orderbooks[key]

        if bids_raw:
            _apply_nado_delta(snap.bids, bids_raw, reverse=True)
        if asks_raw:
            _apply_nado_delta(snap.asks, asks_raw, reverse=False)

        snap.timestamp_ms = time.time() * 1000
        snap.update_count += 1
        snap.is_synced = True
        self._ob_changed.set()

    # ── Position WS/poll streams ────────────────────────────────────────

    async def _run_position_subscription(self, client, exch_name: str, symbol: str) -> None:
        """Route to the correct exchange-specific position feed handler."""
        if exch_name == "extended":
            await self._run_pos_ws_extended(client, symbol)
        elif exch_name == "variational":
            logger.info("DataLayer: Variational has no position WS — using REST polling for %s", symbol)
            await self._run_pos_rest_fallback(client, exch_name, symbol)
        elif exch_name == "nado":
            product_id = client._get_product_id(symbol) if hasattr(client, "_get_product_id") else None
            await self._run_pos_ws_nado(client, symbol, product_id)
        elif exch_name == "grvt":
            await self._run_pos_ws_grvt(client, symbol)
        else:
            # Unknown exchanges: REST poll fallback
            await self._run_pos_rest_fallback(client, exch_name, symbol)

    async def _run_pos_ws_extended(self, client, symbol: str) -> None:
        """Subscribe to Extended private account stream for POSITION events.

        Endpoint: wss://api.starknet.extended.exchange/stream.extended.exchange/v1/account
        The same stream delivers ORDER, TRADE, BALANCE, and POSITION events.
        We only consume POSITION events here.
        """
        key = ("extended", symbol)
        api_key = getattr(client, "_api_key", None)
        if not api_key:
            logger.warning("DataLayer: Extended position WS — no API key, falling back to REST")
            await self._run_pos_rest_fallback(client, "extended", symbol)
            return

        ws_url = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/account"
        reconnect_delay = 1.0

        while self._running:
            try:
                logger.info("DataLayer: Extended position WS connecting: %s", ws_url)
                async for ws in websockets.connect(ws_url, ssl=_SSL_CTX, extra_headers={"X-Api-Key": api_key, "User-Agent": "tradeautonom/1.0"}, close_timeout=5):
                    async with self._pos_locks[key]:
                        self._positions[key].connected = True
                    reconnect_delay = 1.0
                    logger.info("DataLayer: Extended position WS connected")

                    try:
                        async for raw in ws:
                            msg = json.loads(raw)
                            if msg.get("type") != "POSITION":
                                continue
                            data = msg.get("data", {})
                            positions = data.get("positions", [data]) if "positions" in data else [data]
                            for p in positions:
                                market = p.get("market", "")
                                if symbol and market != symbol:
                                    continue
                                size = abs(float(p.get("qty", p.get("size", 0))))
                                side_raw = p.get("side", "").lower()
                                side = "long" if side_raw == "buy" or side_raw == "long" else ("short" if side_raw == "sell" or side_raw == "short" else "")
                                entry_px = float(p.get("entryPrice", p.get("entry_price", 0)))
                                upnl = float(p.get("unrealisedPnl", p.get("unrealized_pnl", 0)))

                                async with self._pos_locks[key]:
                                    snap = self._positions[key]
                                    snap.size = size
                                    snap.side = side
                                    snap.entry_price = entry_px
                                    snap.unrealized_pnl = upnl
                                    snap.timestamp_ms = time.time() * 1000
                                    snap.connected = True
                                    snap.update_count += 1
                                self._pos_changed.set()
                                logger.debug("Extended position update: %s size=%.6f side=%s", symbol, size, side)
                    except websockets.ConnectionClosed:
                        logger.warning("DataLayer: Extended position WS disconnected — reconnecting")
                        async with self._pos_locks[key]:
                            self._positions[key].connected = False
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("DataLayer: Extended position WS error: %s — retry in %.0fs", exc, reconnect_delay)
                async with self._pos_locks[key]:
                    self._positions[key].connected = False
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)

    async def _run_pos_ws_nado(self, client, symbol: str, product_id: int | None) -> None:
        """Subscribe to Nado position_change WS stream.

        Endpoint: wss://gateway.prod.nado.xyz/v1/subscribe
        Stream: position_change (per-subaccount)
        """
        key = ("nado", symbol)
        sender_hex = getattr(client, "_sender_hex", None)
        if not sender_hex:
            logger.warning("DataLayer: Nado position WS — no sender_hex, falling back to REST")
            await self._run_pos_rest_fallback(client, "nado", symbol)
            return

        gateway_rest = getattr(client, "_gateway_rest", "https://gateway.prod.nado.xyz")
        ws_url = gateway_rest.replace("https://", "wss://") + "/subscribe"
        reconnect_delay = 1.0

        while self._running:
            try:
                logger.info("DataLayer: Nado position WS connecting: %s subaccount=%s", ws_url, sender_hex[:20])
                async for ws in websockets.connect(ws_url, ssl=False, close_timeout=5):
                    sub_msg = json.dumps({
                        "method": "subscribe",
                        "stream": {
                            "type": "position_change",
                            "subaccount": sender_hex,
                        },
                    })
                    await ws.send(sub_msg)
                    async with self._pos_locks[key]:
                        self._positions[key].connected = True
                    reconnect_delay = 1.0
                    logger.info("DataLayer: Nado position WS subscribed")

                    try:
                        async for raw in ws:
                            msg = json.loads(raw)
                            if msg.get("type") != "position_change":
                                continue
                            pid = msg.get("product_id")
                            if product_id is not None and pid != product_id:
                                continue
                            size = abs(float(msg.get("size", msg.get("position_size", "0")))) / _X18_FLOAT if isinstance(msg.get("size", msg.get("position_size", "0")), str) and len(msg.get("size", msg.get("position_size", "0"))) > 10 else abs(float(msg.get("size", msg.get("position_size", 0))))
                            side = "long" if float(msg.get("size", msg.get("position_size", 0))) > 0 else ("short" if float(msg.get("size", msg.get("position_size", 0))) < 0 else "")
                            entry_px = float(msg.get("entry_price", 0))
                            if isinstance(msg.get("entry_price", "0"), str) and len(msg.get("entry_price", "0")) > 10:
                                entry_px = float(msg.get("entry_price", "0")) / _X18_FLOAT

                            async with self._pos_locks[key]:
                                snap = self._positions[key]
                                snap.size = size
                                snap.side = side
                                snap.entry_price = entry_px
                                snap.timestamp_ms = time.time() * 1000
                                snap.connected = True
                                snap.update_count += 1
                            self._pos_changed.set()
                            logger.debug("Nado position update: %s size=%.6f side=%s", symbol, size, side)
                    except websockets.ConnectionClosed:
                        logger.warning("DataLayer: Nado position WS disconnected — reconnecting")
                        async with self._pos_locks[key]:
                            self._positions[key].connected = False
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("DataLayer: Nado position WS error: %s — retry in %.0fs", exc, reconnect_delay)
                async with self._pos_locks[key]:
                    self._positions[key].connected = False
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)

    def _grvt_ws_headers(self, client) -> dict[str, str]:
        """Build auth headers for GRVT private WS (cookie + account ID)."""
        api_obj = getattr(client, "_api", None)
        settings = getattr(client, "settings", None)
        sub_account_id = getattr(settings, "grvt_trading_account_id", "") if settings else ""

        headers: dict[str, str] = {}

        # _cookie is a dict like {"gravity": "abc...", "X-Grvt-Account-Id": "123"}
        if api_obj:
            # Ensure cookie is fresh
            if hasattr(api_obj, "refresh_cookie"):
                api_obj.refresh_cookie()
            cookie_dict = getattr(api_obj, "_cookie", None)
            if isinstance(cookie_dict, dict) and "gravity" in cookie_dict:
                headers["Cookie"] = f"gravity={cookie_dict['gravity']}"
                # Use the account ID returned by the login endpoint (may differ from settings value)
                acct_from_cookie = cookie_dict.get("X-Grvt-Account-Id", "")
                headers["X-Grvt-Account-Id"] = str(acct_from_cookie or sub_account_id)
            elif isinstance(cookie_dict, str):
                headers["Cookie"] = cookie_dict
                headers["X-Grvt-Account-Id"] = str(sub_account_id)
            else:
                headers["X-Grvt-Account-Id"] = str(sub_account_id)
        else:
            headers["X-Grvt-Account-Id"] = str(sub_account_id)
        return headers

    async def _run_pos_ws_grvt(self, client, symbol: str) -> None:
        """Subscribe to GRVT v1.position WS stream for real-time position updates.

        Endpoint: wss://trades.grvt.io/ws/full (prod)
        Stream: v1.position
        Selector: {sub_account_id}-{instrument}
        Auth: Cookie + X-Grvt-Account-Id header (same as v1.fill)
        """
        key = ("grvt", symbol)

        settings = getattr(client, "settings", None)
        sub_account_id = getattr(settings, "grvt_trading_account_id", None) if settings else None

        if not sub_account_id:
            logger.warning("DataLayer: GRVT position WS — no sub_account_id, falling back to REST")
            await self._run_pos_rest_fallback(client, "grvt", symbol)
            return

        # Derive WS URL from client's trade base URL
        trade_base = client._trade_base_url() if hasattr(client, "_trade_base_url") else "https://trades.grvt.io"
        ws_url = trade_base.replace("https://", "wss://") + "/ws/full"
        selector = f"{sub_account_id}-{symbol}"

        reconnect_delay = 1.0

        while self._running:
            # Refresh cookie before every connect attempt so the header is always fresh
            api_obj = getattr(client, "_api", None)
            if api_obj and hasattr(api_obj, "refresh_cookie"):
                try:
                    await asyncio.to_thread(api_obj.refresh_cookie)
                except Exception as _ce:
                    logger.warning("DataLayer: GRVT position WS cookie refresh failed: %s", _ce)
            headers = self._grvt_ws_headers(client)
            if "Cookie" not in headers:
                logger.warning("DataLayer: GRVT position WS — no cookie available, retry in %.0fs", reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)
                continue

            try:
                logger.info("DataLayer: GRVT position WS connecting: %s selector=%s", ws_url, selector)
                async with websockets.connect(ws_url, ssl=_SSL_CTX, extra_headers=headers, close_timeout=5) as ws:
                    reconnect_delay = 1.0
                    # Subscribe to v1.position stream
                    sub_msg = json.dumps({
                        "jsonrpc": "2.0",
                        "method": "subscribe",
                        "params": {"stream": "v1.position", "selectors": [selector]},
                        "id": 2,
                    })
                    await ws.send(sub_msg)
                    logger.info("DataLayer: GRVT position WS subscribe sent: %s", selector)

                    async for raw in ws:
                        msg = json.loads(raw)

                        if "error" in msg:
                            logger.warning("DataLayer: GRVT position WS error response: %s", msg["error"])
                            continue

                        result = msg.get("result")
                        if isinstance(result, dict) and "subs" in result:
                            subs = result.get("subs", [])
                            logger.info("DataLayer: GRVT position WS confirmed subs=%s", subs)
                            if subs:
                                async with self._pos_locks[key]:
                                    self._positions[key].connected = True
                            continue

                        feed = msg.get("feed", msg.get("f"))
                        if not feed or not isinstance(feed, dict):
                            continue

                        instrument = feed.get("instrument", feed.get("i", ""))
                        if instrument != symbol:
                            continue

                        raw_size = float(feed.get("size", feed.get("s", 0)))
                        size = abs(raw_size)
                        side = "long" if raw_size > 0 else ("short" if raw_size < 0 else "")
                        entry_px = float(feed.get("entry_price", feed.get("ep", 0)))
                        upnl = float(feed.get("unrealized_pnl", feed.get("up", 0)))

                        async with self._pos_locks[key]:
                            snap = self._positions[key]
                            snap.size = size
                            snap.side = side
                            snap.entry_price = entry_px
                            snap.unrealized_pnl = upnl
                            snap.timestamp_ms = time.time() * 1000
                            snap.connected = True
                            snap.update_count += 1
                        self._pos_changed.set()
                        logger.debug("GRVT position update: %s size=%.6f side=%s entry=%.4f", symbol, size, side, entry_px)

            except asyncio.CancelledError:
                break
            except websockets.ConnectionClosed:
                logger.warning("DataLayer: GRVT position WS disconnected — reconnecting in %.0fs", reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)
            except Exception as exc:
                logger.warning("DataLayer: GRVT position WS error: %s — retry in %.0fs", exc, reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)
            finally:
                async with self._pos_locks[key]:
                    self._positions[key].connected = False

    async def _run_pos_rest_fallback(self, client, exch_name: str, symbol: str) -> None:
        """Poll positions via REST as fallback for exchanges without WS position streams."""
        key = (exch_name, symbol)
        poll_interval = 2.0

        while self._running:
            try:
                positions = await client.async_fetch_positions([symbol])
                size = 0.0
                side = ""
                entry_px = 0.0
                upnl = 0.0
                for p in positions:
                    p_inst = p.get("instrument", p.get("symbol", ""))
                    matched = (p_inst == symbol)
                    if not matched:
                        # Fallback: match by underlying (handles funding_interval mismatch)
                        try:
                            p_parts = p_inst.split("-")
                            s_parts = symbol.split("-")
                            if len(p_parts) >= 2 and len(s_parts) >= 2 and p_parts[1].upper() == s_parts[1].upper():
                                matched = True
                        except Exception:
                            pass
                    if matched:
                        raw_size = float(p.get("size", 0))
                        size = abs(raw_size)
                        side = "long" if raw_size > 0 else ("short" if raw_size < 0 else "")
                        entry_px = float(p.get("entry_price", 0))
                        upnl = float(p.get("unrealized_pnl", 0))
                        break

                async with self._pos_locks[key]:
                    snap = self._positions[key]
                    snap.size = size
                    snap.side = side
                    snap.entry_price = entry_px
                    snap.unrealized_pnl = upnl
                    snap.timestamp_ms = time.time() * 1000
                    snap.connected = True
                    snap.update_count += 1
                self._pos_changed.set()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("DataLayer: position REST poll %s:%s error: %s", exch_name, symbol, exc)
                async with self._pos_locks[key]:
                    self._positions[key].connected = False
            await asyncio.sleep(poll_interval)

    # ── Utility ───────────────────────────────────────────────────────

    def status(self) -> dict:
        """Return a summary of all feed states for monitoring."""
        result = {
            "orderbooks": {}, "funding_rates": {}, "positions": {},
            "feeds_ready": self.is_ready(),
            "oms_url": self._shared_monitor_url or None,
            "oms_active": bool(self._shared_monitor_url),
            "oms_ws_connected": self._oms_ws_active,
        }
        now_ms = time.time() * 1000
        for (exch, sym), snap in self._orderbooks.items():
            result["orderbooks"][f"{exch}:{sym}"] = {
                "connected": snap.connected,
                "synced": snap.is_synced,
                "age_ms": round(now_ms - snap.timestamp_ms) if snap.timestamp_ms else None,
                "bids": len(snap.bids),
                "asks": len(snap.asks),
                "updates": snap.update_count,
            }
        for (exch, sym), snap in self._funding_rates.items():
            result["funding_rates"][f"{exch}:{sym}"] = {
                "rate": snap.funding_rate,
                "age_ms": round(now_ms - snap.update_time_ms) if snap.update_time_ms else None,
            }
        for (exch, sym), snap in self._positions.items():
            result["positions"][f"{exch}:{sym}"] = {
                "size": snap.size,
                "side": snap.side,
                "entry_price": snap.entry_price,
                "connected": snap.connected,
                "age_ms": round(now_ms - snap.timestamp_ms) if snap.timestamp_ms else None,
                "updates": snap.update_count,
            }
        return result


# ── Module-level helpers (shared by message handlers) ─────────────────

def _apply_delta_cumulative(book: list, updates_raw: list, reverse: bool) -> None:
    """Apply Extended delta updates using the 'c' (cumulative) field.

    Each update: {"p": price, "q": change, "c": cumulative_size_after}
    c > 0: upsert level | c == 0: remove level
    """
    price_map = {level[0]: level for level in book}
    for entry in updates_raw:
        price_str = entry.get("p")
        if price_str is None:
            continue
        price = float(price_str)
        cum_str = entry.get("c")
        if cum_str is not None:
            cum = float(cum_str)
            if cum <= 0:
                price_map.pop(price, None)
            else:
                price_map[price] = [price, cum]
        else:
            q = float(entry.get("q", 0))
            if q <= 0:
                price_map.pop(price, None)
            else:
                price_map[price] = [price, q]
    book.clear()
    book.extend(sorted(price_map.values(), key=lambda x: -x[0] if reverse else x[0]))


def _apply_nado_delta(book: list, updates: list, reverse: bool) -> None:
    """Apply Nado incremental book_depth updates.

    Each update is [price_x18, size_x18].
    size == 0 → remove level, otherwise upsert.
    """
    price_map = {level[0]: level for level in book}
    for entry in updates:
        if len(entry) < 2:
            continue
        price = float(entry[0]) / _X18_FLOAT
        size = float(entry[1]) / _X18_FLOAT
        if size <= 0:
            price_map.pop(price, None)
        else:
            price_map[price] = [price, size]
    book.clear()
    book.extend(sorted(price_map.values(), key=lambda x: -x[0] if reverse else x[0]))
