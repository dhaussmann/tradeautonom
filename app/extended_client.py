"""Extended Exchange client — public REST API for market data.

No authentication required for order books and market listings.
Conforms to the ExchangeClient protocol defined in app/exchange.py.
"""

import logging

import requests
import urllib3

logger = logging.getLogger("tradeautonom.extended_client")

_DEFAULT_BASE_URL = "https://api.starknet.extended.exchange/api/v1"
_USER_AGENT = "TradeAutonom/1.0"

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class ExtendedClient:
    """Read-only client for Extended Exchange public market data."""

    def __init__(self, base_url: str = _DEFAULT_BASE_URL) -> None:
        self._base_url = base_url.rstrip("/")
        self._session = requests.Session()
        self._session.verify = False
        self._session.headers.update({"User-Agent": _USER_AGENT})
        logger.info("ExtendedClient initialised (base=%s)", self._base_url)

    # -- Protocol ---------------------------------------------------------

    @property
    def name(self) -> str:
        return "extended"

    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        """Fetch order book and normalise to [[price, qty], ...] format.

        Extended returns: {bid: [{price, qty}, ...], ask: [{price, qty}, ...]}
        We normalise to: {bids: [[price, qty], ...], asks: [[price, qty], ...]}
        """
        url = f"{self._base_url}/info/markets/{symbol}/orderbook"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "OK":
            error = data.get("error", {})
            raise RuntimeError(
                f"Extended order book error for {symbol}: "
                f"{error.get('code', '?')} — {error.get('message', str(data))}"
            )

        book = data.get("data", {})
        bids = [[lv["price"], lv["qty"]] for lv in book.get("bid", [])]
        asks = [[lv["price"], lv["qty"]] for lv in book.get("ask", [])]

        # Respect limit
        return {"bids": bids[:limit], "asks": asks[:limit]}

    def fetch_markets(self) -> list[dict]:
        """Return list of available markets with normalised keys.

        Extended returns: [{name, assetName, status, tradingConfig, ...}, ...]
        We normalise to: [{symbol, name, status, ...}, ...]
        """
        url = f"{self._base_url}/info/markets"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status", "").upper() != "OK":
            raise RuntimeError(f"Extended markets error: {data}")

        markets = []
        for m in data.get("data", []):
            if m.get("status") != "ACTIVE":
                continue
            markets.append({
                "symbol": m["name"],           # e.g. "BTC-USD"
                "name": m["name"],
                "asset": m.get("assetName"),   # e.g. "BTC"
                "status": m.get("status"),
                "min_order_size": m.get("tradingConfig", {}).get("minOrderSize"),
                "max_leverage": m.get("tradingConfig", {}).get("maxLeverage"),
            })
        return markets
