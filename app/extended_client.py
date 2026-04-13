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
        self._qty_step_cache: dict[str, Decimal] = {}      # symbol -> minOrderSizeChange
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
            qty_step = tc.get("minOrderSizeChange")
            if qty_step:
                self._qty_step_cache[name] = Decimal(str(qty_step))

    def _get_tick_size(self, symbol: str) -> Decimal:
        if symbol not in self._tick_size_cache:
            self._load_market_config()
        return self._tick_size_cache.get(symbol, Decimal("0.01"))

    def get_tick_size(self, symbol: str) -> Decimal:
        """Public accessor for tick size (used by VWAP limit computation)."""
        return self._get_tick_size(symbol)

    def get_min_order_size(self, symbol: str) -> Decimal:
        """Return the minimum order size for a symbol (0 if unknown)."""
        if symbol not in self._min_size_cache:
            self._load_market_config()
        return self._min_size_cache.get(symbol, Decimal("0"))

    def get_qty_step(self, symbol: str) -> Decimal:
        """Return the quantity step size (minOrderSizeChange) for a symbol."""
        if symbol not in self._qty_step_cache:
            self._load_market_config()
        return self._qty_step_cache.get(symbol, Decimal("0.01"))

    def _round_qty(self, amount: Decimal, symbol: str) -> Decimal:
        """Round quantity down to the instrument's qty step size."""
        step = self.get_qty_step(symbol)
        return (amount / step).to_integral_value(rounding="ROUND_DOWN") * step

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

        amount = self._round_qty(amount, symbol)

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
        limit_price: float | None = None,
    ) -> dict:
        """Place an aggressive limit IOC order.

        If limit_price is provided (e.g. from VWAP on local orderbook), it is
        used directly — no orderbook fetch needed (saves 200-500ms).

        Otherwise falls back to: best price + offset_ticks * tick_size.
        Uses IOC (Immediate or Cancel) for instant fill-or-kill behavior.
        """
        self._require_trading()
        from x10.perpetual.orders import OrderSide, TimeInForce

        amount = self._round_qty(amount, symbol)
        tick = self._get_tick_size(symbol)

        if limit_price is not None:
            # VWAP-computed limit — skip orderbook fetch entirely
            final_limit = Decimal(str(limit_price))
            # Round to tick
            if side == "buy":
                final_limit = (final_limit / tick).to_integral_value(rounding="ROUND_UP") * tick
            else:
                final_limit = (final_limit / tick).to_integral_value(rounding="ROUND_DOWN") * tick
            best = Decimal(str(best_price)) if best_price is not None else final_limit
            logger.info(
                "Extended VWAP limit: %s %s qty=%s limit=%s (VWAP-computed, no OB fetch)",
                side.upper(), symbol, amount, final_limit,
            )
        else:
            # Fallback: fetch orderbook and use offset_ticks
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
                raw = best + tick * offset_ticks
                final_limit = (raw / tick).to_integral_value(rounding="ROUND_UP") * tick
            else:
                raw = best - tick * offset_ticks
                final_limit = (raw / tick).to_integral_value(rounding="ROUND_DOWN") * tick

            logger.info(
                "Extended aggressive limit: %s %s qty=%s best=%s limit=%s offset=%d tick=%s",
                side.upper(), symbol, amount, best, final_limit, offset_ticks, tick,
            )

        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

        result = _run_async(
            self._trading_client.place_order(
                market_name=symbol,
                amount_of_synthetic=amount,
                price=final_limit,
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
            "limit_price": float(final_limit),
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
                "realized_pnl": float(p.get("realisedPnl", 0)),
                "leverage": float(p.get("leverage", 0)),
                "side": side,
                "created_time": int(p.get("createdAt", 0)),
            })
        return result

    def fetch_funding_payments(self, symbol: str, start_time: int = 0) -> float:
        """Fetch cumulative funding payments for a market from Extended.

        Endpoint: GET /api/v1/user/funding/history?market={symbol}&startTime={startTime}
        Args:
            symbol: Market symbol (e.g. 'HYPE-USD')
            start_time: Unix ms timestamp — only sum funding payments after this time.
                        Use position's createdTime to get funding for current position only.
        Returns: sum of all fundingFee values (positive = received, negative = paid).
        """
        url = f"{self._base_url}/user/funding/history"
        total = 0.0
        cursor = None
        while True:
            params: dict = {"market": symbol}
            if start_time:
                params["startTime"] = start_time
            if cursor:
                params["cursor"] = cursor
            resp = self._session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status", "").upper() != "OK":
                break
            for entry in data.get("data", []):
                total += float(entry.get("fundingFee", 0))
            pagination = data.get("pagination", {})
            next_cursor = pagination.get("cursor")
            count = pagination.get("count", 0)
            if not next_cursor or count == 0:
                break
            cursor = next_cursor
        return total

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

    # ------------------------------------------------------------------
    # Leverage
    # ------------------------------------------------------------------

    def get_leverage(self, market: str) -> dict:
        """Get current leverage for a market."""
        url = f"{self._base_url}/user/leverage?market={market}"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status", "").upper() != "OK":
            raise RuntimeError(f"Extended get_leverage error: {data}")
        entries = data.get("data", [])
        return entries[0] if entries else {}

    def set_leverage(self, market: str, leverage: int) -> bool:
        """Set leverage for a market via PATCH /api/v1/user/leverage."""
        url = f"{self._base_url}/user/leverage"
        payload = {"market": market, "leverage": str(leverage)}
        resp = self._session.patch(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        ok = data.get("status", "").upper() == "OK"
        if ok:
            logger.info("Extended leverage set: %s -> %dx", market, leverage)
        else:
            logger.warning("Extended set_leverage failed: %s -> %dx, response: %s", market, leverage, data)
        return ok

    async def async_set_leverage(self, market: str, leverage: int) -> bool:
        """Async version of set_leverage."""
        client = await self._get_async_session()
        try:
            resp = await client.patch("/user/leverage", json={"market": market, "leverage": str(leverage)})
            resp.raise_for_status()
            data = resp.json()
            ok = data.get("status", "").upper() == "OK"
            if ok:
                logger.info("Extended leverage set (async): %s -> %dx", market, leverage)
            else:
                logger.warning("Extended async_set_leverage failed: %s -> %dx, response: %s", market, leverage, data)
            return ok
        except Exception as exc:
            logger.warning("Extended async_set_leverage error: %s", exc)
            return False

    # ══════════════════════════════════════════════════════════════════
    # Async methods for the new Funding-Arb Maker-Taker engine
    # (AsyncExchangeClient protocol — Phase 2)
    # ══════════════════════════════════════════════════════════════════

    async def _get_async_session(self):
        """Lazily create and return an httpx.AsyncClient."""
        if not hasattr(self, "_async_session") or self._async_session is None:
            import httpx
            self._async_session = httpx.AsyncClient(
                base_url=self._base_url,
                verify=False,
                headers={"User-Agent": _USER_AGENT, **({"X-Api-Key": self._api_key} if self._api_key else {})},
                timeout=15.0,
            )
        return self._async_session

    async def async_fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        """Async version of fetch_order_book."""
        client = await self._get_async_session()
        resp = await client.get(f"/info/markets/{symbol}/orderbook")
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "OK":
            error = data.get("error", {})
            raise RuntimeError(f"Extended OB error: {error.get('code', '?')} — {error.get('message', str(data))}")
        book = data.get("data", {})
        bids = [[lv["price"], lv["qty"]] for lv in book.get("bid", [])]
        asks = [[lv["price"], lv["qty"]] for lv in book.get("ask", [])]
        return {"bids": bids[:limit], "asks": asks[:limit]}

    async def async_fetch_markets(self) -> list[dict]:
        """Async version of fetch_markets."""
        client = await self._get_async_session()
        resp = await client.get("/info/markets")
        resp.raise_for_status()
        data = resp.json()
        if data.get("status", "").upper() != "OK":
            raise RuntimeError(f"Extended markets error: {data}")
        markets = []
        for m in data.get("data", []):
            if m.get("status") != "ACTIVE":
                continue
            name = m["name"]
            tc = m.get("tradingConfig", {})
            # Update caches while we have the data
            tick = tc.get("minPriceChange")
            if tick:
                self._tick_size_cache[name] = Decimal(str(tick))
            min_size = tc.get("minOrderSize")
            if min_size:
                self._min_size_cache[name] = Decimal(str(min_size))
            qty_step = tc.get("minOrderSizeChange")
            if qty_step:
                self._qty_step_cache[name] = Decimal(str(qty_step))
            markets.append({
                "symbol": name, "name": name,
                "asset": m.get("assetName"), "status": m.get("status"),
                "min_order_size": min_size, "max_leverage": tc.get("maxLeverage"),
            })
        return markets

    async def async_get_min_order_size(self, symbol: str) -> Decimal:
        if symbol not in self._min_size_cache:
            await self.async_fetch_markets()
        return self._min_size_cache.get(symbol, Decimal("0"))

    async def async_get_tick_size(self, symbol: str) -> Decimal:
        if symbol not in self._tick_size_cache:
            await self.async_fetch_markets()
        return self._tick_size_cache.get(symbol, Decimal("0.01"))

    async def async_create_post_only_order(
        self, symbol: str, side: str, amount: Decimal, price: Decimal,
        reduce_only: bool = False,
    ) -> dict:
        """Place a GTT post-only limit order on Extended (maker only).

        Uses the x10 SDK's place_order with post_only=True.
        Runs the SDK coroutine directly (no _run_async — we're already async).
        """
        self._require_trading()
        from x10.perpetual.orders import OrderSide

        amount = self._round_qty(amount, symbol)
        tick = self._get_tick_size(symbol)
        price = (Decimal(str(price)) / tick).to_integral_value(rounding="ROUND_DOWN") * tick
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

        logger.info(
            "Extended POST-ONLY %s %s qty=%s @ %s (tick=%s reduce_only=%s)",
            side.upper(), symbol, amount, price, tick, reduce_only,
        )

        result = await self._trading_client.place_order(
            market_name=symbol,
            amount_of_synthetic=amount,
            price=price,
            side=order_side,
            post_only=True,
            reduce_only=reduce_only,
        )

        data = getattr(result, "data", result)
        order_id = getattr(data, "id", None)
        external_id = getattr(data, "external_id", None)
        logger.info("Extended post-only placed: id=%s external_id=%s", order_id, external_id)
        return {
            "id": str(order_id) if order_id is not None else None,
            "external_id": str(external_id) if external_id is not None else None,
            "limit_price": float(price),
        }

    async def async_create_ioc_order(
        self, symbol: str, side: str, amount: Decimal, price: Decimal,
        reduce_only: bool = False,
    ) -> dict:
        """Place an IOC limit order on Extended (taker)."""
        self._require_trading()
        from x10.perpetual.orders import OrderSide, TimeInForce

        amount = self._round_qty(amount, symbol)
        tick = self._get_tick_size(symbol)
        if side == "buy":
            price = (Decimal(str(price)) / tick).to_integral_value(rounding="ROUND_UP") * tick
        else:
            price = (Decimal(str(price)) / tick).to_integral_value(rounding="ROUND_DOWN") * tick
        order_side = OrderSide.BUY if side == "buy" else OrderSide.SELL

        logger.info("Extended IOC %s %s qty=%s @ %s (reduce_only=%s)", side.upper(), symbol, amount, price, reduce_only)

        result = await self._trading_client.place_order(
            market_name=symbol,
            amount_of_synthetic=amount,
            price=price,
            side=order_side,
            time_in_force=TimeInForce.IOC,
            reduce_only=reduce_only,
        )

        data = getattr(result, "data", result)
        order_id = getattr(data, "id", None)
        external_id = getattr(data, "external_id", None)
        logger.info("Extended IOC placed: id=%s external_id=%s", order_id, external_id)

        # Fill detection is handled by WS account stream in _check_taker_fill.
        # No REST poll or sleep here — saves ~700ms per taker order.
        return {
            "id": str(order_id) if order_id is not None else None,
            "external_id": str(external_id) if external_id is not None else None,
            "limit_price": float(price),
            "traded_qty": 0.0,
            "status": "PENDING",
        }

    async def async_cancel_all_orders(self) -> bool:
        """Cancel all open orders on Extended via mass_cancel(cancel_all=True)."""
        self._require_trading()
        try:
            await self._trading_client.mass_cancel(cancel_all=True)
            logger.info("Extended: all orders cancelled (mass_cancel)")
            return True
        except Exception as exc:
            logger.warning("Extended cancel_all_orders error: %s", exc)
            return False

    async def async_cancel_order(self, order_id: str) -> bool:
        """Cancel an open order on Extended via REST.

        Endpoint: DELETE /user/order/{order_id} (singular, not /orders/).
        """
        client = await self._get_async_session()
        try:
            resp = await client.delete(f"/user/order/{order_id}")
            resp.raise_for_status()
            data = resp.json()
            ok = data.get("status", "").upper() == "OK"
            logger.info("Extended cancel_order(%s): %s", order_id, "OK" if ok else data)
            return ok
        except Exception as exc:
            logger.warning("Extended cancel_order(%s) error: %s", order_id, exc)
            return False

    async def async_check_order_fill(self, order_id: str) -> dict:
        """Async version of check_order_fill."""
        client = await self._get_async_session()
        try:
            resp = await client.get(f"/user/orders/{order_id}")
            resp.raise_for_status()
            data = resp.json()
            if data.get("status", "").upper() != "OK":
                return {"filled": False, "status": "API_ERROR", "error": str(data)}
            order = data.get("data", {})
            status = str(order.get("status", "")).upper()
            filled = status in ("FILLED", "CLOSED")
            filled_qty = float(order.get("filledQty", 0))
            avg_price = float(order.get("averagePrice", 0))
            return {"filled": filled, "status": status, "traded_qty": filled_qty, "avg_price": avg_price, "order": order}
        except Exception as exc:
            logger.warning("async_check_order_fill(%s) error: %s", order_id, exc)
            return {"filled": False, "status": "ERROR", "error": str(exc), "traded_qty": 0.0}

    async def async_fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        """Async version of fetch_positions."""
        client = await self._get_async_session()
        resp = await client.get("/user/positions")
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
                "realized_pnl": float(p.get("realisedPnl", 0)),
                "leverage": float(p.get("leverage", 0)),
                "side": side,
                "created_time": int(p.get("createdAt", 0)),
            })
        return result

    async def async_fetch_funding_payments(self, symbol: str, start_time: int = 0) -> float:
        """Async version of fetch_funding_payments."""
        client = await self._get_async_session()
        total = 0.0
        cursor = None
        while True:
            params: dict = {"market": symbol}
            if start_time:
                params["startTime"] = start_time
            if cursor:
                params["cursor"] = cursor
            resp = await client.get("/user/funding/history", params=params)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status", "").upper() != "OK":
                break
            for entry in data.get("data", []):
                total += float(entry.get("fundingFee", 0))
            pagination = data.get("pagination", {})
            next_cursor = pagination.get("cursor")
            count = pagination.get("count", 0)
            if not next_cursor or count == 0:
                break
            cursor = next_cursor
        return total

    async def async_fetch_funding_rate(self, symbol: str) -> dict:
        """Fetch the latest funding rate for a market on Extended.

        Endpoint: GET /api/v1/info/{market}/funding?startTime=&endTime=
        Results sorted descending by timestamp; first entry is the latest applied rate.
        """
        import time as _time
        client = await self._get_async_session()
        now_ms = int(_time.time() * 1000)
        two_hours_ago = now_ms - 2 * 3600 * 1000
        resp = await client.get(
            f"/info/{symbol}/funding",
            params={"startTime": two_hours_ago, "endTime": now_ms},
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("status", "").upper() != "OK":
            raise RuntimeError(f"Extended funding rate error: {data}")
        records = data.get("data", [])
        if not records:
            return {"symbol": symbol, "funding_rate": 0.0, "next_funding_time": None}
        r = records[0]
        return {
            "symbol": symbol,
            "funding_rate": float(r.get("f", 0)),
            "timestamp": r.get("T"),
            "next_funding_time": None,
        }

    async def async_subscribe_fills(self, symbol: str, callback) -> None:
        """Subscribe to Extended private WS account stream for fill events.

        Endpoint: wss://api.starknet.extended.exchange/stream.extended.exchange/v1/account
        Events of type "TRADE" contain fill information.
        """
        import ssl as _ssl
        import websockets
        import json

        ws_url = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/account"
        ssl_ctx = _ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = _ssl.CERT_NONE

        logger.info("Extended fill WS connecting: %s", ws_url)
        async for ws in websockets.connect(ws_url, ssl=ssl_ctx, extra_headers={"X-Api-Key": self._api_key}):
            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "TRADE":
                        for trade in msg.get("data", {}).get("trades", []):
                            if symbol and trade.get("market") != symbol:
                                continue
                            fill = {
                                "order_id": str(trade.get("orderId", "")),
                                "filled_qty": float(trade.get("qty", 0)),
                                "remaining_qty": 0.0,
                                "price": float(trade.get("price", 0)),
                                "is_taker": trade.get("isTaker", True),
                                "fee": float(trade.get("fee", 0)),
                                "symbol": trade.get("market", symbol),
                            }
                            await callback(fill)
            except websockets.ConnectionClosed:
                logger.warning("Extended fill WS disconnected, reconnecting…")
                continue

    async def async_subscribe_funding_rate(self, symbol: str, callback) -> None:
        """Subscribe to Extended public WS funding rate stream.

        Endpoint: wss://api.starknet.extended.exchange/stream.extended.exchange/v1/funding/{market}
        """
        import ssl
        import websockets
        import json

        ws_url = f"wss://api.starknet.extended.exchange/stream.extended.exchange/v1/funding/{symbol}"
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        logger.info("Extended funding WS connecting: %s", ws_url)
        async for ws in websockets.connect(ws_url, ssl=ssl_ctx):
            try:
                logger.info("Extended funding WS connected")
                async for raw in ws:
                    msg = json.loads(raw)
                    fr_data = msg.get("data", {})
                    rate = float(fr_data.get("f", 0))
                    await callback({
                        "symbol": fr_data.get("m", symbol),
                        "funding_rate": rate,
                        "timestamp": str(fr_data.get("T", "")),
                    })
            except websockets.ConnectionClosed:
                logger.warning("Extended funding WS disconnected, reconnecting…")
                continue

    # ══════════════════════════════════════════════════════════════════
    # Journal — history fetching for Trading Journal / PnL tracking
    # ══════════════════════════════════════════════════════════════════

    async def async_fetch_order_history(
        self, market: str | None = None, since_ms: int | None = None, limit: int = 500,
    ) -> list[dict]:
        """Fetch completed orders from Extended.

        Endpoint: GET /user/orders/history?market=&limit=&cursor=
        Returns normalised dicts for journal ingestion.
        """
        self._require_trading()
        client = await self._get_async_session()
        all_orders: list[dict] = []
        cursor: int | None = None

        while True:
            params: dict = {"limit": min(limit, 500)}
            if market:
                params["market"] = market
            if cursor:
                params["cursor"] = cursor

            resp = await client.get("/user/orders/history", params=params)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "OK":
                logger.warning("Extended order history error: %s", data)
                break

            for o in data.get("data", []):
                created = int(o.get("createdTime", 0))
                if since_ms and created < since_ms:
                    continue
                all_orders.append({
                    "exchange_order_id": str(o.get("id", "")),
                    "exchange": "extended",
                    "instrument": o.get("market", ""),
                    "token": self._extract_token(o.get("market", "")),
                    "side": (o.get("side", "")).upper(),
                    "order_type": (o.get("type", "LIMIT")).upper(),
                    "status": (o.get("status", "")).upper(),
                    "price": float(o.get("price", 0)),
                    "average_price": float(o.get("averagePrice", 0)),
                    "qty": float(o.get("qty", 0)),
                    "filled_qty": float(o.get("filledQty", 0)),
                    "fee": float(o.get("payedFee", 0)),
                    "reduce_only": 1 if o.get("reduceOnly") else 0,
                    "post_only": 1 if o.get("postOnly") else 0,
                    "created_at": created,
                    "updated_at": int(o.get("updatedTime", created)),
                })

            pagination = data.get("pagination", {})
            next_cursor = pagination.get("cursor")
            count = pagination.get("count", 0)
            if not next_cursor or count == 0 or len(data.get("data", [])) == 0:
                break
            cursor = next_cursor

        logger.info("Extended order history: fetched %d orders", len(all_orders))
        return all_orders

    async def async_fetch_trade_history(
        self, market: str | None = None, since_ms: int | None = None, limit: int = 500,
    ) -> list[dict]:
        """Fetch individual fills/trades from Extended.

        Endpoint: GET /user/trades?market=&limit=&cursor=
        Returns normalised dicts for journal ingestion.
        """
        self._require_trading()
        client = await self._get_async_session()
        all_fills: list[dict] = []
        cursor: int | None = None

        while True:
            params: dict = {"limit": min(limit, 500)}
            if market:
                params["market"] = market
            if cursor:
                params["cursor"] = cursor

            resp = await client.get("/user/trades", params=params)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "OK":
                logger.warning("Extended trade history error: %s", data)
                break

            for t in data.get("data", []):
                created = int(t.get("createdTime", 0))
                if since_ms and created < since_ms:
                    continue
                qty = float(t.get("qty", 0))
                price = float(t.get("price", 0))
                all_fills.append({
                    "exchange_fill_id": str(t.get("id", "")),
                    "exchange_order_id": str(t.get("orderId", "")),
                    "exchange": "extended",
                    "instrument": t.get("market", ""),
                    "token": self._extract_token(t.get("market", "")),
                    "side": (t.get("side", "")).upper(),
                    "price": price,
                    "qty": qty,
                    "value": float(t.get("value", qty * price)),
                    "fee": float(t.get("fee", 0)),
                    "is_taker": 1 if t.get("isTaker", True) else 0,
                    "trade_type": (t.get("tradeType", "TRADE")).upper(),
                    "created_at": created,
                })

            pagination = data.get("pagination", {})
            next_cursor = pagination.get("cursor")
            count = pagination.get("count", 0)
            if not next_cursor or count == 0 or len(data.get("data", [])) == 0:
                break
            cursor = next_cursor

        logger.info("Extended trade history: fetched %d fills", len(all_fills))
        return all_fills

    async def async_fetch_funding_payments(
        self, market: str | None = None, since_ms: int | None = None, limit: int = 500,
    ) -> list[dict]:
        """Fetch funding payment history from Extended.

        Endpoint: GET /user/funding/history?market=&startTime=&limit=&cursor=
        """
        self._require_trading()
        client = await self._get_async_session()
        all_payments: list[dict] = []
        cursor: int | None = None

        while True:
            params: dict = {"limit": min(limit, 500)}
            if market:
                params["market"] = market
            if since_ms:
                params["startTime"] = since_ms
            if cursor:
                params["cursor"] = cursor

            resp = await client.get("/user/funding/history", params=params)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "OK":
                logger.warning("Extended funding payments error: %s", data)
                break

            for f in data.get("data", []):
                paid_at = int(f.get("paidTime", 0))
                all_payments.append({
                    "exchange_payment_id": str(f.get("id", "")),
                    "exchange": "extended",
                    "instrument": f.get("market", ""),
                    "token": self._extract_token(f.get("market", "")),
                    "side": (f.get("side", "")).upper(),
                    "size": float(f.get("size", 0)),
                    "funding_fee": float(f.get("fundingFee", 0)),
                    "funding_rate": float(f.get("fundingRate", 0)),
                    "mark_price": float(f.get("markPrice", 0)),
                    "paid_at": paid_at,
                })

            pagination = data.get("pagination", {})
            next_cursor = pagination.get("cursor")
            count = pagination.get("count", 0)
            if not next_cursor or count == 0 or len(data.get("data", [])) == 0:
                break
            cursor = next_cursor

        logger.info("Extended funding payments: fetched %d payments", len(all_payments))
        return all_payments

    async def async_fetch_points(self) -> list[dict]:
        """Fetch earned points from Extended.

        Endpoint: GET /user/rewards/earned
        """
        self._require_trading()
        client = await self._get_async_session()
        resp = await client.get("/user/rewards/earned")
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "OK":
            logger.warning("Extended points error: %s", data)
            return []

        result: list[dict] = []
        for season in data.get("data", []):
            season_id = season.get("seasonId", 0)
            for epoch in season.get("epochRewards", []):
                result.append({
                    "exchange": "extended",
                    "season_id": season_id,
                    "epoch_id": epoch.get("epochId", 0),
                    "start_date": epoch.get("startDate", ""),
                    "end_date": epoch.get("endDate", ""),
                    "points": float(epoch.get("pointsReward", 0)),
                })

        logger.info("Extended points: fetched %d epoch records", len(result))
        return result

    @staticmethod
    def _extract_token(instrument: str) -> str:
        """Extract token from instrument (e.g. 'BTC-USD' -> 'BTC')."""
        parts = instrument.replace("_", "-").split("-")
        return parts[0] if parts else instrument
