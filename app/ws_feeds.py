"""WebSocket orderbook feed manager for real-time spread monitoring.

Maintains persistent WebSocket connections to Extended and GRVT exchanges,
keeping a local in-memory copy of each orderbook. Thread-safe reads allow
the ArbitrageEngine to access near-real-time data without REST calls.

Extended: wss://api.starknet.extended.exchange  (BBO snapshots every ~10ms)
GRVT:     wss://market-data.{env}.grvt.io/ws/full  (v1.book.s snapshots)
"""

import json
import logging
import ssl
import threading
import time
from dataclasses import dataclass, field

import websockets.sync.client as ws_sync

logger = logging.getLogger("tradeautonom.ws_feeds")

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class OrderbookSnapshot:
    """Thread-safe local orderbook cache for one instrument."""
    bids: list[list[float]] = field(default_factory=list)   # [[price, size], ...]
    asks: list[list[float]] = field(default_factory=list)
    last_update_ts: float = 0.0                              # time.time()
    update_count: int = 0
    last_seq: int = -1                                        # last processed sequence number
    is_synced: bool = False                                   # False if sequence gap detected


# ---------------------------------------------------------------------------
# GRVT WebSocket endpoint resolution
# ---------------------------------------------------------------------------

_GRVT_WS_ENDPOINTS = {
    "dev":     "wss://market-data.dev.gravitymarkets.io/ws/full",
    "staging": "wss://market-data.stg.gravitymarkets.io/ws/full",
    "testnet": "wss://market-data.testnet.grvt.io/ws/full",
    "prod":    "wss://market-data.grvt.io/ws/full",
}


def _grvt_ws_url(env: str) -> str:
    return _GRVT_WS_ENDPOINTS.get(env, _GRVT_WS_ENDPOINTS["testnet"])


# ---------------------------------------------------------------------------
# Extended WebSocket handler
# ---------------------------------------------------------------------------

