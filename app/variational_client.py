"""Variational Exchange client — RFQ-based DEX (taker-only).

Variational uses a Request-for-Quote model with OLP as the sole market maker.
No orderbook exists; prices come from indicative quotes.
The API is Cloudflare-protected, so we use curl_cffi (browser TLS fingerprint).

Conforms to the AsyncExchangeClient protocol defined in app/exchange.py.
"""

import asyncio
import base64
import json
import logging
import random
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from curl_cffi import requests as _cffi_requests
import requests as _requests

logger = logging.getLogger("tradeautonom.variational_client")

_BASE_URL = "https://omni.variational.io/api"
_STATS_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"

# Default slippage for market orders (0.7%)
_DEFAULT_SLIPPAGE = 0.007

# Browser fingerprints for curl_cffi — rotated on 403 to avoid CF pattern matching
_IMPERSONATE_PROFILES = [
    "chrome120", "chrome119", "chrome116", "safari17_0",
    "chrome110", "edge101", "edge99", "safari15_5", "safari15_3",
]


def _is_http_403(exc: Exception) -> bool:
    """Detect HTTP 403 from requests or curl_cffi exceptions."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        code = getattr(resp, "status_code", None)
        if code == 403:
            return True
    return False


def _http_status(exc: Exception) -> int | None:
    """Extract HTTP status code from a requests/curl_cffi exception, if any."""
    resp = getattr(exc, "response", None)
    if resp is not None:
        return getattr(resp, "status_code", None)
    return None


# The trading API always uses funding_interval_s=3600 regardless of what the
# stats API reports (which may show 14400, 28800, etc.).
_TRADING_FUNDING_INTERVAL = 3600


def _build_instrument(symbol: str) -> dict:
    """Parse config symbol like 'P-SUI-USDC-3600' into Variational instrument dict.

    Format: P-{underlying}-{settlement_asset}-{funding_interval_s}
    The trading API always expects funding_interval_s=3600.
    """
    parts = symbol.split("-")
    if len(parts) != 4 or parts[0] != "P":
        raise ValueError(f"Invalid Variational symbol format: {symbol!r}. Expected P-UNDERLYING-ASSET-INTERVAL (e.g. P-SUI-USDC-3600)")
    return {
        "underlying": parts[1],
        "instrument_type": "perpetual_future",
        "settlement_asset": parts[2],
        "funding_interval_s": _TRADING_FUNDING_INTERVAL,
    }


class VariationalClient:
    """Variational Exchange client — taker-only RFQ perpetuals.

    All trading methods are async (using asyncio.to_thread for curl_cffi).
    """

    def __init__(
        self,
        jwt_token: str,
        wallet_address: str = "",
        base_url: str = _BASE_URL,
        stats_url: str = _STATS_URL,
        proxy_worker_url: str = "",
    ):
        if not jwt_token:
            raise ValueError("Variational requires jwt_token")

        self._jwt_token = jwt_token
        # Extract wallet address from JWT payload if not explicitly provided
        self._wallet_address = wallet_address or self._extract_address_from_jwt(jwt_token)
        self._base_url = base_url.rstrip("/")
        self._stats_url = stats_url

        # CF Worker proxy (bypasses Cloudflare 403 via server-side browser headers)
        self._proxy_url = proxy_worker_url.rstrip("/") if proxy_worker_url else ""
        self._proxy_session: _requests.Session | None = None
        if self._proxy_url:
            self._proxy_session = _requests.Session()
            logger.info("Variational: CF Worker proxy enabled → %s", self._proxy_url)

        # curl_cffi session (fallback — real browser TLS fingerprint to bypass Cloudflare)
        self._cffi_session = _cffi_requests.Session(impersonate="chrome120")

        # Cache for market stats (refreshed periodically)
        self._stats_cache: dict = {}
        self._stats_cache_ts: float = 0.0
        self._stats_cache_ttl: float = 5.0  # seconds

        # Cache for transfers (realized PnL + funding) per instrument
        self._transfers_cache: dict[str, dict[str, float]] = {}
        self._transfers_cache_ts: dict[str, float] = {}
        self._TRANSFERS_CACHE_TTL = 120  # seconds

        # Auth-status tracker — last observed authentication state, exposed via
        # `auth_status` property so the /account/positions endpoint and the
        # frontend can surface a clear "token expired/revoked" hint to the
        # user instead of silently showing an empty positions list.
        self._auth_status: dict = {
            "ok": True,                    # True until we see a 401/403
            "last_status_code": None,      # int|None of last HTTP code
            "last_error": None,            # str|None human-readable hint
            "last_check_ts": 0.0,          # epoch seconds of last update
            "consecutive_failures": 0,     # incremented on 401/403, reset on 2xx
        }

        logger.info("VariationalClient initialized: wallet=%s...%s proxy=%s", self._wallet_address[:6], self._wallet_address[-4:], bool(self._proxy_url))

    @property
    def name(self) -> str:
        return "variational"

    @property
    def auth_status(self) -> dict:
        """Last-observed Variational auth state.

        Returns a dict with:
          - ok: bool — False if last call returned 401/403
          - last_status_code: int|None — the HTTP code observed
          - last_error: str|None — short human hint (e.g. "JWT revoked")
          - last_check_ts: float — epoch seconds of last update
          - consecutive_failures: int — number of consecutive 401/403 calls

        Consumed by /account/positions to surface a clear UI banner so the
        user can refresh the vr-token in Settings without guessing why
        positions are missing.
        """
        return dict(self._auth_status)  # shallow copy — caller should not mutate

    def _record_auth_ok(self) -> None:
        """Mark auth as healthy after a 2xx response."""
        self._auth_status.update({
            "ok": True,
            "last_status_code": 200,
            "last_error": None,
            "last_check_ts": time.time(),
            "consecutive_failures": 0,
        })

    def _record_auth_failure(self, status_code: int | None, hint: str) -> None:
        """Mark auth as failing after a 401/403 (or other auth-relevant) response."""
        self._auth_status["ok"] = False
        self._auth_status["last_status_code"] = status_code
        self._auth_status["last_error"] = hint
        self._auth_status["last_check_ts"] = time.time()
        self._auth_status["consecutive_failures"] += 1

    def _record_auth_failure_from_exc(self, exc: Exception, url: str) -> None:
        """Inspect a request exception and update auth_status + log accordingly.

        Only acts on auth-relevant statuses (401/403). Other errors (5xx,
        timeouts) are not auth failures and leave the status unchanged.
        """
        code = _http_status(exc)
        if code not in (401, 403):
            return
        path = url.replace(self._base_url, "").replace(self._proxy_url, "") or url
        token_tail = self._jwt_token[-4:] if self._jwt_token and len(self._jwt_token) >= 4 else "?"
        wallet_tail = (
            f"{self._wallet_address[:6]}...{self._wallet_address[-4:]}"
            if self._wallet_address and len(self._wallet_address) >= 10
            else self._wallet_address
        )

        # Decode JWT exp to differentiate "expired" vs "revoked"
        is_expired = False
        try:
            parts = self._jwt_token.split(".")
            if len(parts) >= 2:
                payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
                data = json.loads(base64.b64decode(payload_b64))
                exp = data.get("exp", 0)
                if exp and int(time.time()) > exp:
                    is_expired = True
        except Exception:
            pass

        if code == 401:
            if is_expired:
                hint = "Token expired (Variational returned 401 'No token'). Refresh in Settings."
            else:
                hint = "Token rejected as 'No token' by Variational despite being sent. Cookie may be malformed or token type mismatched. Refresh in Settings."
        else:  # 403
            if is_expired:
                hint = "Token expired. Refresh vr-token in Settings."
            else:
                hint = "Token revoked server-side (likely re-login on omni.variational.io rotated the JWT). Refresh vr-token in Settings."

        self._record_auth_failure(code, hint)
        logger.error(
            "[VARIATIONAL-AUTH] %d on %s — %s (token=***%s, wallet=%s, fails=%d)",
            code, path, hint, token_tail, wallet_tail,
            self._auth_status["consecutive_failures"],
        )

    # ── Internal helpers ───────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "vr-connected-address": self._wallet_address,
        }

    def _cookies(self) -> dict:
        return {"vr-token": self._jwt_token}

    @staticmethod
    def _extract_address_from_jwt(token: str) -> str:
        """Extract wallet address from JWT payload."""
        try:
            payload_b64 = token.split(".")[1]
            payload_b64 += "=" * (4 - len(payload_b64) % 4)
            data = json.loads(base64.b64decode(payload_b64))
            addr = data.get("address", "")
            if addr:
                logger.info("Variational: extracted wallet address from JWT: %s...%s", addr[:6], addr[-4:])
                return addr
        except Exception as exc:
            logger.warning("Variational: could not extract address from JWT: %s", exc)
        return ""

    def update_jwt(self, new_token: str) -> None:
        """Update JWT token (e.g. after manual refresh from browser).

        Also:
          - Resets the auth_status tracker so the next request re-evaluates
            health (otherwise stale 401/403 would persist after a fix).
          - Clears the proxy session's cookie jar so old vr-token cookies
            from prior responses don't compete with the new per-request cookie.
          - Logs token expiry status so an already-expired token is noticed
            immediately rather than after the first failed request.
        """
        self._jwt_token = new_token
        new_addr = self._extract_address_from_jwt(new_token)
        if new_addr:
            self._wallet_address = new_addr

        # Reset auth tracking — let the next call re-establish health
        self._auth_status = {
            "ok": True,
            "last_status_code": None,
            "last_error": None,
            "last_check_ts": time.time(),
            "consecutive_failures": 0,
        }

        # Clear cookie jars so any old vr-token (from Set-Cookie responses
        # to earlier requests) cannot override the per-request cookie sent
        # via cookies=self._cookies()
        if self._proxy_session is not None:
            try:
                self._proxy_session.cookies.clear()
            except Exception:
                pass
        try:
            # curl_cffi sessions don't support cookies.clear() reliably; just
            # rebuild the session with the same impersonation profile
            current_fp = getattr(self._cffi_session, "_impersonate", "chrome120")
            self._cffi_session = _cffi_requests.Session(impersonate=current_fp)
        except Exception:
            pass

        # Log expiry of the new token so a stale paste is caught immediately
        try:
            parts = new_token.split(".")
            if len(parts) >= 2:
                payload_b64 = parts[1] + "=" * (4 - len(parts[1]) % 4)
                data = json.loads(base64.b64decode(payload_b64))
                exp = data.get("exp", 0)
                if exp:
                    now = int(time.time())
                    secs_left = exp - now
                    if secs_left <= 0:
                        logger.error(
                            "[VARIATIONAL-AUTH] update_jwt received an ALREADY EXPIRED "
                            "token (exp was %ds ago). Refresh from omni.variational.io.",
                            -secs_left,
                        )
                    elif secs_left < 3600:
                        logger.warning(
                            "[VARIATIONAL-AUTH] update_jwt: new token expires in %ds (<1h)",
                            secs_left,
                        )
                    else:
                        logger.info(
                            "Variational JWT token updated (valid for %.1fh, wallet=%s...%s)",
                            secs_left / 3600,
                            self._wallet_address[:6] if self._wallet_address else "?",
                            self._wallet_address[-4:] if self._wallet_address else "?",
                        )
                    return
        except Exception:
            pass
        logger.info("Variational JWT token updated")

    def _rotate_fingerprint(self) -> None:
        """Switch curl_cffi to a different browser TLS fingerprint."""
        current = getattr(self._cffi_session, "_impersonate", "chrome")
        candidates = [p for p in _IMPERSONATE_PROFILES if p != current]
        new_fp = random.choice(candidates) if candidates else "chrome"
        try:
            self._cffi_session = _cffi_requests.Session(impersonate=new_fp)
        except Exception:
            self._cffi_session = _cffi_requests.Session(impersonate="chrome")
            new_fp = "chrome"
        logger.info("Variational: rotated TLS fingerprint %s → %s", current, new_fp)

    def _retry_on_403(self, fn, *args, max_retries: int | None = 3, **kwargs) -> Any:
        """Call fn(*args, **kwargs), retrying on HTTP 403.

        Uses exponential backoff with jitter and rotates the curl_cffi browser
        fingerprint on each retry to present a different TLS profile to Cloudflare.

        max_retries: int = give up after N retries (default 3).
                     None = retry indefinitely (use for IOC orders).

        Backoff: 3s base, 1.5× growth, ±20% jitter.
          - Finite retries: cap at 30s (non-critical, can wait longer).
          - Infinite retries: cap at 5s (critical order flow, must recover fast).
        """
        attempt = 0
        base_delay = 3.0
        max_delay = 5.0 if max_retries is None else 30.0
        while True:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                if _is_http_403(exc):
                    attempt += 1
                    if max_retries is not None and attempt > max_retries:
                        logger.error(
                            "Variational 403: giving up after %d retries", max_retries,
                        )
                        raise
                    delay = min(base_delay * (1.5 ** (attempt - 1)), max_delay)
                    jitter = delay * 0.2 * (2 * random.random() - 1)  # ±20%
                    wait = delay + jitter
                    logger.warning(
                        "Variational 403 (attempt %d/%s) — rotating fingerprint, retrying in %.1fs",
                        attempt, max_retries or '∞', wait,
                    )
                    self._rotate_fingerprint()
                    time.sleep(wait)
                    continue
                raise

    def _proxy_rewrite_url(self, url: str) -> str:
        """Rewrite omni.variational.io URL to CF Worker proxy URL."""
        return url.replace(self._base_url, self._proxy_url, 1)

    def _sync_get(self, url: str, params: dict | None = None, max_retries: int = 0) -> Any:
        """Synchronous GET.

        Public endpoints (stats API) → direct via curl_cffi (no auth needed).
        Authenticated endpoints → CF Worker proxy only (when configured).
        Direct requests to omni.variational.io are blocked by Cloudflare — no curl_cffi fallback.

        max_retries=0 (default): no retry, fail fast (dashboard/portfolio reads).
        max_retries=N: retry N times on 403 (position checks in order flow).

        Updates self._auth_status on 2xx (auth ok) and 401/403 (auth failure)
        so consumers can surface a clear "token expired/revoked" message.
        """
        # Public stats API — always direct, no auth (does not affect auth_status)
        if url.startswith(self._stats_url):
            resp = self._cffi_session.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()

        # Authenticated endpoints — proxy only (direct is always blocked by Cloudflare)
        if self._proxy_session and url.startswith(self._base_url):
            proxy_url = self._proxy_rewrite_url(url)

            def _do_proxy_get():
                resp = self._proxy_session.get(
                    proxy_url, headers=self._headers(), cookies=self._cookies(),
                    params=params, timeout=15,
                )
                resp.raise_for_status()
                self._record_auth_ok()
                return resp.json()

            try:
                if max_retries == 0:
                    return _do_proxy_get()
                return self._retry_on_403(_do_proxy_get, max_retries=max_retries)
            except Exception as exc:
                self._record_auth_failure_from_exc(exc, url)
                raise

        # No proxy configured — curl_cffi direct (dev/testing only)
        def _do_get():
            resp = self._cffi_session.get(
                url, headers=self._headers(), cookies=self._cookies(),
                params=params, timeout=15,
            )
            resp.raise_for_status()
            self._record_auth_ok()
            return resp.json()

        try:
            if max_retries == 0:
                return _do_get()
            return self._retry_on_403(_do_get, max_retries=max_retries)
        except Exception as exc:
            self._record_auth_failure_from_exc(exc, url)
            raise

    def _sync_post(self, endpoint: str, payload: dict, max_retries: int | None = 3) -> Any:
        """Synchronous POST — CF Worker proxy only (when configured).

        Direct requests to omni.variational.io are blocked by Cloudflare — no curl_cffi fallback.

        max_retries=3 (default): retry 3× on proxy failure (non-critical POSTs).
        max_retries=None: retry indefinitely (IOC orders — must not give up).
        """
        post_url = f"{self._base_url}/{endpoint.lstrip('/')}"

        # Proxy only — direct is always blocked by Cloudflare
        if self._proxy_session:
            proxy_url = self._proxy_rewrite_url(post_url)

            def _do_proxy_post():
                resp = self._proxy_session.post(
                    proxy_url, headers=self._headers(), cookies=self._cookies(),
                    json=payload, timeout=15,
                )
                resp.raise_for_status()
                self._record_auth_ok()
                return resp.json()

            try:
                return self._retry_on_403(_do_proxy_post, max_retries=max_retries)
            except Exception as exc:
                self._record_auth_failure_from_exc(exc, post_url)
                raise

        # No proxy configured — curl_cffi direct (dev/testing only)
        def _do_post():
            resp = self._cffi_session.post(
                post_url, headers=self._headers(), cookies=self._cookies(),
                json=payload, timeout=15,
            )
            resp.raise_for_status()
            self._record_auth_ok()
            return resp.json()

        try:
            return self._retry_on_403(_do_post, max_retries=max_retries)
        except Exception as exc:
            self._record_auth_failure_from_exc(exc, post_url)
            raise

    async def _async_get(self, url: str, params: dict | None = None, max_retries: int = 0) -> Any:
        return await asyncio.to_thread(self._sync_get, url, params, max_retries)

    async def _async_post(self, endpoint: str, payload: dict, max_retries: int | None = 3) -> Any:
        return await asyncio.to_thread(self._sync_post, endpoint, payload, max_retries)

    # ── Market data ────────────────────────────────────────────────────

    async def _get_stats(self, force: bool = False) -> dict:
        """Fetch /metadata/stats with caching."""
        now = time.time()
        if not force and self._stats_cache and (now - self._stats_cache_ts) < self._stats_cache_ttl:
            return self._stats_cache
        try:
            data = await asyncio.to_thread(self._sync_get, self._stats_url)
            self._stats_cache = data
            self._stats_cache_ts = now
        except Exception as exc:
            logger.warning("Variational stats fetch error: %s", exc)
            if self._stats_cache:
                return self._stats_cache
            raise
        return self._stats_cache

    def _find_listing(self, stats: dict, symbol: str) -> dict | None:
        """Find listing by symbol. symbol = 'P-SUI-USDC-3600' → underlying='SUI'."""
        inst = _build_instrument(symbol)
        underlying = inst["underlying"].upper()
        for listing in stats.get("listings", []):
            if listing.get("ticker", "").upper() == underlying:
                return listing
        return None

    def _find_listing_by_underlying(self, stats: dict, underlying: str) -> dict | None:
        """Find a Variational listing by underlying token name (case-insensitive).

        Used when a position object only carries an `underlying` field without
        the full instrument metadata — specifically `funding_interval_s`. The
        live stats listing always carries the current funding interval, so we
        use it to canonicalise the symbol of returned position objects.
        """
        if not underlying:
            return None
        target = underlying.upper()
        for listing in stats.get("listings", []):
            if listing.get("ticker", "").upper() == target:
                return listing
        return None

    async def async_fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        """Build synthetic orderbook from Variational quotes.

        Returns bid/ask from the size_1k quote (closest to spot for small trades).
        Single-level "book" since Variational is RFQ, not orderbook.
        """
        stats = await self._get_stats()
        listing = self._find_listing(stats, symbol)
        if not listing:
            logger.warning("Variational: no listing found for %s", symbol)
            return {"bids": [], "asks": []}

        quotes = listing.get("quotes", {})
        # Use size_1k as primary (tightest spread), size_100k as depth
        bids = []
        asks = []
        for size_key in ["size_1k", "size_100k", "size_1m"]:
            q = quotes.get(size_key)
            if q and q.get("bid") and q.get("ask"):
                bids.append([q["bid"], "1000" if "1k" in size_key else ("100000" if "100k" in size_key else "1000000")])
                asks.append([q["ask"], "1000" if "1k" in size_key else ("100000" if "100k" in size_key else "1000000")])

        return {"bids": bids, "asks": asks}

    async def async_fetch_markets(self) -> list[dict]:
        """Return list of available perpetual markets on Variational."""
        stats = await self._get_stats(force=True)
        result = []
        for listing in stats.get("listings", []):
            ticker = listing.get("ticker", "")
            fi = listing.get("funding_interval_s", 3600)
            symbol = f"P-{ticker}-USDC-{fi}"
            result.append({
                "symbol": symbol,
                "name": f"{ticker}/USDC Perp",
                "underlying": ticker,
                "mark_price": listing.get("mark_price", "0"),
                "funding_rate": listing.get("funding_rate", "0"),
                "base_spread_bps": listing.get("base_spread_bps", "0"),
            })
        return result

    async def async_get_min_order_size(self, symbol: str) -> Decimal:
        """Minimum order size (in base units). Variational min is qty=1 string."""
        return Decimal("1")

    async def async_get_tick_size(self, symbol: str) -> Decimal:
        """Price tick size — derived from quote precision."""
        return Decimal("0.01")

    # ── Order management ───────────────────────────────────────────────

    async def async_create_post_only_order(
        self, symbol: str, side: str, amount: Decimal, price: Decimal,
        reduce_only: bool = False,
    ) -> dict:
        """Not supported — Variational is taker-only (OLP is the sole maker)."""
        raise NotImplementedError("Variational does not support post-only (maker) orders — it is a taker-only RFQ exchange")

    async def async_create_ioc_order(
        self, symbol: str, side: str, amount: Decimal, price: Decimal,
        reduce_only: bool = False,
        max_slippage: float | None = None,
    ) -> dict:
        """Execute a market order via 2-step RFQ: get quote → place order.

        Parameters
        ----------
        symbol, side, amount, price : standard order parameters. ``price`` is
            currently informational; the OLP fills at its quote subject to
            ``max_slippage``.
        reduce_only : bool
            Pass ``True`` for exit orders so an unintended re-open is impossible
            even if the persisted position state was stale.
        max_slippage : float | None
            Slippage cap as a fraction (e.g. 0.007 = 0.7%). When ``None``, falls
            back to the module-level ``_DEFAULT_SLIPPAGE``. Used by the Gold-
            Spread bot to wire its ``max_slippage_pct`` config (normal trades)
            and ``unwind_slippage_pct`` (emergency unwinds) through to the
            Variational API rather than relying on the hard-coded default.
        """
        instrument = _build_instrument(symbol)
        slippage = float(max_slippage) if max_slippage is not None else _DEFAULT_SLIPPAGE

        # Step 1: Get indicative quote
        quote_payload = {
            "instrument": instrument,
            "direction": side.lower(),
            "qty": str(amount),
        }
        logger.info("Variational IOC: getting quote for %s %s %s @ instrument=%s", side, amount, symbol, instrument["underlying"])
        try:
            quote_resp = await self._async_post("quotes/indicative", quote_payload, max_retries=None)
        except Exception as exc:
            logger.error("Variational quote failed: %s", exc)
            return {"id": "", "status": "QUOTE_FAILED", "error": str(exc), "traded_qty": 0.0}

        quote_id = quote_resp.get("quote_id") or quote_resp.get("id", "")
        if not quote_id:
            logger.error("Variational quote response missing quote_id: %s", json.dumps(quote_resp)[:300])
            return {"id": "", "status": "QUOTE_FAILED", "error": "no quote_id in response", "traded_qty": 0.0}

        logger.info("Variational IOC: quote_id=%s, placing market order %s %s", quote_id, side, amount)

        # Step 2: Place market order with quote
        order_payload = {
            "instrument": instrument,
            "side": side.lower(),
            "qty": str(amount),
            "quote_id": quote_id,
            "max_slippage": slippage,
            "is_reduce_only": reduce_only,
        }
        # Snapshot position BEFORE order to verify fill via delta
        pre_pos_size = 0.0
        try:
            pre_positions = await self.async_fetch_positions(symbols=[symbol], max_retries=None)
            if pre_positions:
                pre_pos_size = pre_positions[0].get("size", 0.0)
        except Exception as exc:
            logger.warning("Variational: pre-order position snapshot failed: %s — will use order response", exc)

        try:
            order_resp = await self._async_post("orders/new/market", order_payload, max_retries=None)
        except Exception as exc:
            logger.error("Variational market order failed: %s", exc)
            return {"id": quote_id, "status": "ORDER_FAILED", "error": str(exc), "traded_qty": 0.0}

        # Extract order ID (rfqid used for cancel/status)
        order_id = order_resp.get("rfq_id") or order_resp.get("rfqid") or order_resp.get("id") or quote_id
        status = order_resp.get("status", "FILLED")

        # Verify fill via position delta (don't blindly assume 100% fill)
        traded_qty = 0.0
        try:
            await asyncio.sleep(0.5)  # short settle time
            post_positions = await self.async_fetch_positions(symbols=[symbol], max_retries=None)
            post_pos_size = post_positions[0].get("size", 0.0) if post_positions else 0.0
            actual_delta = abs(post_pos_size - pre_pos_size)
            if actual_delta > 0.001:
                traded_qty = actual_delta
                logger.info("Variational IOC: position delta verified fill: pre=%.6f post=%.6f delta=%.6f", pre_pos_size, post_pos_size, actual_delta)
            else:
                logger.warning("Variational IOC: position delta=%.6f (pre=%.6f post=%.6f) — order may not have filled", actual_delta, pre_pos_size, post_pos_size)
        except Exception as exc:
            logger.warning("Variational: post-order position check failed: %s — falling back to requested amount", exc)
            traded_qty = float(amount)

        logger.info("Variational IOC result: id=%s status=%s traded_qty=%s", order_id, status, traded_qty)
        return {
            "id": order_id,
            "status": status,
            "traded_qty": traded_qty,
            "quote_id": quote_id,
            "order": order_resp,
        }

    async def async_create_limit_order(
        self, symbol: str, side: str, amount: Decimal, price: Decimal,
        reduce_only: bool = False,
    ) -> dict:
        """Place a limit order (single-step, no quote needed)."""
        instrument = _build_instrument(symbol)
        payload = {
            "order_type": "limit",
            "limit_price": str(price),
            "side": side.lower(),
            "instrument": instrument,
            "qty": str(amount),
            "slippage_limit": str(_DEFAULT_SLIPPAGE),
            "is_auto_resize": False,
            "use_mark_price": False,
            "is_reduce_only": reduce_only,
        }
        logger.info("Variational limit order: %s %s %s @ %s", side, amount, symbol, price)
        try:
            resp = await self._async_post("orders/new/limit", payload)
        except Exception as exc:
            logger.error("Variational limit order failed: %s", exc)
            return {"id": "", "status": "FAILED", "error": str(exc)}

        order_id = resp.get("rfqid") or resp.get("id", "")
        logger.info("Variational limit order placed: id=%s", order_id)
        return {"id": order_id, "status": resp.get("status", "SUBMITTED"), "order": resp}

    async def async_cancel_order(self, order_id: str) -> bool:
        """Cancel an order by rfqid."""
        try:
            await self._async_post("orders/cancel", {"rfqid": order_id})
            logger.info("Variational order cancelled: %s", order_id)
            return True
        except Exception as exc:
            logger.warning("Variational cancel_order(%s) error: %s", order_id, exc)
            return False

    async def async_check_order_fill(self, order_id: str) -> dict:
        """Check order status via GET /orders/v2."""
        try:
            orders = await self._async_get(
                f"{self._base_url}/orders/v2",
                params={"status": "pending"},
            )
            # If order is still in pending list, it's not fully filled
            if isinstance(orders, list):
                for o in orders:
                    rfqid = o.get("rfqid", "")
                    if rfqid == order_id:
                        return {
                            "filled": False,
                            "status": "PENDING",
                            "traded_qty": float(o.get("filled_qty", 0)),
                            "order": o,
                        }
            # Not in pending → assume filled (or cancelled)
            # Try to check filled orders
            try:
                filled_orders = await self._async_get(
                    f"{self._base_url}/orders/v2",
                    params={"status": "filled"},
                )
                if isinstance(filled_orders, list):
                    for o in filled_orders:
                        if o.get("rfqid") == order_id:
                            qty = float(o.get("filled_qty", o.get("qty", 0)))
                            return {
                                "filled": True,
                                "status": "FILLED",
                                "traded_qty": qty,
                                "avg_price": float(o.get("avg_price", o.get("price", 0))),
                                "order": o,
                            }
            except Exception:
                pass
            # Assume filled if not in pending
            return {"filled": True, "status": "ASSUMED_FILLED", "traded_qty": 0.0}
        except Exception as exc:
            logger.warning("Variational check_order_fill(%s) error: %s", order_id, exc)
            return {"filled": False, "status": "ERROR", "error": str(exc), "traded_qty": 0.0}

    # ── Positions ──────────────────────────────────────────────────────

    async def async_check_auth(self) -> None:
        """Verify the JWT token is still valid by hitting an authenticated endpoint.

        Raises on failure (e.g. expired JWT → HTTP 401/403).
        """
        await self._async_get(f"{self._base_url}/positions")

    async def async_fetch_positions(self, symbols: list[str] | None = None, max_retries: int = 0) -> list[dict]:
        """Fetch open positions from Variational.

        Raises on HTTP errors (4xx/5xx) so callers can distinguish
        'no position' from 'API error' and retry accordingly.

        max_retries=0 (default): no retry (dashboard reads).
        max_retries=N: retry N times on 403 (order flow position checks).
        """
        positions = await self._async_get(f"{self._base_url}/positions", max_retries=max_retries)

        result = []
        if not isinstance(positions, list):
            positions = positions.get("positions", []) if isinstance(positions, dict) else []

        for pos in positions:
            # Variational wraps data in position_info
            pi = pos.get("position_info", pos)
            inst = pi.get("instrument", {})
            underlying = inst.get("underlying", "") if isinstance(inst, dict) else str(inst)
            fi_raw = inst.get("funding_interval_s") if isinstance(inst, dict) else None
            settle = inst.get("settlement_asset", "USDC") if isinstance(inst, dict) else "USDC"

            # Variational position objects sometimes carry only `underlying`
            # without `funding_interval_s` — but the live /metadata/stats
            # listing for that token always has the current value. Fall back
            # to it so the resulting `full_symbol` matches what bot configs
            # and the BotDetailView UI filter expect.
            if fi_raw is None:
                if not self._stats_cache or (time.time() - self._stats_cache_ts) > 30:
                    try:
                        await self._get_stats(force=True)
                    except Exception:
                        pass
                listing_for_fi = self._find_listing_by_underlying(
                    self._stats_cache, underlying,
                )
                fi = (
                    int(listing_for_fi.get("funding_interval_s", 3600))
                    if listing_for_fi
                    else 3600
                )
            else:
                fi = int(fi_raw)

            # Build full symbol matching the format used in config/jobs
            full_symbol = f"P-{underlying}-{settle}-{fi}"

            size = float(pi.get("qty", pi.get("size", 0)))
            side_val = pi.get("side", "long" if size > 0 else "short")
            entry_price = float(pi.get("avg_entry_price", pi.get("entry_price", 0)))

            # Filter by symbol if specified
            if symbols:
                match = False
                for s in symbols:
                    if s == full_symbol:
                        match = True
                        break
                    try:
                        inst_parsed = _build_instrument(s)
                        if inst_parsed["underlying"].upper() == underlying.upper():
                            match = True
                            break
                    except ValueError:
                        if underlying.upper() in s.upper():
                            match = True
                            break
                if not match:
                    continue

            # Look up mark_price from stats cache
            mark_price = 0.0
            if self._stats_cache:
                listing = self._find_listing(self._stats_cache, full_symbol)
                if listing:
                    mark_price = float(listing.get("mark_price", 0))

            # Optional Variational-specific fields. The position object's
            # `value` is the USD notional (negative for shorts) and
            # `estimated_liquidation_price` is the liquidation level. We
            # surface them so the BotDetailView UI can render them.
            est_liq_raw = pos.get("estimated_liquidation_price")
            est_liq = float(est_liq_raw) if est_liq_raw not in (None, "") else None
            try:
                value_usd = float(pos.get("value", 0))
            except (TypeError, ValueError):
                value_usd = 0.0

            result.append({
                "symbol": full_symbol,
                "instrument": full_symbol,
                # `underlying` lets the frontend match positions by token name
                # when the symbol's funding-interval suffix has drifted (e.g.
                # bot config has "P-XRP-USDC-28800" but Variational position
                # object still carries "P-XRP-USDC-3600" from open-time).
                "underlying": underlying,
                "size": abs(size),
                "side": side_val,
                "entry_price": entry_price,
                "mark_price": mark_price,
                "unrealized_pnl": float(pos.get("upnl", 0)),
                "realized_pnl": float(pos.get("rpnl", 0)),
                "cumulative_funding": float(pos.get("cum_funding", 0)),
                "est_liquidation_price": est_liq,
                "value": value_usd,
                "raw": pos,
            })
        return result

    # ── Funding rate ───────────────────────────────────────────────────

    async def async_fetch_funding_rate(self, symbol: str) -> dict:
        """Fetch funding rate from /metadata/stats for a given symbol."""
        stats = await self._get_stats()
        listing = self._find_listing(stats, symbol)
        if not listing:
            return {"symbol": symbol, "funding_rate": 0.0, "next_funding_time": None}

        rate = float(listing.get("funding_rate", 0))
        interval = listing.get("funding_interval_s", 3600)
        return {
            "symbol": symbol,
            "funding_rate": rate,
            "funding_interval_s": interval,
            "mark_price": float(listing.get("mark_price", 0)),
            "next_funding_time": None,
        }

    # ── Transfers (realized PnL + funding payments) ─────────────────

    def _fetch_transfers_by_instrument(self, transfer_type: str) -> dict[str, float]:
        """Fetch all transfers of a type, paginated, and sum amounts per instrument.

        Uses GET /api/transfers?type={transfer_type} with full date range.
        Returns dict mapping instrument symbol (e.g. 'P-BNB-USDC-3600') to cumulative amount.
        """
        now = time.time()
        if transfer_type in self._transfers_cache and (now - self._transfers_cache_ts.get(transfer_type, 0)) < self._TRANSFERS_CACHE_TTL:
            return self._transfers_cache[transfer_type]

        totals: dict[str, float] = {}
        offset = 0
        limit = 100
        # Use a very wide date range to get all transfers
        gte = "2020-01-01T00:00:00.000Z"
        lte = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        try:
            while True:
                params = {
                    "type": transfer_type,
                    "order_by": "created_at",
                    "order": "desc",
                    "limit": limit,
                    "offset": offset,
                    "created_at_gte": gte,
                    "created_at_lte": lte,
                }
                url = f"{self._base_url}/transfers"
                data = self._sync_get(url, params=params)

                # Extract entries from response
                if isinstance(data, list):
                    entries = data
                elif isinstance(data, dict):
                    entries = data.get("result", data.get("data", data.get("transfers", [])))
                else:
                    entries = []

                if not entries:
                    break

                for entry in entries:
                    # Variational: instrument info is in 'reference_instrument', amount in 'qty'
                    ref_inst = entry.get("reference_instrument", entry.get("instrument", None))
                    underlying = ""
                    settle = "USDC"
                    fi = 3600

                    if isinstance(ref_inst, dict):
                        underlying = ref_inst.get("underlying", "")
                        settle = ref_inst.get("settlement_asset", "USDC")
                        fi = ref_inst.get("funding_interval_s", 3600)
                    elif isinstance(ref_inst, str) and ref_inst:
                        underlying = ref_inst

                    if not underlying:
                        symbol = "_unknown"
                    elif underlying.startswith("P-"):
                        symbol = underlying
                    else:
                        symbol = f"P-{underlying}-{settle}-{fi}"

                    qty = float(entry.get("qty", entry.get("amount", entry.get("value", 0))))
                    totals[symbol] = totals.get(symbol, 0.0) + qty

                if len(entries) < limit:
                    break
                offset += limit
        except Exception as exc:
            logger.warning("Variational _fetch_transfers_by_instrument(%s) error: %s (type=%s)", transfer_type, exc, type(exc).__name__)

        self._transfers_cache[transfer_type] = totals
        self._transfers_cache_ts[transfer_type] = now
        return totals

    async def _async_fetch_transfers_by_instrument(self, transfer_type: str) -> dict[str, float]:
        """Async version of _fetch_transfers_by_instrument."""
        return await asyncio.to_thread(self._fetch_transfers_by_instrument, transfer_type)

    def fetch_funding_payments(self, symbol: str) -> float:
        """Fetch cumulative funding payments for a given symbol from /api/transfers?type=funding."""
        totals = self._fetch_transfers_by_instrument("funding")
        return totals.get(symbol, 0.0)

    async def async_fetch_funding_payments(self, symbol: str) -> float:
        """Async version of fetch_funding_payments."""
        totals = await self._async_fetch_transfers_by_instrument("funding")
        return totals.get(symbol, 0.0)

    def _get_realized_pnl_by_instrument(self) -> dict[str, float]:
        """Fetch cumulative realized PnL per instrument from /api/transfers?type=realized_pnl."""
        return self._fetch_transfers_by_instrument("realized_pnl")

    async def _async_get_realized_pnl_by_instrument(self) -> dict[str, float]:
        """Async version."""
        return await self._async_fetch_transfers_by_instrument("realized_pnl")

    # ── Sync wrappers (legacy ExchangeClient protocol) ──────────────

    def fetch_markets(self) -> list[dict]:
        """Sync wrapper for async_fetch_markets (used by /exchanges/markets endpoint)."""
        try:
            stats = self._sync_get(self._stats_url)
            self._stats_cache = stats
            self._stats_cache_ts = time.time()
        except Exception as exc:
            logger.warning("Variational sync fetch_markets error: %s", exc)
            stats = self._stats_cache or {"listings": []}

        result = []
        for listing in stats.get("listings", []):
            ticker = listing.get("ticker", "")
            fi = listing.get("funding_interval_s", 3600)
            symbol = f"P-{ticker}-USDC-{fi}"
            result.append({"symbol": symbol, "name": f"{ticker}/USDC Perp"})
        return result

    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        """Sync wrapper for async_fetch_order_book."""
        try:
            stats = self._sync_get(self._stats_url)
            self._stats_cache = stats
            self._stats_cache_ts = time.time()
        except Exception:
            stats = self._stats_cache or {"listings": []}

        listing = self._find_listing(stats, symbol)
        if not listing:
            return {"bids": [], "asks": []}

        quotes = listing.get("quotes", {})
        bids, asks = [], []
        for size_key in ["size_1k", "size_100k", "size_1m"]:
            q = quotes.get(size_key)
            if q and q.get("bid") and q.get("ask"):
                notional = "1000" if "1k" in size_key else ("100000" if "100k" in size_key else "1000000")
                bids.append([q["bid"], notional])
                asks.append([q["ask"], notional])
        return {"bids": bids, "asks": asks}

    def fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        """Sync wrapper for async_fetch_positions (used by /account/positions endpoint)."""
        try:
            positions = self._sync_get(f"{self._base_url}/positions")
        except Exception as exc:
            logger.warning("Variational sync fetch_positions error: %s", exc)
            return []

        # Refresh stats cache for mark_price lookup
        try:
            stats = self._sync_get(self._stats_url)
            self._stats_cache = stats
            self._stats_cache_ts = time.time()
        except Exception:
            stats = self._stats_cache or {"listings": []}

        result = []
        if not isinstance(positions, list):
            positions = positions.get("positions", []) if isinstance(positions, dict) else []

        for pos in positions:
            pi = pos.get("position_info", pos)
            inst = pi.get("instrument", {})
            underlying = inst.get("underlying", "") if isinstance(inst, dict) else str(inst)
            fi_raw = inst.get("funding_interval_s") if isinstance(inst, dict) else None
            settle = inst.get("settlement_asset", "USDC") if isinstance(inst, dict) else "USDC"

            # Variational position objects sometimes carry only `underlying`
            # without `funding_interval_s`. Use the live stats listing
            # (already refreshed above) to canonicalise the symbol so it
            # matches what bot configs and the BotDetailView UI filter expect.
            if fi_raw is None:
                listing_for_fi = self._find_listing_by_underlying(stats, underlying)
                fi = (
                    int(listing_for_fi.get("funding_interval_s", 3600))
                    if listing_for_fi
                    else 3600
                )
            else:
                fi = int(fi_raw)

            full_symbol = f"P-{underlying}-{settle}-{fi}"

            size = float(pi.get("qty", pi.get("size", 0)))
            side_val = pi.get("side", "long" if size > 0 else "short")
            entry_price = float(pi.get("avg_entry_price", pi.get("entry_price", 0)))

            if symbols:
                match = False
                for s in symbols:
                    if s == full_symbol:
                        match = True
                        break
                    try:
                        inst_parsed = _build_instrument(s)
                        if inst_parsed["underlying"].upper() == underlying.upper():
                            match = True
                            break
                    except ValueError:
                        if underlying.upper() in s.upper():
                            match = True
                            break
                if not match:
                    continue

            mark_price = 0.0
            listing = self._find_listing(stats, full_symbol)
            if listing:
                mark_price = float(listing.get("mark_price", 0))

            # Optional Variational-specific fields. The position object's
            # `value` is the USD notional (negative for shorts) and
            # `estimated_liquidation_price` is the liquidation level. We
            # surface them so the BotDetailView UI can render them.
            est_liq_raw = pos.get("estimated_liquidation_price")
            est_liq = float(est_liq_raw) if est_liq_raw not in (None, "") else None
            try:
                value_usd = float(pos.get("value", 0))
            except (TypeError, ValueError):
                value_usd = 0.0

            result.append({
                "symbol": full_symbol,
                "instrument": full_symbol,
                # `underlying` lets the frontend match positions by token name
                # when the symbol's funding-interval suffix has drifted (e.g.
                # bot config has "P-XRP-USDC-28800" but Variational position
                # object still carries "P-XRP-USDC-3600" from open-time).
                "underlying": underlying,
                "size": abs(size),
                "side": side_val,
                "entry_price": entry_price,
                "mark_price": mark_price,
                "unrealized_pnl": float(pos.get("upnl", 0)),
                "realized_pnl": float(pos.get("rpnl", 0)),
                "cumulative_funding": float(pos.get("cum_funding", 0)),
                "est_liquidation_price": est_liq,
                "value": value_usd,
            })
        return result

    def get_account_summary(self) -> dict:
        """Fetch account summary (equity + positions) from Variational."""
        equity = "0"
        available = "0"
        unrealized_pnl = "0"
        # Primary: /api/portfolio returns {balance, upnl, margin_usage}
        try:
            portfolio = self._sync_get(f"{self._base_url}/portfolio")
            if isinstance(portfolio, dict) and portfolio.get("balance"):
                equity = str(portfolio["balance"])
                unrealized_pnl = str(portfolio.get("upnl", "0"))
                margin = portfolio.get("margin_usage", {})
                initial = float(margin.get("initial_margin", 0)) if isinstance(margin, dict) else 0
                available = str(float(equity) - initial)
                logger.debug("Variational portfolio: balance=%s upnl=%s", equity, unrealized_pnl)
        except Exception as exc:
            logger.warning("Variational /portfolio error, trying /settlement_pools: %s", exc)
            # Fallback: /settlement_pools
            try:
                pools = self._sync_get(f"{self._base_url}/settlement_pools")
                if isinstance(pools, list) and pools:
                    pool = pools[0]
                    equity = str(pool.get("equity", pool.get("total_equity", "0")))
                    available = str(pool.get("available", pool.get("free_collateral", "0")))
                    unrealized_pnl = str(pool.get("unrealized_pnl", pool.get("upnl", "0")))
                elif isinstance(pools, dict):
                    equity = str(pools.get("equity", pools.get("total_equity", "0")))
                    available = str(pools.get("available", pools.get("free_collateral", "0")))
                    unrealized_pnl = str(pools.get("unrealized_pnl", pools.get("upnl", "0")))
            except Exception as exc2:
                logger.warning("Variational get_account_summary equity error: %s", exc2)

        positions = self.fetch_positions()
        return {
            "total_equity": equity,
            "available_balance": available,
            "unrealized_pnl": unrealized_pnl,
            "positions": positions,
        }

    def get_min_order_size(self, symbol: str) -> Decimal:
        return Decimal("1")

    # ── WebSocket subscriptions (no-op — Variational has no WS) ───────

    async def async_subscribe_fills(self, symbol: str, callback) -> None:
        """No-op — Variational has no fill WebSocket. Fill detection uses REST polling."""
        logger.info("Variational: fill WS not available — using REST polling for %s", symbol)
        await asyncio.sleep(1e9)

    async def async_subscribe_funding_rate(self, symbol: str, callback) -> None:
        """No-op — Variational has no funding rate WebSocket."""
        logger.info("Variational: funding rate WS not available — using REST polling for %s", symbol)
        await asyncio.sleep(1e9)

    # ══════════════════════════════════════════════════════════════════
    # Journal — history fetching for Trading Journal / PnL tracking
    # ══════════════════════════════════════════════════════════════════

    async def async_fetch_order_history(
        self, since_ms: int | None = None, limit: int = 500,
    ) -> list[dict]:
        """Fetch order history from Variational.

        Endpoint: GET /api/orders/v2?limit=&offset=&order_by=created_at&order=desc
        """
        all_orders: list[dict] = []
        offset = 0
        page_size = min(limit, 100)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        params_base: dict = {
            "order_by": "created_at",
            "order": "desc",
            "limit": page_size,
            "created_at_lte": now_str,
        }
        if since_ms:
            params_base["created_at_gte"] = self._ms_to_variational_date(since_ms)
        else:
            params_base["created_at_gte"] = "2020-01-01T00:00:00.000Z"

        while True:
            params = {**params_base, "offset": offset}
            try:
                data = await self._async_get(f"{self._base_url}/orders/v2", params=params)
            except Exception as exc:
                logger.warning("Variational order history error: %s", exc)
                break

            if isinstance(data, list):
                orders = data
            elif isinstance(data, dict):
                orders = data.get("result", data.get("data", data.get("orders", [])))
            else:
                orders = []
            if not orders:
                if offset == 0:
                    logger.info("Variational order history: empty first page, raw type=%s keys=%s",
                                type(data).__name__, list(data.keys()) if isinstance(data, dict) else "n/a")
                break

            for o in orders:
                created_str = o.get("created_at", "")
                created_ms = self._parse_iso_to_ms(created_str)

                raw_inst = o.get("reference_instrument", o.get("instrument_id", o.get("instrument", "")))
                instr, token = self._normalize_instrument(raw_inst)
                status = self._normalize_order_status(o.get("status", ""))
                side_raw = o.get("side", o.get("direction", ""))
                side = "BUY" if side_raw and side_raw.lower() in ("buy", "long") else "SELL"

                qty = float(o.get("size") or o.get("quantity") or o.get("qty") or 0)
                price = float(o.get("price") or o.get("limit_price") or 0)
                filled = float(o.get("filled_size") or o.get("filled_qty") or (qty if status == "FILLED" else 0))

                all_orders.append({
                    "exchange_order_id": str(o.get("id") or o.get("rfqid") or o.get("order_id") or ""),
                    "exchange": "variational",
                    "instrument": instr,
                    "token": token,
                    "side": side,
                    "order_type": (o.get("type") or o.get("order_type") or "MARKET").upper(),
                    "status": status,
                    "price": price,
                    "average_price": float(o.get("average_price") or o.get("avg_price") or price),
                    "qty": qty,
                    "filled_qty": filled,
                    "fee": float(o.get("fee") or 0),
                    "reduce_only": 1 if o.get("is_reduce_only") else 0,
                    "post_only": 0,
                    "created_at": created_ms,
                    "updated_at": self._parse_iso_to_ms(o.get("updated_at", created_str)),
                })

            if len(orders) < page_size:
                break
            offset += page_size
            if len(all_orders) >= limit:
                break

        if all_orders:
            unique_statuses = set(o["status"] for o in all_orders)
            logger.info("Variational order history: fetched %d orders, statuses=%s", len(all_orders), unique_statuses)
        else:
            logger.info("Variational order history: fetched 0 orders")
        return all_orders

    async def async_fetch_trade_history(
        self, since_ms: int | None = None, limit: int = 500,
    ) -> list[dict]:
        """Fetch trade history from Variational.

        Endpoint: GET /api/trades?limit=&offset=&order_by=created_at&order=desc
        """
        all_fills: list[dict] = []
        offset = 0
        page_size = min(limit, 100)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        params_base: dict = {
            "order_by": "created_at",
            "order": "desc",
            "limit": page_size,
            "created_at_lte": now_str,
        }
        if since_ms:
            params_base["created_at_gte"] = self._ms_to_variational_date(since_ms)
        else:
            params_base["created_at_gte"] = "2020-01-01T00:00:00.000Z"

        while True:
            params = {**params_base, "offset": offset}
            try:
                data = await self._async_get(f"{self._base_url}/trades", params=params)
            except Exception as exc:
                logger.warning("Variational trade history error: %s", exc)
                break

            if isinstance(data, list):
                trades = data
            elif isinstance(data, dict):
                trades = data.get("result", data.get("data", data.get("trades", [])))
            else:
                trades = []
            if not trades:
                if offset == 0:
                    logger.info("Variational trade history: empty first page, raw type=%s keys=%s",
                                type(data).__name__, list(data.keys()) if isinstance(data, dict) else "n/a")
                break

            for t in trades:
                created_str = t.get("created_at", "")
                created_ms = self._parse_iso_to_ms(created_str)

                raw_inst = t.get("reference_instrument", t.get("instrument_id", t.get("instrument", "")))
                instr, token = self._normalize_instrument(raw_inst)
                side_raw = t.get("side", t.get("direction", ""))
                side = "BUY" if side_raw and side_raw.lower() in ("buy", "long") else "SELL"

                qty = float(t.get("size") or t.get("quantity") or t.get("qty") or 0)
                price = float(t.get("price") or 0)

                all_fills.append({
                    "exchange_fill_id": str(t.get("id") or t.get("trade_id") or ""),
                    "exchange_order_id": str(t.get("order_id") or t.get("rfqid") or ""),
                    "exchange": "variational",
                    "instrument": instr,
                    "token": token,
                    "side": side,
                    "price": price,
                    "qty": qty,
                    "value": qty * price,
                    "fee": float(t.get("fee", 0)),
                    "is_taker": 1,
                    "trade_type": "TRADE",
                    "created_at": created_ms,
                })

            if len(trades) < page_size:
                break
            offset += page_size
            if len(all_fills) >= limit:
                break

        logger.info("Variational trade history: fetched %d fills", len(all_fills))
        return all_fills

    async def async_fetch_funding_payments(
        self, since_ms: int | None = None, limit: int = 500,
    ) -> list[dict]:
        """Fetch funding payments from Variational.

        Endpoint: GET /api/transfers?type=funding&limit=&offset=&order_by=created_at&order=desc
        """
        all_payments: list[dict] = []
        offset = 0
        page_size = min(limit, 100)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        params_base: dict = {
            "type": "funding",
            "order_by": "created_at",
            "order": "desc",
            "limit": page_size,
            "created_at_lte": now_str,
        }
        if since_ms:
            params_base["created_at_gte"] = self._ms_to_variational_date(since_ms)
        else:
            params_base["created_at_gte"] = "2020-01-01T00:00:00.000Z"

        while True:
            params = {**params_base, "offset": offset}
            try:
                data = await self._async_get(f"{self._base_url}/transfers", params=params)
            except Exception as exc:
                logger.warning("Variational funding payments error: %s", exc)
                break

            if isinstance(data, list):
                transfers = data
            elif isinstance(data, dict):
                transfers = data.get("result", data.get("data", data.get("transfers", [])))
            else:
                transfers = []
            if not transfers:
                if offset == 0:
                    logger.info("Variational funding: empty first page, raw type=%s keys=%s",
                                type(data).__name__, list(data.keys()) if isinstance(data, dict) else "n/a")
                break

            for f in transfers:
                created_str = f.get("created_at", "")
                created_ms = self._parse_iso_to_ms(created_str)

                raw_inst = f.get("reference_instrument", f.get("instrument_id", f.get("instrument", "")))
                instr, token = self._normalize_instrument(raw_inst)
                amount = float(f.get("qty") or f.get("amount") or f.get("value") or 0)

                all_payments.append({
                    "exchange_payment_id": str(f.get("id", f.get("transfer_id", f"var-fund-{created_ms}"))),
                    "exchange": "variational",
                    "instrument": instr,
                    "token": token,
                    "side": "",
                    "size": 0.0,
                    "funding_fee": amount,
                    "funding_rate": 0.0,
                    "mark_price": 0.0,
                    "paid_at": created_ms,
                })

            if len(transfers) < page_size:
                break
            offset += page_size
            if len(all_payments) >= limit:
                break

        logger.info("Variational funding payments: fetched %d records", len(all_payments))
        return all_payments

    async def async_fetch_realized_pnl(
        self, since_ms: int | None = None, limit: int = 500,
    ) -> list[dict]:
        """Fetch realized PnL transfers from Variational.

        Endpoint: GET /api/transfers?type=realized_pnl
        """
        all_pnl: list[dict] = []
        offset = 0
        page_size = min(limit, 100)
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        params_base: dict = {
            "type": "realized_pnl",
            "order_by": "created_at",
            "order": "desc",
            "limit": page_size,
            "created_at_lte": now_str,
        }
        if since_ms:
            params_base["created_at_gte"] = self._ms_to_variational_date(since_ms)
        else:
            params_base["created_at_gte"] = "2020-01-01T00:00:00.000Z"

        while True:
            params = {**params_base, "offset": offset}
            try:
                data = await self._async_get(f"{self._base_url}/transfers", params=params)
            except Exception as exc:
                logger.warning("Variational realized PnL error: %s", exc)
                break

            if isinstance(data, list):
                transfers = data
            elif isinstance(data, dict):
                transfers = data.get("result", data.get("data", data.get("transfers", [])))
            else:
                transfers = []
            if not transfers:
                if offset == 0:
                    logger.info("Variational realized PnL: empty first page, raw type=%s keys=%s",
                                type(data).__name__, list(data.keys()) if isinstance(data, dict) else "n/a")
                break

            for t in transfers:
                raw_inst = t.get("reference_instrument", t.get("instrument_id", t.get("instrument", "")))
                instr, token = self._normalize_instrument(raw_inst)
                all_pnl.append({
                    "id": str(t.get("id", "")),
                    "instrument": instr,
                    "token": token,
                    "amount": float(t.get("qty") or t.get("amount") or t.get("value") or 0),
                    "created_at": self._parse_iso_to_ms(t.get("created_at", "")),
                })

            if len(transfers) < page_size:
                break
            offset += page_size
            if len(all_pnl) >= limit:
                break

        logger.info("Variational realized PnL: fetched %d records", len(all_pnl))
        return all_pnl

    @staticmethod
    def _parse_iso_to_ms(iso_str: str) -> int:
        """Parse ISO-8601 string to ms timestamp."""
        if not iso_str:
            return 0
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1000)
        except Exception:
            return 0

    # Map Variational order statuses to standard ones accepted by JournalCollector
    _STATUS_MAP: dict[str, str] = {
        "FILLED": "FILLED",
        "PARTIALLY_FILLED": "PARTIALLY_FILLED",
        "PARTIAL_FILL": "PARTIAL_FILL",
        # Variational-specific statuses
        "SETTLED": "FILLED",
        "MATCHED": "FILLED",
        "CLEARED": "FILLED",
        "COMPLETE": "FILLED",
        "COMPLETED": "FILLED",
        "EXECUTED": "FILLED",
        "DONE": "FILLED",
        "CLOSED": "FILLED",
        "TRADE": "FILLED",
        # Partial
        "PARTIAL": "PARTIALLY_FILLED",
        "PARTIALLY_MATCHED": "PARTIALLY_FILLED",
        # Keep these as-is (they'll be filtered out downstream)
        "CANCELLED": "CANCELLED",
        "CANCELED": "CANCELLED",
        "REJECTED": "REJECTED",
        "EXPIRED": "EXPIRED",
        "OPEN": "OPEN",
        "PENDING": "PENDING",
        "NEW": "NEW",
    }

    @classmethod
    def _normalize_order_status(cls, raw_status: str) -> str:
        """Normalize Variational order status to standard form."""
        upper = (raw_status or "").upper().strip()
        mapped = cls._STATUS_MAP.get(upper)
        if mapped:
            return mapped
        # Unknown status — log and return as-is
        if upper:
            logger.debug("Variational unknown order status: '%s'", upper)
        return upper

    @staticmethod
    def _ms_to_variational_date(ms: int) -> str:
        """Convert ms timestamp to Variational-compatible ISO date string."""
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    @staticmethod
    def _normalize_instrument(raw) -> tuple[str, str]:
        """Normalize a Variational instrument (dict or string) to (symbol, token).

        Returns (full_symbol like 'P-SUI-USDC-3600', token like 'SUI').
        """
        if isinstance(raw, dict):
            underlying = raw.get("underlying", "")
            settle = raw.get("settlement_asset", "USDC")
            fi = raw.get("funding_interval_s", 3600)
            if underlying:
                symbol = f"P-{underlying}-{settle}-{fi}"
                return symbol, underlying
            return "", ""
        # String form
        raw_str = str(raw) if raw else ""
        if not raw_str:
            return "", ""
        if raw_str.startswith("P-"):
            parts = raw_str.split("-")
            token = parts[1] if len(parts) >= 2 else raw_str
            return raw_str, token
        parts = raw_str.replace("_", "-").split("-")
        token = parts[0] if parts else raw_str
        return raw_str, token

    @staticmethod
    def _extract_token_variational(instrument: str) -> str:
        """Extract token from Variational instrument (e.g. 'P-SUI-USDC-3600' -> 'SUI')."""
        if not instrument:
            return ""
        if instrument.startswith("P-"):
            parts = instrument.split("-")
            return parts[1] if len(parts) >= 2 else instrument
        parts = instrument.replace("_", "-").split("-")
        return parts[0] if parts else instrument

    # ── Points ─────────────────────────────────────────────────────────

    async def async_fetch_points(self) -> list[dict]:
        """Fetch earned points from Variational.

        Endpoint: GET /api/points/summary
        """
        try:
            data = await self._async_get(f"{self._base_url}/points/summary")
        except Exception as exc:
            logger.warning("Variational points fetch error: %s", exc)
            return []

        result: list[dict] = []

        # Handle both list and dict response formats
        entries = data if isinstance(data, list) else data.get("data", data.get("points", [data] if isinstance(data, dict) else []))

        for entry in entries:
            # Variational may return season/epoch structure or flat summary
            if "seasons" in entry or "epochRewards" in entry:
                # Nested season structure (similar to Extended)
                season_id = entry.get("seasonId", entry.get("season_id", 0))
                for epoch in entry.get("epochRewards", entry.get("epochs", [])):
                    result.append({
                        "exchange": "variational",
                        "season_id": season_id,
                        "epoch_id": epoch.get("epochId", epoch.get("epoch_id", 0)),
                        "start_date": epoch.get("startDate", epoch.get("start_date", "")),
                        "end_date": epoch.get("endDate", epoch.get("end_date", "")),
                        "points": float(epoch.get("pointsReward", epoch.get("points", 0))),
                    })
            else:
                # Flat summary entry
                result.append({
                    "exchange": "variational",
                    "season_id": entry.get("season_id", entry.get("seasonId", 0)),
                    "epoch_id": entry.get("epoch_id", entry.get("epochId", 0)),
                    "start_date": entry.get("start_date", entry.get("startDate", "")),
                    "end_date": entry.get("end_date", entry.get("endDate", "")),
                    "points": float(entry.get("points", entry.get("pointsReward", entry.get("total_points", 0)))),
                })

        logger.info("Variational points: fetched %d records", len(result))
        return result

    # ── Leverage ───────────────────────────────────────────────────────

    async def async_set_leverage(self, symbol: str, leverage: int) -> bool:
        """Set leverage via /settlement_pools/leverage (best-effort)."""
        try:
            resp = await self._async_post("settlement_pools/leverage", {
                "leverage": leverage,
            })
            logger.info("Variational leverage set: %s -> %dx, resp=%s", symbol, leverage, str(resp)[:200])
            return True
        except Exception as exc:
            logger.warning("Variational set_leverage error (non-fatal): %s", exc)
            return False
