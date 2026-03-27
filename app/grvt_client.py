import logging
import urllib3
from decimal import Decimal
from functools import wraps

import requests
from pysdk.grvt_ccxt import GrvtCcxt
from pysdk.grvt_ccxt_env import GrvtEnv, get_grvt_endpoint
from pysdk.grvt_ccxt_utils import rand_uint32

from app.config import Settings

logger = logging.getLogger("tradeautonom.grvt_client")


def _patch_session_no_ssl_verify():
    """Monkey-patch requests.Session so new sessions default to verify=False.

    GRVT testnet uses a self-signed certificate chain, which causes
    SSL verification to fail. This context manager patches Session.init
    to disable verification and suppresses the resulting InsecureRequestWarning.
    """
    _original_init = requests.Session.__init__

    @wraps(_original_init)
    def _patched_init(self, *args, **kwargs):
        _original_init(self, *args, **kwargs)
        self.verify = False

    requests.Session.__init__ = _patched_init
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    return _original_init


class GrvtClient:
    """Thin wrapper around grvt-pysdk's GrvtCcxt for authenticated trading."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._env = GrvtEnv(settings.grvt_env)
        params = {
            "api_key": settings.grvt_api_key,
            "trading_account_id": settings.grvt_trading_account_id,
            "private_key": settings.grvt_private_key,
        }

        # GRVT uses self-signed certs on all environments (incl. prod).
        # The patch stays active because the SDK creates new Sessions on
        # every cookie refresh (get_cookie_with_expiration).
        _patch_session_no_ssl_verify()

        self._min_size_cache: dict[str, Decimal] = {}
        self._tick_size_cache: dict[str, Decimal] = {}
        self._api = GrvtCcxt(
            self._env,
            logger,
            parameters=params,
            order_book_ccxt_format=True,
        )
        logger.info("GrvtClient initialised (env=%s)", settings.grvt_env)

    # -- Protocol ---------------------------------------------------------

    @property
    def name(self) -> str:
        return "grvt"

    # ------------------------------------------------------------------
    # Market data helpers
    # ------------------------------------------------------------------

    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        """Return order book dict with 'bids' and 'asks' lists.

        The GRVT SDK's convert_grvt_ob_to_ccxt crashes with KeyError when
        'event_time' is absent from the response (intermittent API behaviour).
        We catch that and fall back to manually converting the raw result.

        GRVT API requires depth >= 10; smaller values return "Depth is invalid".
        """
        # Enforce minimum depth of 10 (GRVT API requirement)
        safe_limit = max(limit, 10)
        try:
            return self._api.fetch_order_book(symbol, limit=safe_limit)
        except KeyError as exc:
            if "event_time" not in str(exc):
                raise
            logger.debug("fetch_order_book: event_time missing, falling back to raw conversion (%s)", exc)
            # Bypass the SDK converter — call the endpoint directly and convert manually.
            # GRVT API requires depth >= 10; smaller values return 400 "Depth is invalid".
            from pysdk.grvt_ccxt_env import get_grvt_endpoint
            path = get_grvt_endpoint(self._env, "GET_ORDER_BOOK")
            payload = {"instrument": symbol, "aggregate": 1}
            if safe_limit:
                payload["depth"] = safe_limit
            response: dict = self._api._auth_and_post(path, payload=payload)
            raw = response.get("result", {})
            return {
                "symbol": raw.get("instrument", symbol),
                "bids": [[b["price"], b["size"]] for b in raw.get("bids", [])],
                "asks": [[a["price"], a["size"]] for a in raw.get("asks", [])],
                "timestamp": None,
                "datetime": None,
            }

    def fetch_ticker(self, symbol: str) -> dict:
        return self._api.fetch_ticker(symbol)

    def fetch_mini_ticker(self, symbol: str) -> dict:
        return self._api.fetch_mini_ticker(symbol)

    def fetch_markets(self) -> list[dict]:
        """Return normalised market list with 'symbol' key."""
        raw = self._api.fetch_all_markets()
        markets = []
        for m in raw:
            sym = m.get("instrument", m.get("symbol", ""))
            markets.append({
                "symbol": sym,
                "name": sym,
                "base": m.get("base"),
                "quote": m.get("quote"),
                "kind": m.get("kind"),
                "tick_size": m.get("tick_size"),
                "min_size": m.get("min_size"),
            })
            if m.get("min_size"):
                self._min_size_cache[sym] = Decimal(str(m["min_size"]))
            if m.get("tick_size"):
                self._tick_size_cache[sym] = Decimal(str(m["tick_size"]))
        return markets

    def get_min_order_size(self, symbol: str) -> Decimal:
        """Return minimum order size for the symbol from GRVT market data."""
        if symbol not in self._min_size_cache:
            self.fetch_markets()
        return self._min_size_cache.get(symbol, Decimal("0"))

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: Decimal,
        client_order_id: int | None = None,
        slippage_pct: float | None = None,  # accepted but unused — GRVT handles slippage natively
    ) -> dict:
        """Place a market order. Returns the API response dict."""
        cid = client_order_id or rand_uint32()
        resp = self._api.create_order(
            symbol=symbol,
            order_type="market",
            side=side,
            amount=amount,
            params={"client_order_id": cid},
        )
        if not resp:
            raise RuntimeError(f"Order rejected by GRVT (empty response). Check server logs for details.")
        logger.info("Market order sent: symbol=%s side=%s amount=%s cid=%s", symbol, side, amount, cid)
        return resp

    def create_aggressive_limit_order(
        self,
        symbol: str,
        side: str,
        amount: Decimal,
        offset_ticks: int = 2,
        best_price: float | None = None,
        client_order_id: int | None = None,
    ) -> dict:
        """Place an aggressive limit order: best price + offset ticks for near-certain fill.

        BUY:  limit = best_ask + offset_ticks * tick_size  (cross the spread aggressively)
        SELL: limit = best_bid - offset_ticks * tick_size

        If best_price is provided it is used directly, avoiding an extra order book fetch.
        """
        cid = client_order_id or rand_uint32()
        tick = self.get_tick_size(symbol)

        if best_price is not None:
            best = Decimal(str(best_price))
        else:
            book = self.fetch_order_book(symbol, limit=1)
            if side == "buy":
                if not book.get("asks"):
                    raise RuntimeError(f"No asks in {symbol} orderbook")
                best = Decimal(str(book["asks"][0][0]))
            else:
                if not book.get("bids"):
                    raise RuntimeError(f"No bids in {symbol} orderbook")
                best = Decimal(str(book["bids"][0][0]))

        if side == "buy":
            limit_price = float(best + tick * offset_ticks)
        else:
            limit_price = float(best - tick * offset_ticks)

        logger.info(
            "Aggressive limit: %s %s qty=%s best=%s limit=%s offset=%d ticks tick=%s",
            side.upper(), symbol, amount, best, limit_price, offset_ticks, tick,
        )
        return self.create_limit_order(
            symbol=symbol, side=side, amount=amount,
            price=limit_price, client_order_id=cid,
        )

    def get_tick_size(self, symbol: str) -> Decimal:
        """Return tick size for the symbol from GRVT market data."""
        if not self._min_size_cache:
            self.fetch_markets()
        # tick_size is stored separately; fall back to 0.01
        return self._tick_size_cache.get(symbol, Decimal("0.01"))

    def check_order_fill(self, client_order_id: int) -> dict:
        """Check if an order has been filled. Returns {filled: bool, status: str, ...}.

        fetch_order returns the raw API response: {"result": {...}, "request_id": ...}.
        The order details are nested under "result", and status under "result.state.status".
        """
        try:
            raw = self.fetch_order(client_order_id)
            # SDK fetch_order returns raw API response — unwrap "result"
            order = raw.get("result", raw) if isinstance(raw, dict) else {}
            state = order.get("state", {}) if order else {}
            status = state.get("status", "").upper() if state else ""
            traded = state.get("traded_size", ["0"])
            traded_qty = float(traded[0]) if traded else 0.0
            filled = status in ("FILLED", "CLOSED") or (status == "PENDING" and traded_qty > 0.0)
            logger.info(
                "check_order_fill(%s): status=%s traded=%s filled=%s",
                client_order_id, status, traded_qty, filled,
            )
            return {"filled": filled, "status": status, "traded_qty": traded_qty, "order": order}
        except Exception as exc:
            logger.warning("check_order_fill(%s) error: %s", client_order_id, exc)
            return {"filled": False, "status": "ERROR", "error": str(exc)}

    def create_limit_order(
        self,
        symbol: str,
        side: str,
        amount: Decimal,
        price: float,
        client_order_id: int | None = None,
        time_in_force: str = "IMMEDIATE_OR_CANCEL",
    ) -> dict:
        """Place a limit IOC order. Returns the API response dict.

        Uses IMMEDIATE_OR_CANCEL by default so the order fills instantly or is
        cancelled — it never lingers in the book as PENDING.
        """
        cid = client_order_id or rand_uint32()
        resp = self._api.create_order(
            symbol=symbol,
            order_type="limit",
            side=side,
            amount=amount,
            price=price,
            params={"client_order_id": cid, "time_in_force": time_in_force},
        )
        if not resp:
            raise RuntimeError(f"Order rejected by GRVT (empty response). Check server logs for details.")

        state = resp.get("state", {})
        status = state.get("status", "UNKNOWN")
        traded = state.get("traded_size", ["0.0"])
        traded_qty = float(traded[0]) if traded else 0.0
        logger.info(
            "Limit order sent: symbol=%s side=%s amount=%s price=%s cid=%s tif=%s → status=%s traded=%s",
            symbol, side, amount, price, cid, time_in_force, status, traded_qty,
        )

        # IOC orders that don't fill immediately come back PENDING then get cancelled.
        # If traded_qty == 0 and status is not FILLED/CLOSED, treat as not filled.
        if status not in ("FILLED", "CLOSED", "PENDING") or (status == "PENDING" and traded_qty == 0.0):
            logger.warning(
                "GRVT limit order %s %s may not have filled: status=%s traded=%s — "
                "order placed but fill not confirmed", side.upper(), symbol, status, traded_qty,
            )

        return resp

    def fetch_order(self, client_order_id: int) -> dict:
        """Fetch order by client_order_id. Returns raw API response (result unwrapping
        is done in check_order_fill to preserve backward-compat with other callers)."""
        return self._api.fetch_order(params={"client_order_id": client_order_id})

    def fetch_open_orders(self, symbol: str) -> list[dict]:
        return self._api.fetch_open_orders(symbol=symbol, params={"kind": "PERPETUAL"})

    def cancel_order(self, order_id: str) -> bool:
        return self._api.cancel_order(id=order_id, params={"time_to_live_ms": "1000"})

    def cancel_all_orders(self) -> bool:
        return self._api.cancel_all_orders()

    def fetch_positions(self, symbols: list[str]) -> list[dict]:
        return self._api.fetch_positions(symbols=symbols)

    def get_account_summary(self) -> dict:
        return self._api.get_account_summary(type="sub-account")

    # ------------------------------------------------------------------
    # Leverage
    # ------------------------------------------------------------------

    def _trade_base_url(self) -> str:
        """Derive the trade API base URL from the CREATE_ORDER endpoint."""
        # e.g. https://trades.testnet.grvt.io/full/v1/create_order -> https://trades.testnet.grvt.io
        url = get_grvt_endpoint(self._env, "CREATE_ORDER")
        return url.rsplit("/full/", 1)[0]

    def get_all_leverage(self) -> list[dict]:
        """Get initial leverage settings for all instruments."""
        path = self._trade_base_url() + "/full/v1/get_all_initial_leverage"
        payload = {"sub_account_id": self.settings.grvt_trading_account_id}
        resp = self._api._auth_and_post(path, payload)
        return resp.get("results", [])

    def set_leverage(self, instrument: str, leverage: int) -> bool:
        """Set initial leverage for a specific instrument."""
        path = self._trade_base_url() + "/full/v1/set_initial_leverage"
        payload = {
            "sub_account_id": self.settings.grvt_trading_account_id,
            "instrument": instrument,
            "leverage": str(leverage),
        }
        resp = self._api._auth_and_post(path, payload)
        success = resp.get("success", False)
        if success:
            logger.info("Leverage set: %s -> %dx", instrument, leverage)
        else:
            logger.warning("Failed to set leverage: %s -> %dx, response: %s", instrument, leverage, resp)
        return success
