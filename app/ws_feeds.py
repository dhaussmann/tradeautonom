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
        with ws_sync.connect(self._url, close_timeout=5) as conn:
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
        bids_raw = data.get("b", [])
        asks_raw = data.get("a", [])

        bids = [[float(b["p"]), float(b["q"])] for b in bids_raw if "p" in b]
        asks = [[float(a["p"]), float(a["q"])] for a in asks_raw if "p" in a]

        if not bids and not asks:
            return

        with self._lock:
            if msg_type == "SNAPSHOT":
                # Full replacement
                if bids:
                    self._book.bids = sorted(bids, key=lambda x: -x[0])
                if asks:
                    self._book.asks = sorted(asks, key=lambda x: x[0])
            else:
                # DELTA: merge updates into existing book
                if bids:
                    self._apply_delta(self._book.bids, bids, reverse=True)
                if asks:
                    self._apply_delta(self._book.asks, asks, reverse=False)
            self._book.last_update_ts = time.time()
            self._book.update_count += 1

    @staticmethod
    def _apply_delta(book: list, updates: list, reverse: bool) -> None:
        """Apply delta updates: size>0 upserts, size==0 removes."""
        price_map = {level[0]: level for level in book}
        for price, size in updates:
            if size == 0:
                price_map.pop(price, None)
            else:
                price_map[price] = [price, size]
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

        bids_raw = feed.get("bids", [])
        asks_raw = feed.get("asks", [])

        # GRVT format: [{"price": "123.45", "size": "0.5", "num_orders": 1}, ...]
        bids = [[float(b["price"]), float(b["size"])] for b in bids_raw if "price" in b]
        asks = [[float(a["price"]), float(a["size"])] for a in asks_raw if "price" in a]

        if not bids and not asks:
            return

        with self._lock:
            if bids:
                self._book.bids = sorted(bids, key=lambda x: -x[0])
            if asks:
                self._book.asks = sorted(asks, key=lambda x: x[0])
            self._book.last_update_ts = time.time()
            self._book.update_count += 1


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

    def __init__(self, grvt_env: str = "testnet", stale_ms: int = 5000):
        self._grvt_env = grvt_env
        self._stale_ms = stale_ms
        self._books: dict[str, OrderbookSnapshot] = {}   # key = "exchange:instrument"
        self._locks: dict[str, threading.Lock] = {}
        self._threads: dict[str, threading.Thread] = {}  # key = "exchange:instrument"
        self._stop_events: dict[str, threading.Event] = {}  # per-feed stop events
        self._global_stop = threading.Event()  # for stop-all
        self._started = False

    def _key(self, exchange: str, instrument: str) -> str:
        return f"{exchange}:{instrument}"

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
            }

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
            exchange = "extended" if "extended" in name else "grvt"
            book = self._books.get(key)
            age_ms = (time.time() - book.last_update_ts) * 1000 if book and book.last_update_ts else None
            result[key] = {
                "exchange": exchange,
                "instrument": instrument,
                "connected": connected,
                "update_count": book.update_count if book else 0,
                "last_update_age_ms": round(age_ms, 0) if age_ms is not None else None,
                "stale": self.is_stale(exchange, instrument),
            }
        return result
