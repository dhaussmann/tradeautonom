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


import threading

# A single background thread with a persistent event loop, shared across all
# ExtendedClient instances. This avoids "Event loop is closed" errors that occur
# when asyncio.run() is called multiple times from a ThreadPoolExecutor inside
# an already-running FastAPI event loop.
_bg_loop: asyncio.AbstractEventLoop | None = None
_bg_loop_lock = threading.Lock()


def _get_bg_loop() -> asyncio.AbstractEventLoop:
    global _bg_loop
    with _bg_loop_lock:
        if _bg_loop is None or _bg_loop.is_closed():
            _bg_loop = asyncio.new_event_loop()
            t = threading.Thread(target=_bg_loop.run_forever, daemon=True)
            t.start()
        return _bg_loop


def _run_async(coro):
    """Run an async coroutine from sync code using a persistent background loop."""
    loop = _get_bg_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=30)


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
        self._tick_size_cache: dict[str, Decimal] = {}    # symbol -> minPriceChange
        self._min_size_cache: dict[str, Decimal] = {}     # symbol -> minOrderSize
        self._api_key = api_key
        self._public_key = public_key
        self._private_key = private_key
        self._vault = vault
        self._has_credentials = bool(api_key and private_key and public_key and vault is not None)

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

    def _load_market_config(self) -> None:
        """Fetch all market configs and populate tick size + min order size caches."""
        url = f"{self._base_url}/info/markets"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        for m in data.get("data", []):
            name = m["name"]
            tc = m.get("tradingConfig", {})
            tick = tc.get("minPriceChange")
            if tick:
                self._tick_size_cache[name] = Decimal(str(tick))
            min_size = tc.get("minOrderSize")
            if min_size:
                self._min_size_cache[name] = Decimal(str(min_size))

    def _get_tick_size(self, symbol: str) -> Decimal:
        if symbol not in self._tick_size_cache:
            self._load_market_config()
        return self._tick_size_cache.get(symbol, Decimal("0.01"))

    def get_min_order_size(self, symbol: str) -> Decimal:
        """Return the minimum order size for a symbol (0 if unknown)."""
        if symbol not in self._min_size_cache:
            self._load_market_config()
        return self._min_size_cache.get(symbol, Decimal("0"))

    def _round_to_tick(self, price: Decimal, symbol: str) -> Decimal:
        """Round price down (sell) or up (buy) to the instrument's tick size."""
        tick = self._get_tick_size(symbol)
        # quantize with ROUND_DOWN — caller adjusts direction via slippage buffer
        return (price / tick).to_integral_value(rounding="ROUND_DOWN") * tick

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
        slippage_pct: float | None = None,
    ) -> dict:
        """Place a market order (emulated as IOC limit with slippage buffer).

        Extended does not support native market orders. The limit price is set
        to best_price * (1 ± slippage_pct/100). Falls back to _MARKET_ORDER_SLIPPAGE
        (0.75%) if no slippage_pct is given.
        """
        self._require_trading()
        from x10.perpetual.orders import OrderSide, TimeInForce

        # Use best ask/bid as reference — more accurate than mid price
        book = self.fetch_order_book(symbol, limit=1)
        if side == "buy":
            if not book["asks"]:
                raise RuntimeError(f"No asks in {symbol} orderbook")
            ref_price = Decimal(str(book["asks"][0][0]))
        else:
            if not book["bids"]:
                raise RuntimeError(f"No bids in {symbol} orderbook")
            ref_price = Decimal(str(book["bids"][0][0]))

        slip = Decimal(str(slippage_pct / 100)) if slippage_pct is not None else _MARKET_ORDER_SLIPPAGE

        tick = self._get_tick_size(symbol)
        if side == "buy":
            raw = ref_price * (1 + slip)
            limit_price = (raw / tick).to_integral_value(rounding="ROUND_UP") * tick
        else:
            raw = ref_price * (1 - slip)
            limit_price = (raw / tick).to_integral_value(rounding="ROUND_DOWN") * tick

        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

        logger.info(
            "Extended MARKET %s %s qty=%s limit_price=%s (best=%s slip=%.4f%% tick=%s)",
            side.upper(), symbol, amount, limit_price, ref_price,
            float(slip) * 100, tick,
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

        data = getattr(result, "data", result)
        order_id = getattr(data, "id", None)
        external_id = getattr(data, "external_id", None)
        logger.info("Extended order placed: id=%s external_id=%s", order_id, external_id)
        return {"id": str(order_id) if order_id is not None else None, "external_id": str(external_id) if external_id is not None else None}

    def create_aggressive_limit_order(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        amount: Decimal,
        offset_ticks: int = 2,
        best_price: float | None = None,
    ) -> dict:
        """Place an aggressive limit IOC order: best price + offset ticks.

        BUY:  limit = best_ask + offset_ticks * tick_size
        SELL: limit = best_bid - offset_ticks * tick_size
        Uses IOC (Immediate or Cancel) for instant fill-or-kill behavior.

        If best_price is provided it is used directly, avoiding an extra order book fetch.
        """
        self._require_trading()
        from x10.perpetual.orders import OrderSide, TimeInForce

        tick = self._get_tick_size(symbol)

        # Always fetch a fresh orderbook — the best_price from pre-trade checks
        # can be several seconds stale (parallel execution), causing IOC cancellation.
        book = self.fetch_order_book(symbol, limit=1)
        if side == "buy":
            if not book["asks"]:
                raise RuntimeError(f"No asks in {symbol} orderbook")
            best = Decimal(str(book["asks"][0][0]))
        else:
            if not book["bids"]:
                raise RuntimeError(f"No bids in {symbol} orderbook")
            best = Decimal(str(book["bids"][0][0]))

        if side == "buy":
            # BUY aggressive: limit above best_ask → crosses into asks, fills immediately
            raw = best + tick * offset_ticks
            limit_price = (raw / tick).to_integral_value(rounding="ROUND_UP") * tick
        else:
            # SELL aggressive: limit BELOW best_bid → crosses into bids, fills immediately
            # Setting limit ABOVE bid would be passive and IOC would cancel unfilled
            raw = best - tick * offset_ticks
            limit_price = (raw / tick).to_integral_value(rounding="ROUND_DOWN") * tick

        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

        logger.info(
            "Extended aggressive limit: %s %s qty=%s best=%s limit=%s offset=%d tick=%s",
            side.upper(), symbol, amount, best, limit_price, offset_ticks, tick,
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

        # WrappedApiResponse[PlacedOrderModel] — unwrap .data to get the actual order
        data = getattr(result, "data", result)
        order_id = getattr(data, "id", None)
        external_id = getattr(data, "external_id", None)
        logger.info(
            "Extended aggressive limit placed: id=%s external_id=%s",
            order_id, external_id,
        )
        # Use numeric id for fill checks — /user/orders/external/{id} uses PlacedOrderModel.id
        return {
            "id": str(order_id) if order_id is not None else None,
            "external_id": str(external_id) if external_id is not None else None,
            "limit_price": float(limit_price),
            "best_price": float(best),
        }

    def check_order_fill(self, order_id: str) -> dict:
        """Check if an order has been filled via REST API.

        Endpoint: GET /user/orders/{id} — returns a single order object.
        Uses the numeric PlacedOrderModel.id.
        """
        try:
            url = f"{self._base_url}/user/orders/{order_id}"
            resp = self._session.get(url, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status", "").upper() != "OK":
                return {"filled": False, "status": "API_ERROR", "error": str(data)}
            order = data.get("data", {})
            status = str(order.get("status", "")).upper()
            filled = status in ("FILLED", "CLOSED")
            logger.info("check_order_fill extended(%s): status=%s filled=%s", str(order_id)[:20], status, filled)
            return {"filled": filled, "status": status, "order": order}
        except Exception as exc:
            logger.warning("check_order_fill(%s) error: %s", str(order_id)[:20], exc)
            return {"filled": False, "status": "ERROR", "error": str(exc)}

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

        tick = self._get_tick_size(symbol)
        price = (Decimal(str(price)) / tick).to_integral_value(rounding="ROUND_DOWN") * tick

        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

        logger.info(
            "Extended LIMIT %s %s qty=%s @ %s (post_only=%s reduce_only=%s tick=%s)",
            side.upper(), symbol, amount, price, post_only, reduce_only, tick,
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

        data = getattr(result, "data", result)
        order_id = getattr(data, "id", None)
        external_id = getattr(data, "external_id", None)
        logger.info("Extended limit order placed: id=%s external_id=%s", order_id, external_id)
        return {"id": str(order_id) if order_id is not None else None, "external_id": str(external_id) if external_id is not None else None}

    def fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        """Fetch open positions via REST, normalised to match GRVT format."""
        url = f"{self._base_url}/user/positions"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status", "").upper() != "OK":
            raise RuntimeError(f"Extended positions error: {data}")

        result = []
        for p in data.get("data", []):
            market = p.get("market", "")
            if symbols and market not in symbols:
                continue
            side = str(p.get("side", "")).upper()
            size = float(p.get("size", 0))
            result.append({
                "instrument": market,
                "size": size if side == "LONG" else -size,
                "entry_price": float(p.get("openPrice", 0)),
                "mark_price": float(p.get("markPrice", 0)),
                "unrealized_pnl": float(p.get("unrealisedPnl", 0)),
                "leverage": float(p.get("leverage", 0)),
                "side": side,
            })
        return result

    def get_account_summary(self) -> dict:
        """Fetch account balance/equity summary, normalised to match GRVT format."""
        url = f"{self._base_url}/user/balance"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status", "").upper() != "OK":
            raise RuntimeError(f"Extended balance error: {data}")
        d = data.get("data", {})
        positions = self.fetch_positions()
        return {
            "total_equity": d.get("equity", "0"),
            "available_balance": d.get("availableForTrade", "0"),
            "unrealized_pnl": d.get("unrealisedPnl", "0"),
            "positions": positions,
        }

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
