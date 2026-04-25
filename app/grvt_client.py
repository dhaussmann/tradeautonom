import asyncio
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

        # Shared fill WS state — one connection per client, multiple symbol callbacks
        self._fill_callbacks: list[tuple[str, object]] = []
        self._fill_ws_task: object = None
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

    def _round_qty(self, amount: Decimal, symbol: str) -> Decimal:
        """Round quantity down to the instrument's min_size step."""
        step = self.get_min_order_size(symbol)
        if step and step > 0:
            return (amount / step).to_integral_value(rounding="ROUND_DOWN") * step
        return amount

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
        amount = self._round_qty(amount, symbol)
        cid = client_order_id or rand_uint32()
        resp = self._api.create_order(
            symbol=symbol,
            order_type="market",
            side=side,
            amount=amount,
            params={"client_order_id": cid, "time_in_force": "FILL_OR_KILL"},
        )
        if not resp:
            raise RuntimeError(f"GRVT market order rejected (empty response). symbol={symbol} side={side} amount={amount} cid={cid}")
        state = resp.get("state", {})
        status = state.get("status", "UNKNOWN")
        reject = state.get("reject_reason", "")
        traded = state.get("traded_size", ["0"])
        traded_qty = float(traded[0]) if traded else 0.0
        logger.info("GRVT market order: symbol=%s side=%s amount=%s cid=%s status=%s traded=%s reject=%s",
                     symbol, side, amount, cid, status, traded_qty, reject)
        return resp

    def create_aggressive_limit_order(
        self,
        symbol: str,
        side: str,
        amount: Decimal,
        offset_ticks: int = 2,
        best_price: float | None = None,
        client_order_id: int | None = None,
        limit_price: float | None = None,
    ) -> dict:
        """Place an aggressive limit order.

        If limit_price is provided (e.g. from VWAP on local orderbook), it is
        used directly — no orderbook fetch or offset calculation needed.

        Otherwise falls back to: best price + offset_ticks * tick_size.
        """
        amount = self._round_qty(amount, symbol)
        cid = client_order_id or rand_uint32()

        if limit_price is not None:
            # VWAP-computed limit — use directly
            final_price = limit_price
            logger.info(
                "GRVT VWAP limit: %s %s qty=%s limit=%.4f (VWAP-computed, no OB fetch)",
                side.upper(), symbol, amount, final_price,
            )
        else:
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
                final_price = float(best + tick * offset_ticks)
            else:
                final_price = float(best - tick * offset_ticks)

            logger.info(
                "Aggressive limit: %s %s qty=%s best=%s limit=%s offset=%d ticks tick=%s",
                side.upper(), symbol, amount, best, final_price, offset_ticks, tick,
            )

        return self.create_limit_order(
            symbol=symbol, side=side, amount=amount,
            price=final_price, client_order_id=cid,
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
            # GRVT exposes the realized VWAP as `avg_fill_price` (list per leg).
            avg_prices = state.get("avg_fill_price", [0]) if state else [0]
            try:
                avg_price = float(avg_prices[0]) if isinstance(avg_prices, list) and avg_prices else float(avg_prices or 0)
            except (TypeError, ValueError):
                avg_price = 0.0
            filled = status in ("FILLED", "CLOSED") or (status == "PENDING" and traded_qty > 0.0)
            logger.info(
                "check_order_fill(%s): status=%s traded=%s avg_price=%.6f filled=%s",
                client_order_id, status, traded_qty, avg_price, filled,
            )
            return {
                "filled": filled,
                "status": status,
                "traded_qty": traded_qty,
                "avg_price": avg_price,
                "order": order,
            }
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
        # order_id here is actually our client_order_id (cid) —
        # use client_order_id param so GRVT matches correctly.
        return self._api.cancel_order(
            params={"client_order_id": str(order_id), "time_to_live_ms": "1000"},
        )

    def cancel_all_orders(self) -> bool:
        return self._api.cancel_all_orders()

    async def async_cancel_all_orders(self) -> bool:
        import asyncio
        return await asyncio.to_thread(self.cancel_all_orders)

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
        """Set initial leverage for a specific instrument.

        Uses the (deprecated) set_initial_leverage endpoint, then verifies
        via get_all_initial_leverage that the value was actually applied.
        Retries once if verification fails.
        """
        path = self._trade_base_url() + "/full/v1/set_initial_leverage"
        payload = {
            "sub_account_id": self.settings.grvt_trading_account_id,
            "instrument": instrument,
            "leverage": str(leverage),
        }

        for attempt in range(2):
            resp = self._api._auth_and_post(path, payload)
            logger.info("GRVT set_leverage(%s, %dx) attempt %d response: %s", instrument, leverage, attempt + 1, resp)
            success = resp.get("success", False)
            if not success:
                logger.warning("GRVT set_leverage API returned non-success: %s -> %dx, response: %s", instrument, leverage, resp)
                continue

            # Verify leverage was actually applied
            actual = self._verify_leverage(instrument)
            if actual is not None and str(actual) == str(leverage):
                logger.info("GRVT leverage VERIFIED: %s -> %dx (actual=%s)", instrument, leverage, actual)
                return True
            elif actual is not None:
                logger.warning("GRVT leverage MISMATCH: %s requested=%dx actual=%s (attempt %d)", instrument, leverage, actual, attempt + 1)
            else:
                logger.warning("GRVT leverage verification inconclusive for %s (attempt %d) — instrument not found in get_all_leverage", instrument, attempt + 1)
                return bool(success)

        logger.error("GRVT set_leverage FAILED after retries: %s requested=%dx", instrument, leverage)
        return False

    def _verify_leverage(self, instrument: str) -> str | None:
        """Read back the current leverage for an instrument. Returns the leverage string or None."""
        try:
            all_lev = self.get_all_leverage()
            for entry in all_lev:
                if entry.get("instrument") == instrument:
                    return entry.get("leverage")
        except Exception as exc:
            logger.warning("GRVT _verify_leverage error: %s", exc)
        return None

    async def async_set_leverage(self, instrument: str, leverage: int) -> bool:
        """Async wrapper around set_leverage (SDK is synchronous)."""
        import asyncio
        return await asyncio.to_thread(self.set_leverage, instrument, leverage)

    # ══════════════════════════════════════════════════════════════════
    # Async methods for the new Funding-Arb Maker-Taker engine
    # (AsyncExchangeClient protocol — Phase 2)
    # ══════════════════════════════════════════════════════════════════

    def _market_data_base_url(self) -> str:
        """Derive the market-data API base URL from environment."""
        url = get_grvt_endpoint(self._env, "GET_ORDER_BOOK")
        # e.g. https://market-data.testnet.grvt.io/full/v1/book -> https://market-data.testnet.grvt.io
        return url.rsplit("/full/", 1)[0]

    async def _get_async_session(self):
        """Lazily create and return an httpx.AsyncClient for GRVT."""
        if not hasattr(self, "_async_session") or self._async_session is None:
            import httpx
            self._async_session = httpx.AsyncClient(
                verify=False,
                timeout=15.0,
            )
        return self._async_session

    def _get_auth_headers(self) -> dict[str, str]:
        """Return auth headers (cookie + account-id) for private REST calls."""
        sub_account_id = self.settings.grvt_trading_account_id
        try:
            self._api.refresh_cookie()
        except Exception as exc:
            logger.warning("GRVT cookie refresh failed: %s", exc)
        cookie = getattr(self._api, "_cookie", None)
        headers: dict[str, str] = {}
        if isinstance(cookie, dict):
            grav = cookie.get("gravity", "")
            if grav:
                headers["Cookie"] = f"gravity={grav}"
            acct = cookie.get("X-Grvt-Account-Id", sub_account_id)
            headers["X-Grvt-Account-Id"] = str(acct)
        elif cookie:
            headers["Cookie"] = str(cookie)
            headers["X-Grvt-Account-Id"] = sub_account_id
        else:
            headers["X-Grvt-Account-Id"] = sub_account_id
        return headers

    async def async_fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        """Async version of fetch_order_book using direct REST.

        GRVT API only accepts specific depth values. The SDK defaults to 10.
        """
        safe_limit = max(limit, 10)
        client = await self._get_async_session()
        url = get_grvt_endpoint(self._env, "GET_ORDER_BOOK")
        payload = {"instrument": symbol, "aggregate": 1, "depth": 10}
        resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            body = resp.text[:500]
            raise RuntimeError(f"GRVT OB {resp.status_code} for {symbol}: {body}")
        data = resp.json()
        raw = data.get("result", {})
        return {
            "symbol": raw.get("instrument", symbol),
            "bids": [[b["price"], b["size"]] for b in raw.get("bids", [])],
            "asks": [[a["price"], a["size"]] for a in raw.get("asks", [])],
        }

    async def async_fetch_markets(self) -> list[dict]:
        """Async version of fetch_markets."""
        # Delegate to sync version (GRVT SDK is sync-only)
        import asyncio
        return await asyncio.to_thread(self.fetch_markets)

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
        """Place a GTT post-only limit order on GRVT (maker only).

        Uses GRVT SDK's create_order with time_in_force=GOOD_TILL_TIME
        and post_only=True. Runs in a thread since the SDK is sync.
        """
        import asyncio
        amount = self._round_qty(amount, symbol)
        cid = rand_uint32()

        logger.info(
            "GRVT POST-ONLY %s %s qty=%s @ %s cid=%s (reduce_only=%s)",
            side.upper(), symbol, amount, price, cid, reduce_only,
        )

        params = {
            "client_order_id": cid,
            "time_in_force": "GOOD_TILL_TIME",
            "post_only": True,
        }
        if reduce_only:
            params["reduce_only"] = True

        def _place():
            return self._api.create_order(
                symbol=symbol,
                order_type="limit",
                side=side,
                amount=amount,
                price=float(price),
                params=params,
            )

        resp = await asyncio.to_thread(_place)
        if not resp:
            raise RuntimeError("GRVT post-only order rejected (empty response)")

        state = resp.get("state", {})
        status = state.get("status", "UNKNOWN")
        order_id = resp.get("order_id", resp.get("metadata", {}).get("client_order_id", str(cid)))

        logger.info("GRVT post-only placed: cid=%s status=%s", cid, status)

        if status == "REJECTED" and state.get("reject_reason") == "FAIL_POST_ONLY":
            raise RuntimeError(f"GRVT post-only rejected: would cross book (FAIL_POST_ONLY)")

        return {
            "id": str(cid),
            "order_id": str(order_id),
            "status": status,
            "limit_price": float(price),
        }

    async def async_create_ioc_order(
        self, symbol: str, side: str, amount: Decimal, price: Decimal,
        reduce_only: bool = False,
    ) -> dict:
        """Place an IOC limit order on GRVT (taker)."""
        import asyncio
        amount = self._round_qty(amount, symbol)
        cid = rand_uint32()

        logger.info("GRVT IOC %s %s qty=%s @ %s cid=%s (reduce_only=%s)", side.upper(), symbol, amount, price, cid, reduce_only)

        params = {"client_order_id": cid, "time_in_force": "IMMEDIATE_OR_CANCEL"}
        if reduce_only:
            params["reduce_only"] = True

        def _place():
            return self._api.create_order(
                symbol=symbol,
                order_type="limit",
                side=side,
                amount=amount,
                price=float(price),
                params=params,
            )

        resp = await asyncio.to_thread(_place)
        if not resp:
            raise RuntimeError("GRVT IOC order rejected (empty response)")

        state = resp.get("state", {})
        status = state.get("status", "UNKNOWN")
        traded = state.get("traded_size", ["0"])
        traded_qty = float(traded[0]) if traded else 0.0

        logger.info("GRVT IOC placed: cid=%s status=%s traded=%s", cid, status, traded_qty)
        return {
            "id": str(cid),
            "status": status,
            "traded_qty": traded_qty,
            "limit_price": float(price),
        }

    async def async_cancel_order(self, order_id: str) -> bool:
        """Cancel an open order on GRVT."""
        import asyncio
        try:
            result = await asyncio.to_thread(self.cancel_order, order_id)
            logger.info("GRVT cancel_order(%s): %s", order_id, result)
            return bool(result)
        except Exception as exc:
            logger.warning("GRVT cancel_order(%s) error: %s", order_id, exc)
            return False

    async def async_check_order_fill(self, order_id: str) -> dict:
        """Async version of check_order_fill."""
        import asyncio
        return await asyncio.to_thread(self.check_order_fill, int(order_id))

    async def async_fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        """Async version of fetch_positions."""
        import asyncio
        return await asyncio.to_thread(self.fetch_positions, symbols or [])

    async def async_fetch_funding_rate(self, symbol: str) -> dict:
        """Fetch the latest funding rate for a GRVT perpetual.

        Endpoint: POST full/v1/funding
        """
        client = await self._get_async_session()
        url = self._market_data_base_url() + "/full/v1/funding"
        payload = {
            "instrument": symbol,
            "limit": 1,
            "agg_type": "FUNDING_INTERVAL",
        }
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("result", [])
        if not results:
            return {"symbol": symbol, "funding_rate": 0.0, "next_funding_time": None}
        r = results[0]
        return {
            "symbol": symbol,
            "funding_rate": float(r.get("funding_rate", 0)),
            "funding_rate_8h_avg": float(r.get("funding_rate_8_h_avg", 0)),
            "mark_price": r.get("mark_price"),
            "funding_time": r.get("funding_time"),
            "funding_interval_hours": r.get("funding_interval_hours"),
            "next_funding_time": None,
        }

    async def async_subscribe_fills(self, symbol: str, callback) -> None:
        """Register a per-symbol fill callback on the shared GRVT account WS.

        GRVT enforces per-account connection limits. All bots share one WS
        connection using an account-level selector; fills are fanned out by symbol.
        """
        self._fill_callbacks.append((symbol, callback))
        if self._fill_ws_task is None or self._fill_ws_task.done():
            self._fill_ws_task = asyncio.create_task(
                self._run_shared_fill_ws(), name="grvt-fill-ws-shared"
            )
        try:
            await asyncio.get_event_loop().create_future()
        except asyncio.CancelledError:
            self._fill_callbacks = [(s, cb) for s, cb in self._fill_callbacks if cb is not callback]
            raise

    async def _run_shared_fill_ws(self) -> None:
        """Single shared WS connection to the GRVT account fill stream."""
        import ssl as _ssl
        import websockets
        import json

        trade_base = self._trade_base_url()
        ws_url = trade_base.replace("https://", "wss://") + "/ws/full"
        sub_account_id = self.settings.grvt_trading_account_id
        # Account-level selector (no symbol) delivers fills for all instruments
        selector = str(sub_account_id)
        sub_msg = json.dumps({
            "jsonrpc": "2.0", "method": "subscribe",
            "params": {"stream": "v1.fill", "selectors": [selector]}, "id": 1,
        })
        ssl_ctx = _ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = _ssl.CERT_NONE
        reconnect_delay = 5.0

        logger.info("GRVT fill WS connecting (shared): %s selector=%s", ws_url, selector)
        while True:
            try:
                self._api.refresh_cookie()
            except Exception as exc:
                logger.warning("GRVT fill WS: cookie refresh failed: %s", exc)

            cookie = getattr(self._api, "_cookie", None)
            headers = {}
            if isinstance(cookie, dict):
                grav = cookie.get("gravity", "")
                if grav:
                    headers["Cookie"] = f"gravity={grav}"
                acct = cookie.get("X-Grvt-Account-Id", sub_account_id)
                headers["X-Grvt-Account-Id"] = str(acct)
            elif cookie:
                headers["Cookie"] = str(cookie)
                headers["X-Grvt-Account-Id"] = sub_account_id
            else:
                headers["X-Grvt-Account-Id"] = sub_account_id

            if "Cookie" not in headers:
                logger.warning("GRVT fill WS: no cookie — retry in %.0fs", reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60.0)
                continue

            try:
                async with websockets.connect(ws_url, ssl=ssl_ctx, extra_headers=headers) as ws:
                    reconnect_delay = 5.0
                    await ws.send(sub_msg)
                    logger.info("GRVT fill WS connected (serving %d callbacks, selector=%s)", len(self._fill_callbacks), selector)

                    async for raw in ws:
                        msg = json.loads(raw)
                        f = msg.get("feed")
                        if not isinstance(f, dict):
                            continue
                        oid = f.get("order_id", "")
                        client_oid = f.get("client_order_id", "")
                        fill_symbol = f.get("instrument", "")
                        fill = {
                            "order_id": client_oid or oid,
                            "order_id_hex": oid,
                            "filled_qty": float(f.get("size", 0)),
                            "remaining_qty": 0.0,
                            "price": float(f.get("price", 0)),
                            "is_taker": f.get("is_taker", False),
                            "symbol": fill_symbol,
                        }
                        logger.info("GRVT fill WS: order=%s client=%s qty=%.6f price=%.4f taker=%s symbol=%s",
                                    oid, client_oid, fill["filled_qty"], fill["price"], fill["is_taker"], fill_symbol)
                        for sym, cb in list(self._fill_callbacks):
                            if not sym or sym == fill_symbol:
                                try:
                                    await cb(fill)
                                except Exception:
                                    pass

            except websockets.ConnectionClosed:
                logger.warning("GRVT fill WS disconnected — reconnecting")
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("GRVT fill WS error: %s — retry in %.0fs", exc, reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60.0)

    # ══════════════════════════════════════════════════════════════════
    # Journal — history fetching for Trading Journal / PnL tracking
    # ══════════════════════════════════════════════════════════════════

    async def async_fetch_order_history(
        self, instrument: str | None = None, since_ms: int | None = None, limit: int = 500,
    ) -> list[dict]:
        """Fetch completed orders from GRVT.

        Uses direct REST: POST /full/v1/order_history
        """
        client = await self._get_async_session()
        url = self._trade_base_url() + "/full/v1/order_history"
        sub_account_id = self.settings.grvt_trading_account_id
        all_orders: list[dict] = []
        cursor: str = ""

        import time as _time
        now_ns = str(int(_time.time() * 1_000_000_000))
        start_ns = str(since_ms * 1_000_000) if since_ms else "0"

        while True:
            payload: dict = {
                "sub_account_id": sub_account_id,
                "kind": ["PERPETUAL"],
                "start_time": start_ns,
                "end_time": now_ns,
                "limit": min(limit, 500),
                "cursor": cursor,
            }
            if instrument:
                payload["base"] = [instrument.split("_")[0]]

            try:
                logger.info("GRVT order history request: %s", payload)
                resp = await client.post(url, json=payload, headers=self._get_auth_headers())
                resp.raise_for_status()
                data = resp.json()
                logger.info("GRVT order history response: count=%d next=%r", len(data.get("result", [])), data.get("next", ""))
            except Exception as exc:
                logger.warning("GRVT order history error: %s", exc)
                break

            results = data.get("result", [])
            if not results:
                break

            for o in results:
                metadata = o.get("metadata", {})
                state = o.get("state", {})
                legs = o.get("legs", [{}])
                leg = legs[0] if legs else {}

                created_ns = int(metadata.get("create_time", 0))
                created_ms = created_ns // 1_000_000 if created_ns > 1e15 else created_ns

                instr = leg.get("instrument", instrument or "")
                status = state.get("status", "").upper()
                # GRVT returns these as arrays (one per leg)
                avg_prices = state.get("avg_fill_price", [0])
                traded_sizes = state.get("traded_size", [0])
                book_sizes = state.get("book_size", [0])
                avg_price = float(avg_prices[0]) if isinstance(avg_prices, list) and avg_prices else float(avg_prices or 0)
                filled_qty = float(traded_sizes[0]) if isinstance(traded_sizes, list) and traded_sizes else float(traded_sizes or 0)
                book_qty = float(book_sizes[0]) if isinstance(book_sizes, list) and book_sizes else float(book_sizes or 0)

                all_orders.append({
                    "exchange_order_id": str(o.get("order_id", metadata.get("client_order_id", ""))),
                    "exchange": "grvt",
                    "instrument": instr,
                    "token": self._extract_token(instr),
                    "side": (leg.get("side", "")).upper() if leg.get("side") else ("BUY" if leg.get("is_buying_asset") else "SELL"),
                    "order_type": o.get("time_in_force", "LIMIT").upper(),
                    "status": status,
                    "price": float(leg.get("limit_price", 0)),
                    "average_price": avg_price,
                    "qty": filled_qty + book_qty,
                    "filled_qty": filled_qty,
                    "fee": 0.0,
                    "reduce_only": 1 if o.get("reduce_only") else 0,
                    "post_only": 1 if o.get("post_only") else 0,
                    "created_at": created_ms,
                    "updated_at": created_ms,
                })

            next_cursor = data.get("next", "")
            if not next_cursor or len(results) < min(limit, 500):
                break
            cursor = next_cursor

        logger.info("GRVT order history: fetched %d orders", len(all_orders))
        return all_orders

    async def async_fetch_fill_history(
        self, instrument: str | None = None, since_ms: int | None = None, limit: int = 500,
    ) -> list[dict]:
        """Fetch fill/trade history from GRVT.

        Uses direct REST: POST /full/v1/fill_history
        """
        client = await self._get_async_session()
        url = self._trade_base_url() + "/full/v1/fill_history"
        sub_account_id = self.settings.grvt_trading_account_id
        all_fills: list[dict] = []
        cursor: str = ""
        since_ns = since_ms * 1_000_000 if since_ms else 0

        while True:
            payload: dict = {
                "sub_account_id": sub_account_id,
                "limit": min(limit, 500),
            }
            if instrument:
                payload["base"] = [instrument.split("_")[0]]
            if cursor:
                payload["cursor"] = cursor

            try:
                resp = await client.post(url, json=payload, headers=self._get_auth_headers())
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("GRVT fill history error: %s", exc)
                break

            results = data.get("result", [])
            if not results:
                break

            hit_cutoff = False
            for f in results:
                created_ns = int(f.get("fill_time", f.get("event_time", 0)))
                if since_ns and created_ns < since_ns:
                    hit_cutoff = True
                    break
                created_ms = created_ns // 1_000_000 if created_ns > 1e15 else created_ns

                instr = f.get("instrument", instrument or "")
                qty = float(f.get("size", f.get("fill_size", 0)))
                price = float(f.get("price", f.get("fill_price", 0)))
                fee = float(f.get("fee", 0))
                is_taker = f.get("is_taker", False)

                all_fills.append({
                    "exchange_fill_id": str(f.get("fill_id", f.get("event_id", ""))),
                    "exchange_order_id": str(f.get("order_id", "")),
                    "exchange": "grvt",
                    "instrument": instr,
                    "token": self._extract_token(instr),
                    "side": (f.get("side", "")).upper() if f.get("side") else ("BUY" if f.get("is_buying_asset") else "SELL"),
                    "price": price,
                    "qty": qty,
                    "value": qty * price,
                    "fee": fee,
                    "is_taker": 1 if is_taker else 0,
                    "trade_type": "TRADE",
                    "created_at": created_ms,
                })

            next_cursor = data.get("next", "")
            if hit_cutoff or not next_cursor or len(results) < min(limit, 500):
                break
            cursor = next_cursor

        logger.info("GRVT fill history: fetched %d fills", len(all_fills))
        return all_fills

    async def async_fetch_funding_payments(
        self, instrument: str | None = None, since_ms: int | None = None, limit: int = 500,
    ) -> list[dict]:
        """Fetch funding payment history from GRVT.

        Uses direct REST: POST /full/v1/funding_payment_history (private, requires auth).
        """
        client = await self._get_async_session()
        url = self._trade_base_url() + "/full/v1/funding_payment_history"
        sub_account_id = self.settings.grvt_trading_account_id
        all_payments: list[dict] = []
        cursor: str = ""

        while True:
            payload: dict = {
                "sub_account_id": sub_account_id,
                "limit": min(limit, 500),
            }
            if instrument:
                payload["base"] = [instrument.split("_")[0]]
            if since_ms:
                payload["start_time"] = str(since_ms * 1_000_000)
            if cursor:
                payload["cursor"] = cursor

            try:
                resp = await client.post(url, json=payload, headers=self._get_auth_headers())
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.warning("GRVT funding payments error: %s", exc)
                break

            results = data.get("result", [])
            if not results:
                break

            for r in results:
                event_ns = int(r.get("event_time", 0))
                event_ms = event_ns // 1_000_000 if event_ns > 1e15 else event_ns

                instr = r.get("instrument", instrument or "")
                all_payments.append({
                    "exchange_payment_id": r.get("tx_id", f"grvt-{instr}-{event_ms}"),
                    "exchange": "grvt",
                    "instrument": instr,
                    "token": self._extract_token(instr),
                    "side": "",
                    "size": 0.0,
                    "funding_fee": float(r.get("amount", 0)),
                    "funding_rate": 0.0,
                    "mark_price": 0.0,
                    "paid_at": event_ms,
                })

            next_cursor = data.get("next", "")
            if not next_cursor or len(results) < min(limit, 500):
                break
            cursor = next_cursor

        logger.info("GRVT funding payments: fetched %d records", len(all_payments))
        return all_payments

    @staticmethod
    def _extract_token(instrument: str) -> str:
        """Extract token from GRVT instrument (e.g. 'BTC_USDT_Perp' -> 'BTC')."""
        parts = instrument.replace("_", "-").split("-")
        return parts[0] if parts else instrument

    async def async_subscribe_funding_rate(self, symbol: str, callback) -> None:
        """Subscribe to GRVT funding rate via WS JSONRPC polling.

        GRVT doesn't have a dedicated funding rate WS stream — we use
        periodic REST polling wrapped in an async loop.
        """
        import asyncio

        logger.info("GRVT funding rate poller started for %s", symbol)
        while True:
            try:
                rate_data = await self.async_fetch_funding_rate(symbol)
                await callback(rate_data)
            except Exception as exc:
                logger.warning("GRVT funding rate poll error: %s", exc)
            await asyncio.sleep(60)  # poll every 60s
