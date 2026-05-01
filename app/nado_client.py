"""NADO Exchange client — direct REST + EIP-712 signing for nado.xyz.

Public data (orderbook, markets): REST queries against the gateway.
Trading (orders, positions): REST execute endpoint with EIP-712 signed orders
using eth_account (compatible with the project's existing eth-account>=0.13).
Conforms to the ExchangeClient protocol defined in app/exchange.py.

The official nado-protocol SDK is NOT used because it pins pydantic<2 and
eth-account<0.9, which conflict with grvt-pysdk and FastAPI.
"""

import logging
import random
import time
from decimal import Decimal
from typing import Literal

import requests
import urllib3
from eth_account import Account
from eth_account.messages import encode_typed_data

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("tradeautonom.nado_client")

_X18 = Decimal("1000000000000000000")  # 1e18


def _from_x18(value: str | int) -> Decimal:
    """Convert an x18-encoded string/int to a human-readable Decimal."""
    return Decimal(str(value)) / _X18


def _to_x18(value: Decimal | float) -> str:
    """Convert a human-readable value to x18 string for the NADO API."""
    return str(int(Decimal(str(value)) * _X18))


# ---------------------------------------------------------------------------
# EIP-712 helpers
# ---------------------------------------------------------------------------

def _build_sender_bytes32(address: str, subaccount_name: str) -> str:
    """Build the NADO sender bytes32: 20-byte address + 12-byte subaccount name."""
    addr_bytes = bytes.fromhex(address[2:])  # strip 0x
    name_bytes = subaccount_name.encode("utf-8")[:12].ljust(12, b"\x00")
    return "0x" + (addr_bytes + name_bytes).hex()


def _gen_order_verifying_contract(product_id: int) -> str:
    """For PlaceOrder, verifying contract = address(product_id)."""
    be_bytes = product_id.to_bytes(20, byteorder="big", signed=False)
    return "0x" + be_bytes.hex()


def _gen_order_nonce(recv_window_ms: int = 10000) -> int:
    """Generate nonce: MSB 44 bits = recv_time (ms), LSB 20 bits = random."""
    recv_time_ms = int(time.time() * 1000) + recv_window_ms
    rand_bits = random.getrandbits(20)
    return (recv_time_ms << 20) | rand_bits


def _build_ioc_appendix() -> int:
    """Build appendix for IOC order: version=1, order_type=IOC(1), rest=0."""
    # Bits: [version 8b=1][isolated 1b=0][order_type 2b=1(IOC)][rest=0]
    version = 1           # bits 0-7
    isolated = 0          # bit 8
    order_type = 1        # bits 9-10 (IOC=1)
    return version | (isolated << 8) | (order_type << 9)


def _build_fok_appendix() -> int:
    """Build appendix for FOK (Fill or Kill) order: version=1, order_type=FOK(2)."""
    version = 1           # bits 0-7
    isolated = 0          # bit 8
    order_type = 2        # bits 9-10 (FOK=2)
    return version | (isolated << 8) | (order_type << 9)


def _build_reduce_only_ioc_appendix() -> int:
    """Build appendix for reduce-only IOC order."""
    version = 1
    isolated = 0
    order_type = 1        # IOC
    reduce_only = 1       # bit 11
    return version | (isolated << 8) | (order_type << 9) | (reduce_only << 11)


def _sign_order(
    private_key: str,
    chain_id: int,
    verifying_contract: str,
    sender: str,
    price_x18: str,
    amount_x18: str,
    expiration: str,
    nonce: int,
    appendix: int,
) -> str:
    """Sign an Order struct using EIP-712 and return the hex signature."""
    full_message = {
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": [
                {"name": "sender", "type": "bytes32"},
                {"name": "priceX18", "type": "int128"},
                {"name": "amount", "type": "int128"},
                {"name": "expiration", "type": "uint64"},
                {"name": "nonce", "type": "uint64"},
                {"name": "appendix", "type": "uint128"},
            ],
        },
        "primaryType": "Order",
        "domain": {
            "name": "Nado",
            "version": "0.0.1",
            "chainId": chain_id,
            "verifyingContract": verifying_contract,
        },
        "message": {
            "sender": bytes.fromhex(sender[2:]),
            "priceX18": int(price_x18),
            "amount": int(amount_x18),
            "expiration": int(expiration),
            "nonce": nonce,
            "appendix": appendix,
        },
    }

    signable = encode_typed_data(full_message=full_message)
    signed = Account.sign_message(signable, private_key)
    return signed.signature.hex()


