"""Extended Exchange client — public REST + x10 SDK for trading.

Public data (order book, markets): direct REST calls (no auth needed).
Trading (orders, positions): x10-python-trading-starknet SDK.
Conforms to the ExchangeClient protocol defined in app/exchange.py.
"""

import asyncio
import logging
import uuid
from decimal import Decimal
from typing import Literal

import requests
import urllib3

logger = logging.getLogger("tradeautonom.extended_client")

_DEFAULT_BASE_URL = "https://api.starknet.extended.exchange/api/v1"
_USER_AGENT = "TradeAutonom/1.0"
# Market order price slippage buffer (per Extended docs: 0.75%)
_MARKET_ORDER_SLIPPAGE = Decimal("0.0075")

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _run_async(coro):
    """Run an async coroutine from sync code."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # Already inside an event loop (e.g. FastAPI) — use a new thread
        import concurrent.futures
        with concurrent.futures.ThreadPoolExecutor() as pool:
            return pool.submit(asyncio.run, coro).result()
    return asyncio.run(coro)


class ExtendedClient:
    """Extended Exchange client with public data + trading support."""

    def __init__(
        self,
        base_url: str = _DEFAULT_BASE_URL,
        api_key: str = "",
        public_key: str = "",
        private_key: str = "",
        vault: int = 0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # REST session for public endpoints
        self._session = requests.Session()
        self._session.verify = False
        self._session.headers.update({"User-Agent": _USER_AGENT})
        if api_key:
            self._session.headers.update({"X-Api-Key": api_key})

        # x10 SDK trading client (lazy init — only if credentials provided)
        self._trading_client = None
        self._api_key = api_key
        self._public_key = public_key
        self._private_key = private_key
        self._vault = vault
        self._has_credentials = bool(api_key and private_key and public_key and vault)

        if self._has_credentials:
            self._init_trading_client()
            logger.info("ExtendedClient initialised WITH trading (base=%s)", self._base_url)
        else:
            logger.info("ExtendedClient initialised READ-ONLY (base=%s)", self._base_url)

    def _init_trading_client(self) -> None:
        """Initialise the x10 SDK trading client."""
        from x10.perpetual.accounts import StarkPerpetualAccount
        from x10.perpetual.configuration import MAINNET_CONFIG, TESTNET_CONFIG
        from x10.perpetual.trading_client import PerpetualTradingClient

        account = StarkPerpetualAccount(
            vault=self._vault,
            private_key=self._private_key,
            public_key=self._public_key,
            api_key=self._api_key,
        )
        # Choose config based on base URL
        if "sepolia" in self._base_url or "testnet" in self._base_url:
            config = TESTNET_CONFIG
        else:
            config = MAINNET_CONFIG

        self._trading_client = PerpetualTradingClient(config, account)
        logger.info("x10 SDK trading client initialised (vault=%s)", self._vault)

    def _require_trading(self) -> None:
        if not self._trading_client:
            raise RuntimeError(
                "Extended trading not available — set EXTENDED_API_KEY, "
                "EXTENDED_PUBLIC_KEY, EXTENDED_PRIVATE_KEY and EXTENDED_VAULT in .env"
            )

    # -- Protocol ---------------------------------------------------------

    @property
    def name(self) -> str:
        return "extended"

    @property
    def can_trade(self) -> bool:
        return self._has_credentials

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

        return {"bids": bids[:limit], "asks": asks[:limit]}

    def fetch_markets(self) -> list[dict]:
        """Return list of available markets with normalised keys."""
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

    # -- Trading ----------------------------------------------------------

    def create_market_order(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        amount: Decimal,
        expected_price: float | None = None,
    ) -> dict:
        """Place a market order (emulated as IOC limit with slippage buffer).

        Extended does not support native market orders. Per their docs,
        market orders are IOC limits with price = best_price * (1 ± 0.75%).
        """
        self._require_trading()
        from x10.perpetual.orders import OrderSide, TimeInForce

        # Determine aggressive limit price from orderbook
        if expected_price:
            ref_price = Decimal(str(expected_price))
        else:
            book = self.fetch_order_book(symbol, limit=1)
            if side == "buy" and book["asks"]:
                ref_price = Decimal(str(book["asks"][0][0]))
            elif side == "sell" and book["bids"]:
                ref_price = Decimal(str(book["bids"][0][0]))
            else:
                raise RuntimeError(f"No {'asks' if side == 'buy' else 'bids'} in {symbol} orderbook")

        if side == "buy":
            limit_price = ref_price * (1 + _MARKET_ORDER_SLIPPAGE)
        else:
            limit_price = ref_price * (1 - _MARKET_ORDER_SLIPPAGE)

        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

        logger.info(
            "Extended MARKET %s %s qty=%s price=%s (ref=%s)",
            side.upper(), symbol, amount, limit_price.quantize(Decimal("0.01")), ref_price,
        )

        result = _run_async(
            self._trading_client.place_order(
                market_name=symbol,
                amount_of_synthetic=amount,
                price=limit_price,
                side=order_side,
                time_in_force=TimeInForce.IOC,
            )
        )

        logger.info("Extended order placed: %s", result)
        return {"id": str(getattr(result, "id", None)), "external_id": str(getattr(result, "external_id", None))}

    def create_limit_order(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        amount: Decimal,
        price: Decimal,
        post_only: bool = False,
        reduce_only: bool = False,
    ) -> dict:
        """Place a limit order on Extended."""
        self._require_trading()
        from x10.perpetual.orders import OrderSide

        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

        logger.info(
            "Extended LIMIT %s %s qty=%s @ %s (post_only=%s reduce_only=%s)",
            side.upper(), symbol, amount, price, post_only, reduce_only,
        )

        result = _run_async(
            self._trading_client.place_order(
                market_name=symbol,
                amount_of_synthetic=amount,
                price=price,
                side=order_side,
                post_only=post_only,
                reduce_only=reduce_only,
            )
        )

        logger.info("Extended limit order placed: %s", result)
        return {"id": str(getattr(result, "id", None)), "external_id": str(getattr(result, "external_id", None))}

    def fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        """Fetch open positions, normalised to match GRVT format."""
        self._require_trading()

        positions = _run_async(self._trading_client.account.get_positions())

        result = []
        for p in positions.data:
            market = getattr(p, "market", "")
            if symbols and market not in symbols:
                continue
            side = str(getattr(p, "side", "")).upper()
            size = float(getattr(p, "size", 0))
            result.append({
                "instrument": market,
                "size": size if side == "LONG" else -size,
                "entry_price": float(getattr(p, "open_price", 0)),
                "mark_price": float(getattr(p, "mark_price", 0)),
                "unrealised_pnl": float(getattr(p, "unrealised_pnl", 0)),
                "leverage": float(getattr(p, "leverage", 1)),
                "side": side,
            })
        return result

    def fetch_fees(self, symbol: str) -> dict:
        """Fetch current fee rates for a market."""
        url = f"{self._base_url}/user/fees?market={symbol}"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status", "").upper() != "OK":
            raise RuntimeError(f"Extended fees error: {data}")
        fees = data.get("data", [])
        return fees[0] if fees else {}
