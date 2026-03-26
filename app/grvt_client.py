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
        """Return order book dict with 'bids' and 'asks' lists."""
        return self._api.fetch_order_book(symbol, limit=limit)

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

    def create_limit_order(
        self,
        symbol: str,
        side: str,
        amount: Decimal,
        price: float,
        client_order_id: int | None = None,
    ) -> dict:
        """Place a limit order. Returns the API response dict."""
        cid = client_order_id or rand_uint32()
        resp = self._api.create_order(
            symbol=symbol,
            order_type="limit",
            side=side,
            amount=amount,
            price=price,
            params={"client_order_id": cid},
        )
        if not resp:
            raise RuntimeError(f"Order rejected by GRVT (empty response). Check server logs for details.")
        logger.info(
            "Limit order sent: symbol=%s side=%s amount=%s price=%s cid=%s",
            symbol, side, amount, price, cid,
        )
        return resp

    def fetch_order(self, client_order_id: int) -> dict:
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