class NadoClient:
    """NADO Exchange client with public data + perp trading support."""

    def __init__(
        self,
        private_key: str = "",
        subaccount_name: str = "default",
        env: str = "mainnet",
        linked_signer_key: str = "",
        wallet_address: str = "",
    ) -> None:
        self._private_key = private_key
        self._linked_signer_key = linked_signer_key
        self._wallet_address = wallet_address
        self._subaccount_name = subaccount_name
        self._env = env
        self._has_credentials = bool(private_key or linked_signer_key)

        # Caches populated from /symbols endpoint
        self._symbol_to_product_id: dict[str, int] = {}
        self._product_id_to_symbol: dict[int, str] = {}
        self._tick_size_cache: dict[str, Decimal] = {}    # symbol -> price_increment
        self._min_size_cache: dict[str, Decimal] = {}     # symbol -> min qty (base)
        self._min_notional_cache: dict[str, Decimal] = {}  # symbol -> min notional (USD)
        self._size_increment_cache: dict[str, Decimal] = {}  # symbol -> size_increment

        # Shared fill WS state — one connection per client, multiple symbol callbacks
        self._fill_callbacks: list[tuple[str, object]] = []
        self._fill_ws_task: object = None
        self._product_type_cache: dict[str, str] = {}     # symbol -> "perp" or "spot"
        self._order_product_cache: dict[str, int] = {}     # digest -> product_id

        # REST endpoints
        if env == "mainnet":
            self._gateway_rest = "https://gateway.prod.nado.xyz/v1"
        else:
            self._gateway_rest = "https://gateway.sepolia.nado.xyz/v1"

        # REST session (SSL verify disabled — same pattern as Extended/GRVT)
        self._session = requests.Session()
        self._session.verify = False
        self._session.headers.update({"Content-Type": "application/json"})

        # Signer address + sender bytes32
        self._signer_address: str | None = None
        self._sender_hex: str | None = None
        self._chain_id: int | None = None
        self._endpoint_addr: str = ""

        # Pending link-signer state (used during the 2-step wallet-sign flow)
        self._pending_link: dict | None = None

        if self._has_credentials:
            self._init_signer()
            logger.info("NadoClient initialised WITH trading (env=%s)", env)
        else:
            logger.info("NadoClient initialised READ-ONLY (env=%s)", env)

        # Pre-load symbols + chain info
        try:
            self._load_symbols()
        except Exception as exc:
            logger.warning("Failed to pre-load NADO symbols: %s", exc)
        if self._has_credentials:
            try:
                self._load_contracts()
            except Exception as exc:
                logger.warning("Failed to pre-load NADO contracts: %s", exc)

    def _init_signer(self) -> None:
        """Derive signer address from private key and build sender bytes32.

        If a linked signer key is set, that is used for signing trades
        (typical in the MetaMask/browser-wallet flow where the main private key
        is not available to the bot).  If only the main wallet private_key is
        available, it is used instead — it always works on Nado regardless of
        linked signer state.
        The sender bytes32 always uses the main wallet address (or explicit wallet_address).
        """
        # Determine the signing key
        # Prefer private_key (main wallet — always valid on Nado) when available;
        # fall back to linked_signer_key (typical when wallet is MetaMask-only).
        self._trading_key = self._private_key or self._linked_signer_key

        # Determine the wallet address for sender bytes32
        if self._wallet_address:
            wallet_addr = self._wallet_address
        elif self._private_key:
            wallet_addr = Account.from_key(self._private_key).address
        elif self._linked_signer_key:
            wallet_addr = Account.from_key(self._linked_signer_key).address
        else:
            raise RuntimeError("NADO: no key available to derive wallet address")

        self._signer_address = wallet_addr
        self._sender_hex = _build_sender_bytes32(wallet_addr, self._subaccount_name)
        trading_addr = Account.from_key(self._trading_key).address
        signing_mode = "wallet-key" if self._trading_key == self._private_key else "linked-signer"
        logger.info("NADO wallet: %s signing: %s (%s) sender: %s...", wallet_addr, trading_addr, signing_mode, self._sender_hex[:20])

    def _load_contracts(self) -> None:
        """Fetch chain_id and endpoint_addr from NADO contracts query."""
        url = f"{self._gateway_rest}/query"
        payload = {"type": "contracts"}
        resp = self._session.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            self._chain_id = int(data["data"]["chain_id"])
            self._endpoint_addr = data["data"].get("endpoint_addr", "")
            logger.info("NADO chain_id=%d endpoint=%s", self._chain_id, self._endpoint_addr)
        else:
            raise RuntimeError(f"NADO contracts query failed: {data}")

    def _require_trading(self) -> None:
        if not self._has_credentials:
            raise RuntimeError(
                "NADO trading not available — set NADO_PRIVATE_KEY in .env"
            )
        if self._chain_id is None:
            self._load_contracts()

    # -- Protocol ---------------------------------------------------------

    @property
    def name(self) -> str:
        return "nado"

    @property
    def can_trade(self) -> bool:
        return self._has_credentials

    # -- Symbol / Market data ---------------------------------------------

    def _load_symbols(self) -> None:
        """Fetch /symbols and populate caches."""
        url = f"{self._gateway_rest}/symbols"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        symbols = resp.json()

        for s in symbols:
            symbol = s["symbol"]
            product_id = s["product_id"]
            ptype = s.get("type", "")

            self._symbol_to_product_id[symbol] = product_id
            self._product_id_to_symbol[product_id] = symbol
            self._product_type_cache[symbol] = ptype

            tick_x18 = s.get("price_increment_x18", "0")
            if tick_x18 and tick_x18 != "0":
                self._tick_size_cache[symbol] = _from_x18(tick_x18)

            size_inc = s.get("size_increment", "0")
            if size_inc and size_inc != "0":
                self._size_increment_cache[symbol] = _from_x18(size_inc)

            # NADO min_size is USD notional (not base qty)
            min_size = s.get("min_size", "0")
            if min_size and min_size != "0":
                self._min_notional_cache[symbol] = _from_x18(min_size)

        logger.info("NADO symbols loaded: %d products", len(symbols))

    def _get_product_id(self, symbol: str) -> int:
        """Resolve a symbol string to NADO product_id."""
        if symbol not in self._symbol_to_product_id:
            self._load_symbols()
        pid = self._symbol_to_product_id.get(symbol)
        if pid is None:
            raise ValueError(f"Unknown NADO symbol: {symbol}")
        return pid

    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        """Fetch orderbook via market_liquidity query, normalised to [[price, qty], ...]."""
        product_id = self._get_product_id(symbol)
        depth = min(limit, 100)

        url = f"{self._gateway_rest}/query"
        payload = {"type": "market_liquidity", "product_id": product_id, "depth": depth}
        resp = self._session.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "success":
            raise RuntimeError(f"NADO orderbook error for {symbol}: {data.get('error', str(data))}")

        book = data.get("data", {})
        bids = []
        for price_x18, size_x18 in book.get("bids", []):
            bids.append([str(_from_x18(price_x18)), str(_from_x18(size_x18))])
        asks = []
        for price_x18, size_x18 in book.get("asks", []):
            asks.append([str(_from_x18(price_x18)), str(_from_x18(size_x18))])

        return {"bids": bids, "asks": asks}

    @staticmethod
    def _extract_asset(symbol: str) -> str:
        """Extract base asset from NADO symbol: 'BTC-PERP' -> 'BTC', 'KBTC' -> 'KBTC'."""
        for suffix in ("-PERP", "-SPOT"):
            if symbol.upper().endswith(suffix):
                return symbol[: -len(suffix)].upper()
        return symbol.upper()

    def fetch_markets(self) -> list[dict]:
        """Return list of available markets with normalised keys."""
        if not self._symbol_to_product_id:
            self._load_symbols()

        markets = []
        for symbol, product_id in self._symbol_to_product_id.items():
            ptype = self._product_type_cache.get(symbol, "")
            tick = self._tick_size_cache.get(symbol)
            min_size = self._min_size_cache.get(symbol)
            markets.append({
                "symbol": symbol,
                "name": symbol,
                "asset": self._extract_asset(symbol),
                "product_id": product_id,
                "type": ptype,
                "tick_size": str(tick) if tick else None,
                "min_size": str(min_size) if min_size else None,
            })
        return markets

    def get_min_order_size(self, symbol: str) -> Decimal:
        """Return the minimum order size in base qty.

        TEMPORARY: the notional→qty conversion produces wrong values in
        some cases (rejects valid orders). Returning the raw size_increment
        (the true base-qty tick) disables the notional-based floor check
        downstream. Exchange-side validation still rejects orders below
        the real minimum if one is ever sent — this just prevents us from
        pre-emptively blocking them bot-side.

        To re-enable the notional-based computation, restore the block:
            notional = self._min_notional_cache.get(symbol, Decimal("0"))
            if notional > 0:
                book = self.fetch_order_book(symbol, limit=1)
                ... (mid-price conversion) ...
        """
        if symbol not in self._size_increment_cache:
            self._load_symbols()
        return self._size_increment_cache.get(symbol, Decimal("0"))

    def get_tick_size(self, symbol: str) -> Decimal:
        """Public accessor for tick size."""
        if symbol not in self._tick_size_cache:
            self._load_symbols()
        return self._tick_size_cache.get(symbol, Decimal("0.01"))

    def get_qty_step(self, symbol: str) -> Decimal:
        """Return the quantity step size for a symbol."""
        if symbol not in self._size_increment_cache:
            self._load_symbols()
        return self._size_increment_cache.get(symbol, Decimal("0.01"))

    def _round_qty(self, amount: Decimal, symbol: str) -> Decimal:
        """Round quantity down to the instrument's size increment."""
        step = self.get_qty_step(symbol)
        if step and step > 0:
            return (amount / step).to_integral_value(rounding="ROUND_DOWN") * step
        return amount

    # -- Trading ----------------------------------------------------------

    def create_market_order(
        self,
        symbol: str,
        side: str,
        amount: Decimal,
        slippage_pct: float | None = None,
        **kwargs,
    ) -> dict:
        """Market-order via FOK with depth-aware aggressive pricing.

        Replaces the previous "best ± 10 ticks" fudge (unit-inappropriate
        across price scales; triggered code=2031 on thin books) with:
          1. Walk a 20-level book to find the marginal price needed to
             consume `amount`. Raise early if depth is insufficient.
          2. Use a percentage-of-mid band (default 0.5% via caller's
             `slippage_pct`, falls back to 0.5% literal if omitted) as
             the *upper bound* on the FOK limit. If the walked price
             exceeds that band, raise rather than overpay.
        """
        self._require_trading()
        amount = self._round_qty(amount, symbol)
        product_id = self._get_product_id(symbol)
        tick = self.get_tick_size(symbol)

        pct = Decimal(str(slippage_pct)) if slippage_pct is not None else Decimal("0.5")
        band_factor = pct / Decimal("100")

        # Fetch deeper book and walk it to confirm depth + compute fair limit
        book = self.fetch_order_book(symbol, limit=20)
        levels_key = "asks" if side == "buy" else "bids"
        levels = book.get(levels_key, [])
        if not levels:
            raise RuntimeError(f"No {levels_key} in {symbol} orderbook")

        best = Decimal(str(levels[0][0]))

        # Walk levels until cumulative size >= amount; track worst price
        cum_size = Decimal("0")
        worst = best
        consumed_levels = 0
        for price_str, size_str in levels:
            price = Decimal(str(price_str))
            size = Decimal(str(size_str))
            cum_size += size
            worst = price
            consumed_levels += 1
            if cum_size >= amount:
                break

        if cum_size < amount:
            raise RuntimeError(
                f"NADO: insufficient depth on {symbol} {side} "
                f"(need {amount}, have {cum_size} across {consumed_levels} levels)"
            )

        # Compute the percentage-band cap relative to best
        if side == "buy":
            band_cap = best * (Decimal("1") + band_factor)
            # If the walked worst price exceeds our slippage cap, abort:
            # the FOK would either overpay or fail. Either way: skip.
            if worst > band_cap:
                raise RuntimeError(
                    f"NADO: insufficient depth on {symbol} buy within "
                    f"{pct}% slippage (best={best}, worst_walked={worst}, "
                    f"cap={band_cap})"
                )
            # Use the looser of walked-worst and cap, then add a tiny
            # safety pad and round UP to tick.
            raw = max(worst, best) * (Decimal("1") + band_factor)
            final_limit = (raw / tick).to_integral_value(rounding="ROUND_UP") * tick
        else:
            band_cap = best * (Decimal("1") - band_factor)
            if worst < band_cap:
                raise RuntimeError(
                    f"NADO: insufficient depth on {symbol} sell within "
                    f"{pct}% slippage (best={best}, worst_walked={worst}, "
                    f"cap={band_cap})"
                )
            raw = min(worst, best) * (Decimal("1") - band_factor)
            final_limit = (raw / tick).to_integral_value(rounding="ROUND_DOWN") * tick

        logger.info(
            "NADO market (FOK) walk: %s %s qty=%s best=%s worst=%s "
            "limit=%s slippage_pct=%s depth_levels=%d cum_size=%s",
            side.upper(), symbol, amount, best, worst,
            final_limit, pct, consumed_levels, cum_size,
        )

        order_amount = amount if side == "buy" else -amount
        price_x18 = _to_x18(final_limit)
        amount_x18 = _to_x18(order_amount)
        expiration = str(int(time.time()) + 60)
        nonce = _gen_order_nonce()
        appendix = _build_fok_appendix()

        verifying_contract = _gen_order_verifying_contract(product_id)
        signature = _sign_order(
            private_key=self._trading_key,
            chain_id=self._chain_id,
            verifying_contract=verifying_contract,
            sender=self._sender_hex,
            price_x18=price_x18,
            amount_x18=amount_x18,
            expiration=expiration,
            nonce=nonce,
            appendix=appendix,
        )

        ptype = self._product_type_cache.get(symbol, "")
        payload = {
            "place_order": {
                "product_id": product_id,
                "order": {
                    "sender": self._sender_hex,
                    "priceX18": price_x18,
                    "amount": amount_x18,
                    "expiration": expiration,
                    "nonce": str(nonce),
                    "appendix": str(appendix),
                },
                "signature": "0x" + signature,
            }
        }
        if ptype == "spot":
            payload["place_order"]["spot_leverage"] = True

        url = f"{self._gateway_rest}/execute"
        resp = self._session.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        resp_data = resp.json()

        status = resp_data.get("status", "unknown")
        digest = None
        if isinstance(resp_data.get("data"), dict):
            digest = resp_data["data"].get("digest")

        logger.info(
            "NADO FOK order placed: %s %s qty=%s limit=%s -> status=%s digest=%s",
            side.upper(), symbol, amount, final_limit, status, digest,
        )

        if status == "failure":
            error_msg = resp_data.get("error", "Unknown error")
            error_code = resp_data.get("error_code", "")
            raise RuntimeError(f"NADO order failed: {error_msg} (code={error_code})")

        # Verify actual fill by querying the order digest
        actual_traded_qty = float(amount)  # default: assume full fill
        if digest:
            self._order_product_cache[digest] = product_id
            time.sleep(0.5)
            try:
                query_url = f"{self._gateway_rest}/query"
                q_payload = {"type": "order", "digest": digest, "subaccount": self._sender_hex, "product_id": product_id}
                q_resp = self._session.post(query_url, json=q_payload, timeout=5)
                q_data = q_resp.json()
                if q_data.get("status") == "success":
                    order_info = q_data.get("data", {})
                    filled_x18 = order_info.get("filled_amount", "0")
                    filled_qty = abs(float(_from_x18(filled_x18)))
                    if filled_qty > 0:
                        actual_traded_qty = filled_qty
                        logger.info("NADO FOK fill verified: %s %s requested=%s filled=%s",
                                    side.upper(), symbol, amount, filled_qty)
                    else:
                        logger.warning("NADO FOK fill query returned 0 — using requested qty %s", amount)
            except Exception as exc:
                logger.warning("NADO FOK fill verification failed: %s — using requested qty %s", exc, amount)

        return {
            "id": digest,
            "status": status,
            "fill_price": float(best),
            "limit_price": float(final_limit),
            "digest": digest,
            "traded_qty": actual_traded_qty,
        }

    def create_aggressive_limit_order(
        self,
        symbol: str,
        side: Literal["buy", "sell"],
        amount: Decimal,
        offset_ticks: int = 2,
        best_price: float | None = None,
        limit_price: float | None = None,
    ) -> dict:
        """Place an aggressive limit IOC order on NADO.

        If limit_price is provided (e.g. from VWAP), it is used directly.
        Otherwise falls back to: best price + offset_ticks * tick_size.
        Uses IOC (Immediate or Cancel) for instant fill-or-kill behavior.
        """
        self._require_trading()
        amount = self._round_qty(amount, symbol)
        product_id = self._get_product_id(symbol)
        tick = self.get_tick_size(symbol)

        if limit_price is not None:
            final_limit = Decimal(str(limit_price))
            if side == "buy":
                final_limit = (final_limit / tick).to_integral_value(rounding="ROUND_UP") * tick
            else:
                final_limit = (final_limit / tick).to_integral_value(rounding="ROUND_DOWN") * tick
            logger.info(
                "NADO VWAP limit: %s %s qty=%s limit=%s (VWAP-computed)",
                side.upper(), symbol, amount, final_limit,
            )
        else:
            if best_price is not None:
                best = Decimal(str(best_price))
            else:
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
                "NADO aggressive limit: %s %s qty=%s best=%s limit=%s offset=%d tick=%s",
                side.upper(), symbol, amount, best, final_limit, offset_ticks, tick,
            )

        # NADO: positive amount = buy, negative amount = sell
        order_amount = amount if side == "buy" else -amount
        price_x18 = _to_x18(final_limit)
        amount_x18 = _to_x18(order_amount)
        expiration = str(int(time.time()) + 60)  # 60 seconds
        nonce = _gen_order_nonce()
        appendix = _build_ioc_appendix()

        # EIP-712 sign
        verifying_contract = _gen_order_verifying_contract(product_id)
        signature = _sign_order(
            private_key=self._trading_key,
            chain_id=self._chain_id,
            verifying_contract=verifying_contract,
            sender=self._sender_hex,
            price_x18=price_x18,
            amount_x18=amount_x18,
            expiration=expiration,
            nonce=nonce,
            appendix=appendix,
        )

        # POST to execute endpoint
        ptype = self._product_type_cache.get(symbol, "")
        payload = {
            "place_order": {
                "product_id": product_id,
                "order": {
                    "sender": self._sender_hex,
                    "priceX18": price_x18,
                    "amount": amount_x18,
                    "expiration": expiration,
                    "nonce": str(nonce),
                    "appendix": str(appendix),
                },
                "signature": "0x" + signature,
            }
        }
        if ptype == "spot":
            payload["place_order"]["spot_leverage"] = True

        url = f"{self._gateway_rest}/execute"
        resp = self._session.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        resp_data = resp.json()

        status = resp_data.get("status", "unknown")
        digest = None
        if isinstance(resp_data.get("data"), dict):
            digest = resp_data["data"].get("digest")

        logger.info(
            "NADO IOC order placed: %s %s qty=%s limit=%s → status=%s digest=%s",
            side.upper(), symbol, amount, final_limit, status, digest,
        )

        if status == "failure":
            error_msg = resp_data.get("error", "Unknown error")
            error_code = resp_data.get("error_code", "")
            raise RuntimeError(f"NADO order failed: {error_msg} (code={error_code})")

        # IOC orders on Nado either fill immediately or are cancelled
        # Since status="success" means the order was accepted and matched,
        # the traded_qty equals the full amount
        return {
            "id": digest,
            "status": status,
            "traded_qty": float(amount),  # IOC = immediate full fill
            "limit_price": float(final_limit),
            "digest": digest,
        }

    def check_order_fill(self, order_id: str) -> dict:
        """Check if an order has been filled.

        For IOC orders on NADO, the order either fills immediately or is cancelled.
        A successful place_order response with status="success" means the order was
        accepted by the matching engine. IOC orders don't linger in the book.
        """
        # IOC orders are instant — success from place_order = matched.
        return {"filled": True, "status": "FILLED"}

    def fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        """Fetch open perp positions via subaccount_info query."""
        if not self._sender_hex:
            return []

        url = f"{self._gateway_rest}/query"
        payload = {"type": "subaccount_info", "subaccount": self._sender_hex}
        resp = self._session.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "success":
            raise RuntimeError(f"NADO subaccount_info error: {data.get('error', str(data))}")

        result = []
        sub_data = data.get("data", {})
        for pb in sub_data.get("perp_balances", []):
            product_id = pb.get("product_id", 0)
            symbol = self._product_id_to_symbol.get(product_id, f"product_{product_id}")

            if symbols and symbol not in symbols:
                continue

            amount_x18 = pb.get("balance", {}).get("amount", "0")
            size = float(_from_x18(amount_x18))
            v_quote_x18 = pb.get("balance", {}).get("v_quote_balance", "0")
            v_quote = float(_from_x18(v_quote_x18))

            if size == 0.0:
                continue

            side = "LONG" if size > 0 else "SHORT"
            entry_price = abs(v_quote / size) if size != 0 else 0.0

            # Fetch mark price from market_price query
            mark_price = 0.0
            try:
                mp_resp = self._session.post(url, json={"type": "market_price", "product_id": product_id}, timeout=5)
                mp_data = mp_resp.json()
                if mp_data.get("status") == "success":
                    bid = float(_from_x18(mp_data["data"].get("bid_x18", "0")))
                    ask = float(_from_x18(mp_data["data"].get("ask_x18", "0")))
                    mark_price = (bid + ask) / 2 if bid and ask else bid or ask
            except Exception:
                pass

            # PnL = size * mark_price + v_quote (v_quote is negative for longs)
            unrealized_pnl = size * mark_price + v_quote if mark_price else 0.0

            result.append({
                "instrument": symbol,
                "size": size,
                "entry_price": entry_price,
                "mark_price": mark_price,
                "unrealized_pnl": unrealized_pnl,
                "leverage": 0.0,
                "side": side,
            })
        return result

    def get_account_summary(self) -> dict:
        """Fetch account balance/equity summary."""
        if not self._sender_hex:
            return {"total_equity": "0", "available_balance": "0", "unrealized_pnl": "0", "positions": []}

        url = f"{self._gateway_rest}/query"
        payload = {"type": "subaccount_info", "subaccount": self._sender_hex}
        resp = self._session.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get("status") != "success":
            raise RuntimeError(f"NADO subaccount_info error: {data.get('error', str(data))}")

        sub_data = data.get("data", {})
        healths = sub_data.get("healths", [])
        unweighted = healths[2] if len(healths) > 2 else {}
        total_equity = str(_from_x18(unweighted.get("assets", "0")))

        positions = self.fetch_positions()
        return {
            "total_equity": total_equity,
            "available_balance": str(_from_x18(unweighted.get("health", "0"))),
            "unrealized_pnl": "0",
            "positions": positions,
        }

    def fetch_fees(self, symbol: str) -> dict:
        """Fetch current fee rates for a market from symbols data."""
        url = f"{self._gateway_rest}/symbols"
        resp = self._session.get(url, timeout=10)
        resp.raise_for_status()
        for s in resp.json():
            if s["symbol"] == symbol:
                maker = float(_from_x18(s.get("maker_fee_rate_x18", "0")))
                taker = float(_from_x18(s.get("taker_fee_rate_x18", "0")))
                return {
                    "maker_fee": maker,
                    "taker_fee": taker,
                    "symbol": symbol,
                }
        return {"maker_fee": 0.0, "taker_fee": 0.0, "symbol": symbol}

    # ------------------------------------------------------------------
    # Leverage
    # ------------------------------------------------------------------

    async def async_set_leverage(self, instrument: str, leverage: int) -> bool:
        """No-op: Nado uses unified margin — leverage is implicit from position size vs. collateral."""
        logger.info("NADO async_set_leverage(%s, %dx): no-op (unified margin)", instrument, leverage)
        return True

    # ══════════════════════════════════════════════════════════════════
    # Async methods for the new Funding-Arb Maker-Taker engine
    # (AsyncExchangeClient protocol — Phase 2)
    # ══════════════════════════════════════════════════════════════════

    async def _get_async_session(self):
        """Lazily create and return an httpx.AsyncClient."""
        if not hasattr(self, "_async_session") or self._async_session is None:
            import httpx
            self._async_session = httpx.AsyncClient(
                base_url=self._gateway_rest,
                verify=False,
                headers={"Content-Type": "application/json"},
                timeout=15.0,
            )
        return self._async_session

    async def async_fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        """Async version of fetch_order_book."""
        product_id = self._get_product_id(symbol)
        depth = min(limit, 100)
        client = await self._get_async_session()
        resp = await client.post("/query", json={"type": "market_liquidity", "product_id": product_id, "depth": depth})
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"NADO OB error for {symbol}: {data.get('error', str(data))}")
        book = data.get("data", {})
        bids = [[str(_from_x18(p)), str(_from_x18(q))] for p, q in book.get("bids", [])]
        asks = [[str(_from_x18(p)), str(_from_x18(q))] for p, q in book.get("asks", [])]
        return {"bids": bids, "asks": asks}

    async def async_fetch_markets(self) -> list[dict]:
        """Async version of fetch_markets."""
        client = await self._get_async_session()
        resp = await client.get("/symbols")
        resp.raise_for_status()
        symbols = resp.json()
        markets = []
        for s in symbols:
            sym = s["symbol"]
            pid = s["product_id"]
            self._symbol_to_product_id[sym] = pid
            self._product_id_to_symbol[pid] = sym
            tick_x18 = s.get("price_increment_x18", "0")
            if tick_x18 and tick_x18 != "0":
                self._tick_size_cache[sym] = _from_x18(tick_x18)
            size_inc = s.get("size_increment", "0")
            if size_inc and size_inc != "0":
                self._size_increment_cache[sym] = _from_x18(size_inc)
            # NADO min_size is USD notional
            min_size_raw = s.get("min_size", "0")
            if min_size_raw and min_size_raw != "0":
                self._min_notional_cache[sym] = _from_x18(min_size_raw)
            markets.append({
                "symbol": sym, "name": sym, "asset": self._extract_asset(sym),
                "product_id": pid, "type": s.get("type", ""),
                "tick_size": str(self._tick_size_cache.get(sym, "")),
                "min_size": str(self._min_size_cache.get(sym, "")),
            })
        return markets

    async def async_get_min_order_size(self, symbol: str) -> Decimal:
        """Return min order qty derived from USD notional min and current price."""
        if symbol not in self._min_notional_cache:
            await self.async_fetch_markets()
        return self.get_min_order_size(symbol)

    async def async_get_tick_size(self, symbol: str) -> Decimal:
        if symbol not in self._tick_size_cache:
            await self.async_fetch_markets()
        return self._tick_size_cache.get(symbol, Decimal("0.01"))

    @staticmethod
    def _build_post_only_appendix(reduce_only: bool = False) -> int:
        """Build appendix for POST_ONLY order: version=1, order_type=POST_ONLY(3)."""
        version = 1           # bits 0-7
        isolated = 0          # bit 8
        order_type = 3        # bits 9-10 (POST_ONLY=3)
        ro = 1 if reduce_only else 0  # bit 11
        return version | (isolated << 8) | (order_type << 9) | (ro << 11)

    @staticmethod
    def _build_default_appendix() -> int:
        """Build appendix for DEFAULT (GTT) order: version=1, order_type=DEFAULT(0)."""
        version = 1           # bits 0-7
        isolated = 0          # bit 8
        order_type = 0        # bits 9-10 (DEFAULT=0)
        return version | (isolated << 8) | (order_type << 9)

    def _place_signed_order(
        self, symbol: str, side: str, amount: Decimal, price: Decimal, appendix: int,
    ) -> dict:
        """Internal: sign and place an order with the given appendix flags."""
        self._require_trading()
        amount = self._round_qty(amount, symbol)
        product_id = self._get_product_id(symbol)
        tick = self.get_tick_size(symbol)
        price = (Decimal(str(price)) / tick).to_integral_value(rounding="ROUND_DOWN") * tick

        order_amount = amount if side == "buy" else -amount
        price_x18 = _to_x18(price)
        amount_x18 = _to_x18(order_amount)
        expiration = str(int(time.time()) + 300)  # 5 min for GTT / post-only
        nonce = _gen_order_nonce()

        verifying_contract = _gen_order_verifying_contract(product_id)
        signature = _sign_order(
            private_key=self._trading_key,
            chain_id=self._chain_id,
            verifying_contract=verifying_contract,
            sender=self._sender_hex,
            price_x18=price_x18,
            amount_x18=amount_x18,
            expiration=expiration,
            nonce=nonce,
            appendix=appendix,
        )

        payload = {
            "place_order": {
                "product_id": product_id,
                "order": {
                    "sender": self._sender_hex,
                    "priceX18": price_x18,
                    "amount": amount_x18,
                    "expiration": expiration,
                    "nonce": str(nonce),
                    "appendix": str(appendix),
                },
                "signature": "0x" + signature,
            }
        }
        ptype = self._product_type_cache.get(symbol, "")
        if ptype == "spot":
            payload["place_order"]["spot_leverage"] = True

        url = f"{self._gateway_rest}/execute"
        resp = self._session.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        resp_data = resp.json()

        status = resp_data.get("status", "unknown")
        digest = None
        if isinstance(resp_data.get("data"), dict):
            digest = resp_data["data"].get("digest")

        if status == "failure":
            error_msg = resp_data.get("error", "Unknown error")
            error_code = resp_data.get("error_code", "")
            raise RuntimeError(f"NADO order failed: {error_msg} (code={error_code})")

        if digest:
            self._order_product_cache[digest] = product_id

        return {
            "id": digest,
            "status": status,
            "limit_price": float(price),
            "digest": digest,
            "nonce": str(nonce),
            "product_id": product_id,
        }

    async def async_create_post_only_order(
        self, symbol: str, side: str, amount: Decimal, price: Decimal,
        reduce_only: bool = False,
    ) -> dict:
        """Place a POST_ONLY limit order on NADO (maker only).

        Uses appendix bits 9-10 = 3 (POST_ONLY). Rejected if it would cross the book.
        """
        import asyncio
        appendix = self._build_post_only_appendix(reduce_only=reduce_only)
        logger.info("NADO POST-ONLY %s %s qty=%s @ %s reduce_only=%s", side.upper(), symbol, amount, price, reduce_only)
        result = await asyncio.to_thread(self._place_signed_order, symbol, side, amount, price, appendix)
        logger.info("NADO post-only placed: digest=%s status=%s", result.get("digest"), result.get("status"))
        return result

    async def async_create_ioc_order(
        self, symbol: str, side: str, amount: Decimal, price: Decimal,
        reduce_only: bool = False, ioc_slippage_buffer_pct: float = 5.0,
    ) -> dict:
        """Place an IOC limit order on NADO (taker) with aggressive pricing.

        IOC orders on NADO should behave like market orders. Since NADO only supports
        limit orders, we apply a slippage buffer to ensure immediate execution:
        - BUY: limit price is 5% higher than requested (clears the ask side)
        - SELL: limit price is 5% lower than requested (clears the bid side)

        This ensures the order fills completely even if price moves during transmission.
        """
        import asyncio

        # Apply aggressive slippage buffer for market-like execution
        buffer_multiplier = Decimal(str(1 + ioc_slippage_buffer_pct / 100))
        if side.upper() == "BUY":
            adjusted_price = price * buffer_multiplier
        else:  # SELL
            adjusted_price = price / buffer_multiplier

        logger.info(
            "NADO IOC %s %s qty=%s: original_price=%s → adjusted_price=%s (buffer=%.1f%%) reduce_only=%s",
            side.upper(), symbol, amount, price, adjusted_price, ioc_slippage_buffer_pct, reduce_only
        )

        appendix = _build_reduce_only_ioc_appendix() if reduce_only else _build_ioc_appendix()
        result = await asyncio.to_thread(self._place_signed_order, symbol, side, amount, adjusted_price, appendix)
        # Add traded_qty for IOC orders (immediate fill)
        result["traded_qty"] = float(amount)
        logger.info("NADO IOC placed: digest=%s status=%s traded_qty=%s", result.get("digest"), result.get("status"), result["traded_qty"])
        return result

    def _sign_cancellation(self, product_ids: list[int], digests_hex: list[str], nonce: int) -> str:
        """Sign a Cancellation struct using EIP-712."""
        full_message = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "Cancellation": [
                    {"name": "sender", "type": "bytes32"},
                    {"name": "productIds", "type": "uint32[]"},
                    {"name": "digests", "type": "bytes32[]"},
                    {"name": "nonce", "type": "uint64"},
                ],
            },
            "primaryType": "Cancellation",
            "domain": {
                "name": "Nado",
                "version": "0.0.1",
                "chainId": self._chain_id,
                "verifyingContract": self._endpoint_addr,
            },
            "message": {
                "sender": bytes.fromhex(self._sender_hex[2:]),
                "productIds": product_ids,
                "digests": [bytes.fromhex(d[2:] if d.startswith("0x") else d) for d in digests_hex],
                "nonce": nonce,
            },
        }
        signable = encode_typed_data(full_message=full_message)
        signed = Account.sign_message(signable, self._trading_key)
        return signed.signature.hex()

    async def async_cancel_order(self, order_id: str) -> bool:
        """Cancel an open order on NADO using signed cancel_orders execute."""
        self._require_trading()
        product_id = self._order_product_cache.get(order_id)
        if product_id is None:
            # Fallback: try cancel with every known perp product_id
            logger.warning("NADO cancel: unknown product_id for digest %s — trying all known products", order_id[:16])
            if not self._symbol_to_product_id:
                self._load_symbols()
            for sym, pid in self._symbol_to_product_id.items():
                if self._product_type_cache.get(sym) == "spot":
                    continue
                ok = await self._cancel_with_product_id(order_id, pid)
                if ok:
                    logger.info("NADO cancel fallback succeeded with product_id=%d (%s)", pid, sym)
                    return True
            logger.warning("NADO cancel fallback: none of %d products worked for %s", len(self._symbol_to_product_id), order_id[:16])
            return False

        return await self._cancel_with_product_id(order_id, product_id)

    async def _cancel_with_product_id(self, order_id: str, product_id: int) -> bool:
        """Send a signed cancel_orders request for a single digest + product_id."""
        nonce = _gen_order_nonce()
        digest_hex = order_id if order_id.startswith("0x") else "0x" + order_id

        try:
            signature = self._sign_cancellation([product_id], [digest_hex], nonce)
        except Exception as exc:
            logger.warning("NADO cancel sign error: %s", exc)
            return False

        payload = {
            "cancel_orders": {
                "tx": {
                    "sender": self._sender_hex,
                    "productIds": [product_id],
                    "digests": [digest_hex],
                    "nonce": str(nonce),
                },
                "signature": "0x" + signature,
            }
        }

        client = await self._get_async_session()
        try:
            resp = await client.post("/execute", json=payload)
            resp.raise_for_status()
            data = resp.json()
            ok = data.get("status") == "success"
            logger.info("NADO cancel_orders(%s, pid=%d): %s", order_id[:16], product_id, "OK" if ok else data)
            return ok
        except Exception as exc:
            logger.warning("NADO cancel_orders(%s, pid=%d) error: %s", order_id[:16], product_id, exc)
            return False

    async def async_cancel_all_orders(self) -> bool:
        """Cancel all open orders across all known perp products on NADO."""
        self._require_trading()
        if not self._symbol_to_product_id:
            self._load_symbols()
        cancelled_any = False
        for sym, pid in self._symbol_to_product_id.items():
            if self._product_type_cache.get(sym) == "spot":
                continue
            nonce = _gen_order_nonce()
            # cancel_product_orders: cancel all orders for a given product
            try:
                signature = self._sign_cancellation([pid], [], nonce)
                payload = {
                    "cancel_product_orders": {
                        "tx": {
                            "sender": self._sender_hex,
                            "productIds": [pid],
                            "nonce": str(nonce),
                        },
                        "signature": "0x" + signature,
                    }
                }
                client = await self._get_async_session()
                resp = await client.post("/execute", json=payload)
                resp.raise_for_status()
                data = resp.json()
                if data.get("status") == "success":
                    cancelled_any = True
                    logger.info("NADO cancel_all: cancelled orders for product %d (%s)", pid, sym)
            except Exception as exc:
                logger.debug("NADO cancel_all product %d (%s) error: %s", pid, sym, exc)
        return cancelled_any

    async def async_check_order_fill(self, order_id: str) -> dict:
        """Check order fill status on NADO.

        For IOC orders, success from place_order = matched.
        For POST_ONLY/DEFAULT orders, we query order status.
        """
        client = await self._get_async_session()
        try:
            payload = {"type": "order", "digest": order_id, "subaccount": self._sender_hex}
            product_id = self._order_product_cache.get(order_id)
            if product_id is not None:
                payload["product_id"] = product_id
            resp = await client.post("/query", json=payload)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") != "success":
                return {"filled": False, "status": "API_ERROR", "error": str(data), "traded_qty": 0.0}
            order_data = data.get("data", {})
            filled_x18 = order_data.get("filled_amount", "0")
            original_x18 = order_data.get("original_amount", "0")
            filled_qty = abs(float(_from_x18(filled_x18)))
            original_qty = abs(float(_from_x18(original_x18)))
            remaining = original_qty - filled_qty
            is_filled = filled_qty > 0 and remaining <= 0
            return {
                "filled": is_filled,
                "status": "FILLED" if is_filled else ("PARTIAL" if filled_qty > 0 else "OPEN"),
                "traded_qty": filled_qty,
                "remaining_qty": max(0, remaining),
            }
        except Exception as exc:
            logger.warning("NADO async_check_order_fill(%s) error: %s", order_id[:16], exc)
            return {"filled": False, "status": "ERROR", "error": str(exc), "traded_qty": 0.0}

    async def async_fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        """Async version of fetch_positions."""
        if not self._sender_hex:
            return []
        client = await self._get_async_session()
        resp = await client.post("/query", json={"type": "subaccount_info", "subaccount": self._sender_hex})
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            raise RuntimeError(f"NADO subaccount_info error: {data.get('error', str(data))}")
        result = []
        sub_data = data.get("data", {})
        for pb in sub_data.get("perp_balances", []):
            product_id = pb.get("product_id", 0)
            symbol = self._product_id_to_symbol.get(product_id, f"product_{product_id}")
            if symbols and symbol not in symbols:
                continue
            amount_x18 = pb.get("balance", {}).get("amount", "0")
            size = float(_from_x18(amount_x18))
            v_quote_x18 = pb.get("balance", {}).get("v_quote_balance", "0")
            v_quote = float(_from_x18(v_quote_x18))
            if size == 0.0:
                continue
            side = "LONG" if size > 0 else "SHORT"
            entry_price = abs(v_quote / size) if size != 0 else 0.0

            # Fetch mark price from market_price query
            mark_price = 0.0
            try:
                mp_resp = await client.post("/query", json={"type": "market_price", "product_id": product_id})
                mp_data = mp_resp.json()
                if mp_data.get("status") == "success":
                    bid = float(_from_x18(mp_data["data"].get("bid_x18", "0")))
                    ask = float(_from_x18(mp_data["data"].get("ask_x18", "0")))
                    mark_price = (bid + ask) / 2 if bid and ask else bid or ask
            except Exception:
                pass

            unrealized_pnl = size * mark_price + v_quote if mark_price else 0.0
            result.append({
                "instrument": symbol, "size": size, "entry_price": entry_price,
                "mark_price": mark_price, "unrealized_pnl": unrealized_pnl, "leverage": 0.0, "side": side,
            })
        return result

    async def async_fetch_funding_rate(self, symbol: str) -> dict:
        """Fetch the current funding rate for a NADO perpetual.

        Uses the funding_rate indexer query endpoint.
        """
        client = await self._get_async_session()
        product_id = self._get_product_id(symbol)
        resp = await client.post("/query", json={"type": "funding_rate", "product_id": product_id})
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "success":
            return {"symbol": symbol, "funding_rate": 0.0, "next_funding_time": None}
        fr_data = data.get("data", {})
        rate_x18 = fr_data.get("funding_rate_x18", "0")
        return {
            "symbol": symbol,
            "funding_rate": float(_from_x18(rate_x18)),
            "update_time": fr_data.get("update_time"),
            "next_funding_time": None,
        }

    async def async_subscribe_fills(self, symbol: str, callback) -> None:
        """Register a per-symbol fill callback on the shared NADO subaccount WS.

        All bots share one WS using a subaccount-level subscription (product_id=null);
        fills are fanned out by symbol via _product_id_to_symbol mapping.
        """
        import asyncio
        self._fill_callbacks.append((symbol, callback))
        if self._fill_ws_task is None or self._fill_ws_task.done():
            self._fill_ws_task = asyncio.create_task(
                self._run_shared_fill_ws(), name="nado-fill-ws-shared"
            )
        try:
            await asyncio.get_event_loop().create_future()
        except asyncio.CancelledError:
            self._fill_callbacks = [(s, cb) for s, cb in self._fill_callbacks if cb is not callback]
            raise

    async def _run_shared_fill_ws(self) -> None:
        """Single shared WS connection to the NADO subaccount fill stream."""
        import websockets
        import json
        import asyncio

        ws_url = self._gateway_rest.replace("https://", "wss://") + "/subscribe"
        # product_id=null subscribes to all fills for the subaccount
        sub_msg = json.dumps({
            "method": "subscribe",
            "stream": {"type": "fill", "subaccount": self._sender_hex, "product_id": None},
        })

        logger.info("NADO fill WS connecting (shared): %s subaccount=%s",
                    ws_url, self._sender_hex[:20] if self._sender_hex else "?")
        async for ws in websockets.connect(ws_url, ssl=False):
            try:
                await ws.send(sub_msg)
                logger.info("NADO fill WS connected (serving %d callbacks)", len(self._fill_callbacks))

                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "fill":
                        continue
                    pid = msg.get("product_id", 0)
                    fill_symbol = self._product_id_to_symbol.get(pid, "")
                    fill = {
                        "order_id": msg.get("order_digest", ""),
                        "filled_qty": abs(float(_from_x18(msg.get("filled_qty", "0")))),
                        "remaining_qty": abs(float(_from_x18(msg.get("remaining_qty", "0")))),
                        "price": float(_from_x18(msg.get("price", "0"))),
                        "is_taker": msg.get("is_taker", True),
                        "fee": float(_from_x18(msg.get("fee", "0"))),
                        "symbol": fill_symbol,
                    }
                    for sym, cb in list(self._fill_callbacks):
                        if not sym or sym == fill_symbol:
                            try:
                                await cb(fill)
                            except Exception:
                                pass
            except websockets.ConnectionClosed:
                logger.warning("NADO fill WS disconnected, reconnecting…")
                continue
            except asyncio.CancelledError:
                break

    async def async_subscribe_funding_rate(self, symbol: str, callback) -> None:
        """Subscribe to NADO real-time funding rate WS stream.

        Updates every ~20 seconds. No authentication required.
        """
        import websockets
        import json

        ws_url = self._gateway_rest.replace("https://", "wss://") + "/subscribe"
        product_id = self._get_product_id(symbol) if symbol else None

        logger.info("NADO funding WS connecting: %s product=%s", ws_url, product_id)
        async for ws in websockets.connect(ws_url, ssl=False):
            try:
                sub_msg = json.dumps({
                    "method": "subscribe",
                    "stream": {
                        "type": "funding_rate",
                        "product_id": product_id,
                    },
                })
                await ws.send(sub_msg)
                logger.info("NADO funding WS subscribed")

                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "funding_rate":
                        continue
                    pid = msg.get("product_id", 0)
                    sym = self._product_id_to_symbol.get(pid, symbol or f"product_{pid}")
                    await callback({
                        "symbol": sym,
                        "funding_rate": float(_from_x18(msg.get("funding_rate_x18", "0"))),
                        "timestamp": msg.get("update_time", ""),
                    })
            except websockets.ConnectionClosed:
                logger.warning("NADO funding WS disconnected, reconnecting…")
                continue

    # ══════════════════════════════════════════════════════════════════
    # Linked Signer — wallet-connect authorization flow
    # ══════════════════════════════════════════════════════════════════

    @staticmethod
    def generate_trading_key() -> dict:
        """Generate a random ETH private key + address for use as a linked signer."""
        acct = Account.create()
        return {
            "private_key": acct.key.hex(),
            "address": acct.address,
        }

    def prepare_link_signer(self, wallet_address: str, subaccount_name: str = "default") -> dict:
        """Prepare EIP-712 typed data for the user to sign with MetaMask.

        Returns { typed_data, trading_key, trading_address, sender_hex, signer_hex }
        The typed_data must be signed by the user's wallet (eth_signTypedData_v4),
        then passed to submit_link_signer().
        """
        if self._chain_id is None or not self._endpoint_addr:
            self._load_contracts()

        # Generate a fresh trading key for the bot
        key_info = self.generate_trading_key()
        trading_key = key_info["private_key"]
        trading_address = key_info["address"]

        # Build sender and signer bytes32
        sender_hex = _build_sender_bytes32(wallet_address, subaccount_name)
        signer_hex = _build_sender_bytes32(trading_address, subaccount_name)

        # Get the current tx nonce for this wallet
        nonce = self._get_tx_nonce(wallet_address)

        # Build EIP-712 typed data for MetaMask
        typed_data = {
            "types": {
                "EIP712Domain": [
                    {"name": "name", "type": "string"},
                    {"name": "version", "type": "string"},
                    {"name": "chainId", "type": "uint256"},
                    {"name": "verifyingContract", "type": "address"},
                ],
                "LinkSigner": [
                    {"name": "sender", "type": "bytes32"},
                    {"name": "signer", "type": "bytes32"},
                    {"name": "nonce", "type": "uint64"},
                ],
            },
            "primaryType": "LinkSigner",
            "domain": {
                "name": "Nado",
                "version": "0.0.1",
                "chainId": self._chain_id,
                "verifyingContract": self._endpoint_addr,
            },
            "message": {
                "sender": sender_hex,
                "signer": signer_hex,
                "nonce": nonce,
            },
        }

        # Store pending state for submit step
        self._pending_link = {
            "trading_key": trading_key,
            "trading_address": trading_address,
            "wallet_address": wallet_address,
            "subaccount_name": subaccount_name,
            "sender_hex": sender_hex,
            "signer_hex": signer_hex,
            "nonce": nonce,
            "typed_data": typed_data,
        }

        logger.info(
            "NADO prepare_link_signer: wallet=%s trading=%s nonce=%d",
            wallet_address, trading_address, nonce,
        )

        return {
            "typed_data": typed_data,
            "trading_address": trading_address,
            "sender_hex": sender_hex,
            "signer_hex": signer_hex,
        }

    def submit_link_signer(self, signature: str) -> dict:
        """Submit the signed LinkSigner execute to NADO.

        Called after the user signs the typed_data from prepare_link_signer()
        in MetaMask. Returns { status, trading_key, trading_address }.
        """
        if not self._pending_link:
            raise RuntimeError("No pending link_signer — call prepare_link_signer() first")

        pending = self._pending_link

        # Ensure signature has 0x prefix
        if not signature.startswith("0x"):
            signature = "0x" + signature

        payload = {
            "link_signer": {
                "tx": {
                    "sender": pending["sender_hex"],
                    "signer": pending["signer_hex"],
                    "nonce": str(pending["nonce"]),
                },
                "signature": signature,
            }
        }

        url = f"{self._gateway_rest}/execute"
        resp = self._session.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        resp_data = resp.json()

        status = resp_data.get("status", "unknown")
        logger.info("NADO submit_link_signer: status=%s", status)

        if status == "failure":
            error_msg = resp_data.get("error", "Unknown error")
            error_code = resp_data.get("error_code", "")
            self._pending_link = None
            raise RuntimeError(f"NADO link_signer failed: {error_msg} (code={error_code})")

        result = {
            "status": status,
            "trading_key": pending["trading_key"],
            "trading_address": pending["trading_address"],
            "wallet_address": pending["wallet_address"],
            "subaccount_name": pending["subaccount_name"],
        }
        self._pending_link = None
        return result

    def get_trading_address(self) -> str | None:
        """Return the address derived from the active trading key, or None."""
        if not self._trading_key:
            return None
        return Account.from_key(self._trading_key).address

    def verify_signer(self) -> dict:
        """Verify that the trading key is valid for signing on Nado.

        When signing with the main wallet private_key, the check always passes
        because the main wallet key is always accepted by Nado regardless of
        the linked signer state.  When signing with a linked_signer_key, the
        key's address must match the remote linked signer.

        Returns: {"ok": bool, "local": str, "remote": str, "signing_mode": str, "error": str|None}
        """
        local = self.get_trading_address()
        if not local:
            return {"ok": False, "local": "", "remote": "", "signing_mode": "none", "error": "No trading key configured"}

        signing_mode = "wallet-key" if self._trading_key == self._private_key else "linked-signer"

        # When using the main wallet key, signing always works
        if signing_mode == "wallet-key":
            return {"ok": True, "local": local, "remote": "(wallet-key — always valid)", "signing_mode": signing_mode, "error": None}

        # When using linked signer key, verify it matches the remote
        try:
            info = self.get_linked_signer()
        except Exception as exc:
            return {"ok": False, "local": local, "remote": "", "signing_mode": signing_mode, "error": str(exc)}
        remote = info.get("linked_signer") or ""
        if not remote:
            return {"ok": False, "local": local, "remote": "", "signing_mode": signing_mode, "error": info.get("error", "No remote signer found")}
        match = local.lower() == remote.lower()
        if not match:
            logger.warning("NADO signer mismatch: local=%s remote=%s — linked signer was changed externally (e.g. via nado.xyz 1-Click Trading). Re-link via bot frontend.", local, remote)
        return {
            "ok": match, "local": local, "remote": remote, "signing_mode": signing_mode,
            "error": None if match else "Signer changed externally — please re-link via Settings",
        }

    def get_linked_signer(self, wallet_address: str | None = None, subaccount_name: str | None = None) -> dict:
        """Query NADO for the current linked signer of a subaccount."""
        addr = wallet_address or self._signer_address or self._wallet_address
        sub = subaccount_name or self._subaccount_name
        if not addr:
            return {"linked_signer": None, "error": "No wallet address"}

        sender_hex = _build_sender_bytes32(addr, sub)
        url = f"{self._gateway_rest}/query"
        payload = {"type": "linked_signer", "subaccount": sender_hex}
        try:
            resp = self._session.post(url, json=payload, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "success":
                signer = data.get("data", {}).get("linked_signer", "")
                return {"linked_signer": signer, "subaccount": sender_hex}
            return {"linked_signer": None, "error": data.get("error", "unknown")}
        except Exception as exc:
            logger.warning("NADO get_linked_signer error: %s", exc)
            return {"linked_signer": None, "error": str(exc)}

    def _get_tx_nonce(self, address: str) -> int:
        """Fetch the current tx nonce for an address from NADO."""
        url = f"{self._gateway_rest}/query"
        payload = {"type": "nonces", "address": address}
        resp = self._session.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") == "success":
            return int(data.get("data", {}).get("tx_nonce", 0))
        raise RuntimeError(f"NADO nonces query failed: {data}")