class _ExtendedFeedThread(threading.Thread):
    """Background thread: connects to Extended WS orderbook stream."""

    def __init__(
        self,
        instrument: str,
        book: OrderbookSnapshot,
        lock: threading.Lock,
        stop_event: threading.Event,
    ):
        super().__init__(daemon=True, name=f"ws-extended-{instrument}")
        self.instrument = instrument
        self._book = book
        self._lock = lock
        self._stop_ev = stop_event
        # Extended WS endpoint — full orderbook (snapshots every 100ms + deltas)
        self._url = (
            f"wss://api.starknet.extended.exchange"
            f"/stream.extended.exchange/v1/orderbooks/{instrument}"
        )
        self.connected = False
        self._reconnect_delay = 1.0

    def run(self) -> None:
        while not self._stop_ev.is_set():
            try:
                self._connect_and_listen()
            except Exception as exc:
                self.connected = False
                if self._stop_ev.is_set():
                    break
                logger.warning(
                    "Extended WS (%s) error: %s — reconnecting in %.0fs",
                    self.instrument, exc, self._reconnect_delay,
                )
                self._stop_ev.wait(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    def _connect_and_listen(self) -> None:
        logger.info("Extended WS connecting: %s", self._url)
        # Extended uses self-signed certs — disable SSL verification
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        with ws_sync.connect(self._url, ssl=ssl_ctx, close_timeout=5) as conn:
            self.connected = True
            self._reconnect_delay = 1.0
            logger.info("Extended WS connected: %s", self.instrument)

            while not self._stop_ev.is_set():
                try:
                    raw = conn.recv(timeout=20)  # server pings every 15s
                except TimeoutError:
                    # No data received — send a ping to keep alive
                    try:
                        conn.ping()
                    except Exception:
                        break
                    continue

                self._handle_message(raw)

    def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        data = msg.get("data")
        if not data:
            return

        msg_type = msg.get("type", "SNAPSHOT")
        seq = msg.get("seq", -1)

        with self._lock:
            # --- Sequence validation ---
            if msg_type == "SNAPSHOT":
                # SNAPSHOT resets sequence — always accept
                self._book.last_seq = seq
                self._book.is_synced = True
            elif seq != -1 and self._book.last_seq != -1:
                expected = self._book.last_seq + 1
                if seq != expected:
                    logger.warning(
                        "Extended WS (%s) seq gap: expected %d got %d — marking out-of-sync",
                        self.instrument, expected, seq,
                    )
                    self._book.is_synced = False
                self._book.last_seq = seq

            bids_raw = data.get("b", [])
            asks_raw = data.get("a", [])

            if msg_type == "SNAPSHOT":
                # Full replacement — use 'q' directly (absolute sizes in snapshots)
                bids = [[float(b["p"]), float(b["q"])] for b in bids_raw if "p" in b]
                asks = [[float(a["p"]), float(a["q"])] for a in asks_raw if "p" in a]
                if bids:
                    self._book.bids = sorted(bids, key=lambda x: -x[0])
                if asks:
                    self._book.asks = sorted(asks, key=lambda x: x[0])
            else:
                # DELTA: use 'c' (cumulative size after change) when available.
                # 'c' == "0" or absent with negative 'q' means level removed.
                if bids_raw:
                    self._apply_delta_cumulative(self._book.bids, bids_raw, reverse=True)
                if asks_raw:
                    self._apply_delta_cumulative(self._book.asks, asks_raw, reverse=False)

            self._book.last_update_ts = time.time()
            self._book.update_count += 1

    @staticmethod
    def _apply_delta_cumulative(book: list, updates_raw: list, reverse: bool) -> None:
        """Apply delta updates using the 'c' (cumulative) field from Extended.

        Each update: {"p": price, "q": change, "c": cumulative_size_after}
        - c > 0: upsert level with cumulative size
        - c == 0 or c absent with q < 0: remove level
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
                # No 'c' field — fall back to 'q' (change). Negative = remove.
                q = float(entry.get("q", 0))
                if q <= 0:
                    price_map.pop(price, None)
                else:
                    price_map[price] = [price, q]
        book.clear()
        book.extend(sorted(price_map.values(), key=lambda x: -x[0] if reverse else x[0]))


# ---------------------------------------------------------------------------
# GRVT WebSocket handler
# ---------------------------------------------------------------------------

class _GrvtFeedThread(threading.Thread):
    """Background thread: connects to GRVT WS market data stream."""

    def __init__(
        self,
        instrument: str,
        env: str,
        book: OrderbookSnapshot,
        lock: threading.Lock,
        stop_event: threading.Event,
    ):
        super().__init__(daemon=True, name=f"ws-grvt-{instrument}")
        self.instrument = instrument
        self._env = env
        self._book = book
        self._lock = lock
        self._stop_ev = stop_event
        self._url = _grvt_ws_url(env)
        self.connected = False
        self._reconnect_delay = 1.0

    def run(self) -> None:
        while not self._stop_ev.is_set():
            try:
                self._connect_and_listen()
            except Exception as exc:
                self.connected = False
                if self._stop_ev.is_set():
                    break
                logger.warning(
                    "GRVT WS (%s) error: %s — reconnecting in %.0fs",
                    self.instrument, exc, self._reconnect_delay,
                )
                self._stop_ev.wait(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    def _connect_and_listen(self) -> None:
        logger.info("GRVT WS connecting: %s (instrument=%s)", self._url, self.instrument)

        # GRVT uses self-signed certs — disable SSL verification
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        with ws_sync.connect(self._url, ssl=ssl_ctx, close_timeout=5) as conn:
            # Subscribe to orderbook snapshots
            # Format: instrument@rate-depth  (rate=500ms, depth=10 levels)
            subscribe_msg = json.dumps({
                "jsonrpc": "2.0",
                "method": "subscribe",
                "params": {
                    "stream": "v1.book.s",
                    "selectors": [f"{self.instrument}@500-10"],
                },
                "id": 1,
            })
            conn.send(subscribe_msg)
            logger.info("GRVT WS subscribe sent: %s@500-10", self.instrument)

            # Read subscription confirmation
            try:
                resp_raw = conn.recv(timeout=10)
                resp = json.loads(resp_raw)
                if "error" in resp:
                    raise RuntimeError(f"GRVT subscribe error: {resp['error']}")
                subs = resp.get("result", resp).get("subs", resp.get("subs", []))
                logger.info("GRVT WS subscribed: %s", subs)
            except TimeoutError:
                logger.warning("GRVT WS subscribe response timeout")

            self.connected = True
            self._reconnect_delay = 1.0

            while not self._stop_ev.is_set():
                try:
                    raw = conn.recv(timeout=30)
                except TimeoutError:
                    try:
                        conn.ping()
                    except Exception:
                        break
                    continue

                self._handle_message(raw)

    def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # GRVT feed data: {"stream":"v1.book.s","selector":"...","sequence_number":"...","feed":{...}}
        feed = msg.get("feed")
        if not feed:
            return

        seq = int(msg.get("sequence_number", 0))
        prev_seq = int(msg.get("prev_sequence_number", 0))

        bids_raw = feed.get("bids", [])
        asks_raw = feed.get("asks", [])

        # GRVT format: [{"price": "123.45", "size": "0.5", "num_orders": 1}, ...]
        bids = [[float(b["price"]), float(b["size"])] for b in bids_raw if "price" in b]
        asks = [[float(a["price"]), float(a["size"])] for a in asks_raw if "price" in a]

        if not bids and not asks:
            return

        with self._lock:
            # --- Sequence validation ---
            # GRVT v1.book.s sends full snapshots every 500ms (not deltas),
            # so gaps are less critical but still worth tracking.
            if seq == 0:
                # Initial snapshot (sequence_number=0) — always accept
                self._book.is_synced = True
            elif self._book.last_seq > 0 and prev_seq != self._book.last_seq:
                logger.warning(
                    "GRVT WS (%s) seq gap: prev_seq=%d but last_seq=%d — marking out-of-sync",
                    self.instrument, prev_seq, self._book.last_seq,
                )
                self._book.is_synced = False
            else:
                self._book.is_synced = True
            self._book.last_seq = seq

            if bids:
                self._book.bids = sorted(bids, key=lambda x: -x[0])
            if asks:
                self._book.asks = sorted(asks, key=lambda x: x[0])
            self._book.last_update_ts = time.time()
            self._book.update_count += 1


# ---------------------------------------------------------------------------
# NADO WebSocket handler
# ---------------------------------------------------------------------------

_NADO_WS_ENDPOINTS = {
    "mainnet": "wss://gateway.prod.nado.xyz/v1/subscribe",
    "testnet": "wss://gateway.sepolia.nado.xyz/v1/subscribe",
}

_X18_FLOAT = 1e18


class _NadoFeedThread(threading.Thread):
    """Background thread: connects to NADO WS book_depth stream."""

    def __init__(
        self,
        instrument: str,
        product_id: int,
        book: OrderbookSnapshot,
        lock: threading.Lock,
        stop_event: threading.Event,
        env: str = "mainnet",
    ):
        super().__init__(daemon=True, name=f"ws-nado-{instrument}")
        self.instrument = instrument
        self._product_id = product_id
        self._book = book
        self._lock = lock
        self._stop_ev = stop_event
        self._url = _NADO_WS_ENDPOINTS.get(env, _NADO_WS_ENDPOINTS["mainnet"])
        self.connected = False
        self._reconnect_delay = 1.0

    def run(self) -> None:
        while not self._stop_ev.is_set():
            try:
                self._connect_and_listen()
            except Exception as exc:
                self.connected = False
                if self._stop_ev.is_set():
                    break
                logger.warning(
                    "NADO WS (%s) error: %s — reconnecting in %.0fs",
                    self.instrument, exc, self._reconnect_delay,
                )
                self._stop_ev.wait(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 30.0)

    def _connect_and_listen(self) -> None:
        logger.info("NADO WS connecting: %s (product_id=%d)", self._url, self._product_id)

        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        with ws_sync.connect(self._url, ssl=ssl_ctx, close_timeout=5) as conn:
            # Subscribe to book_depth stream for this product
            subscribe_msg = json.dumps({
                "method": "subscribe",
                "stream": {
                    "type": "book_depth",
                    "product_id": self._product_id,
                },
                "id": self._product_id,
            })
            conn.send(subscribe_msg)
            logger.info("NADO WS subscribe sent: book_depth product_id=%d", self._product_id)

            # Read subscription confirmation
            try:
                resp_raw = conn.recv(timeout=10)
                resp = json.loads(resp_raw)
                if resp.get("error"):
                    raise RuntimeError(f"NADO subscribe error: {resp}")
                logger.info("NADO WS subscribed: %s", resp)
            except TimeoutError:
                logger.warning("NADO WS subscribe response timeout")

            self.connected = True
            self._reconnect_delay = 1.0

            while not self._stop_ev.is_set():
                try:
                    raw = conn.recv(timeout=30)
                except TimeoutError:
                    try:
                        conn.ping()
                    except Exception:
                        break
                    continue

                self._handle_message(raw)

    def _handle_message(self, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        # NADO book_depth: bids/asks are at the top level (no "data" wrapper)
        data = msg.get("data") or (msg if "bids" in msg else None)
        if not data:
            return

        bids_raw = data.get("bids", [])
        asks_raw = data.get("asks", [])

        if not bids_raw and not asks_raw:
            return

        with self._lock:
            # NADO book_depth sends diffs: [price_x18, size_x18]
            # size_x18 == "0" means level removed; otherwise upsert
            if bids_raw:
                self._apply_nado_delta(self._book.bids, bids_raw, reverse=True)
            if asks_raw:
                self._apply_nado_delta(self._book.asks, asks_raw, reverse=False)

            self._book.last_update_ts = time.time()
            self._book.update_count += 1
            self._book.is_synced = True

    @staticmethod
    def _apply_nado_delta(book: list, updates: list, reverse: bool) -> None:
        """Apply NADO incremental book_depth updates.

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


# ---------------------------------------------------------------------------
# Public API: OrderbookFeedManager
# ---------------------------------------------------------------------------

class OrderbookFeedManager:
    """Manages WebSocket feeds for both exchanges.

    Usage:
        mgr = OrderbookFeedManager(grvt_env="testnet")
        mgr.start({"extended": "SOL-USD", "grvt": "SOL_USDT_Perp"})
        book = mgr.get_book("extended", "SOL-USD")
        mgr.stop()
    """

    def __init__(self, grvt_env: str = "testnet", stale_ms: int = 5000, nado_env: str = "mainnet"):
        self._grvt_env = grvt_env
        self._nado_env = nado_env
        self._stale_ms = stale_ms
        self._nado_product_ids: dict[str, int] = {}  # symbol -> product_id cache
        self._books: dict[str, OrderbookSnapshot] = {}   # key = "exchange:instrument"
        self._locks: dict[str, threading.Lock] = {}
        self._threads: dict[str, threading.Thread] = {}  # key = "exchange:instrument"
        self._stop_events: dict[str, threading.Event] = {}  # per-feed stop events
        self._global_stop = threading.Event()  # for stop-all
        self._started = False

    def _key(self, exchange: str, instrument: str) -> str:
        return f"{exchange}:{instrument}"

    def _resolve_nado_product_id(self, instrument: str) -> int | None:
        """Resolve a NADO symbol to its integer product_id via REST."""
        if instrument in self._nado_product_ids:
            return self._nado_product_ids[instrument]
        try:
            import requests
            base = _NADO_WS_ENDPOINTS.get(self._nado_env, _NADO_WS_ENDPOINTS["mainnet"])
            # Derive REST URL from WS URL
            rest_url = base.replace("wss://", "https://").rsplit("/subscribe", 1)[0]
            resp = requests.get(f"{rest_url}/symbols", timeout=10, verify=False)
            resp.raise_for_status()
            for s in resp.json():
                sym = s.get("symbol", "")
                pid = s.get("product_id")
                if pid is not None:
                    self._nado_product_ids[sym] = pid
            return self._nado_product_ids.get(instrument)
        except Exception as exc:
            logger.warning("Failed to resolve NADO product_id for %s: %s", instrument, exc)
            return None

    def start(self, instruments: dict[str, str]) -> None:
        """Start WS feed threads.

        Args:
            instruments: {"extended": "SOL-USD", "grvt": "SOL_USDT_Perp"}
        """
        if self._started:
            logger.warning("OrderbookFeedManager already started")
            return

        self._global_stop.clear()

        for exchange, instrument in instruments.items():
            self.add_feed(exchange, instrument)

        self._started = True

    def stop(self) -> None:
        """Stop all WS feed threads."""
        self._global_stop.set()
        for ev in self._stop_events.values():
            ev.set()
        for t in self._threads.values():
            t.join(timeout=5)
        self._threads.clear()
        self._stop_events.clear()
        self._started = False
        logger.info("All WS feeds stopped")

    def add_feed(self, exchange: str, instrument: str) -> None:
        """Add a single WS feed dynamically. No-op if already running."""
        key = self._key(exchange, instrument)
        if key in self._threads and self._threads[key].is_alive():
            return  # already running

        book = OrderbookSnapshot()
        lock = threading.Lock()
        stop_ev = threading.Event()
        self._books[key] = book
        self._locks[key] = lock
        self._stop_events[key] = stop_ev

        if exchange == "extended":
            t = _ExtendedFeedThread(instrument, book, lock, stop_ev)
        elif exchange == "grvt":
            t = _GrvtFeedThread(instrument, self._grvt_env, book, lock, stop_ev)
        elif exchange == "nado":
            product_id = self._resolve_nado_product_id(instrument)
            if product_id is None:
                logger.warning("Cannot resolve NADO product_id for %s — skipping WS feed", instrument)
                return
            t = _NadoFeedThread(instrument, product_id, book, lock, stop_ev, env=self._nado_env)
        else:
            logger.warning("Unknown exchange for WS feed: %s — skipping", exchange)
            return

        self._threads[key] = t
        t.start()
        logger.info("Added WS feed: %s → %s", exchange, instrument)

    def remove_feed(self, exchange: str, instrument: str) -> None:
        """Stop and remove a single WS feed. No-op if not running."""
        key = self._key(exchange, instrument)
        stop_ev = self._stop_events.pop(key, None)
        if stop_ev:
            stop_ev.set()
        t = self._threads.pop(key, None)
        if t:
            t.join(timeout=5)
        self._books.pop(key, None)
        self._locks.pop(key, None)
        logger.info("Removed WS feed: %s → %s", exchange, instrument)

    def get_book(self, exchange: str, instrument: str) -> dict | None:
        """Get cached orderbook in the same format as REST fetch_order_book.

        Returns None if no data is available yet.
        """
        key = self._key(exchange, instrument)
        book = self._books.get(key)
        lock = self._locks.get(key)
        if not book or not lock:
            return None

        with lock:
            if not book.bids and not book.asks:
                return None
            return {
                "bids": [list(b) for b in book.bids],
                "asks": [list(a) for a in book.asks],
                "timestamp": int(book.last_update_ts * 1000),
                "datetime": None,
                "_source": "websocket",
                "_update_count": book.update_count,
                "_is_synced": book.is_synced,
            }

    def get_books_atomic(
        self,
        exchange_a: str, instrument_a: str,
        exchange_b: str, instrument_b: str,
    ) -> tuple[dict | None, dict | None]:
        """Read both orderbooks under their respective locks without yielding.

        Minimises the window for price movement between reading the two books.
        Locks are always acquired in sorted key order to prevent deadlocks.
        Returns (book_a, book_b) — either may be None if not available.
        """
        key_a = self._key(exchange_a, instrument_a)
        key_b = self._key(exchange_b, instrument_b)
        book_a = self._books.get(key_a)
        book_b = self._books.get(key_b)
        lock_a = self._locks.get(key_a)
        lock_b = self._locks.get(key_b)

        if not book_a or not lock_a or not book_b or not lock_b:
            # Fall back to individual reads
            return (self.get_book(exchange_a, instrument_a),
                    self.get_book(exchange_b, instrument_b))

        # Acquire locks in sorted order to prevent deadlocks
        first_key, second_key = sorted([key_a, key_b])
        first_lock = self._locks[first_key]
        second_lock = self._locks[second_key]

        def _snap(bk: OrderbookSnapshot) -> dict | None:
            if not bk.bids and not bk.asks:
                return None
            return {
                "bids": [list(b) for b in bk.bids],
                "asks": [list(a) for a in bk.asks],
                "timestamp": int(bk.last_update_ts * 1000),
                "datetime": None,
                "_source": "websocket",
                "_update_count": bk.update_count,
                "_is_synced": bk.is_synced,
            }

        with first_lock:
            with second_lock:
                return (_snap(book_a), _snap(book_b))

    def get_bbo(self, exchange: str, instrument: str) -> tuple[float, float] | None:
        """Get best bid and ask. Returns (bid, ask) or None."""
        key = self._key(exchange, instrument)
        book = self._books.get(key)
        lock = self._locks.get(key)
        if not book or not lock:
            return None

        with lock:
            if not book.bids or not book.asks:
                return None
            return (book.bids[0][0], book.asks[0][0])

    def is_stale(self, exchange: str, instrument: str, max_age_ms: int | None = None) -> bool:
        """Check if cached data is too old."""
        max_ms = max_age_ms if max_age_ms is not None else self._stale_ms
        key = self._key(exchange, instrument)
        book = self._books.get(key)
        if not book or book.last_update_ts == 0:
            return True
        age_ms = (time.time() - book.last_update_ts) * 1000
        return age_ms > max_ms

    def status(self) -> dict:
        """Return connection status for all feeds."""
        result = {}
        for key, t in self._threads.items():
            name = t.name
            connected = getattr(t, "connected", False)
            instrument = getattr(t, "instrument", "?")
            if "nado" in name:
                exchange = "nado"
            elif "extended" in name:
                exchange = "extended"
            else:
                exchange = "grvt"
            book = self._books.get(key)
            age_ms = (time.time() - book.last_update_ts) * 1000 if book and book.last_update_ts else None
            result[key] = {
                "exchange": exchange,
                "instrument": instrument,
                "connected": connected,
                "is_synced": book.is_synced if book else False,
                "update_count": book.update_count if book else 0,
                "last_update_age_ms": round(age_ms, 0) if age_ms is not None else None,
                "stale": self.is_stale(exchange, instrument),
            }
        return result
