"""RISEx Exchange client — REST + EIP-712 permit signing for rise.trade.

Public data (orderbook, markets): REST queries against the RISEx API.
Trading (orders, positions): REST endpoints with EIP-712 VerifyWitness permits
using eth_account (compatible with the project's existing eth-account>=0.13).
Conforms to the ExchangeClient / AsyncExchangeClient protocol in app/exchange.py.

Authentication model:
  1. A *signer key* (private key) must be pre-registered via the RISEx web UI.
  2. Each state-changing API call carries a VerifyWitness EIP-712 permit signed
     by that signer key.
  3. Nonce management is bitmap-based (nonceAnchor + nonceBitmapIndex).
"""

import asyncio
import base64
import logging
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any

import requests
import urllib3
from eth_account import Account
from eth_account.messages import encode_typed_data

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("tradeautonom.risex_client")

# ──────────────────────────────────────────────────────────────────────
# Keccak-256 helper (eth_hash ships with eth_account)
# ──────────────────────────────────────────────────────────────────────
try:
    from eth_hash.auto import keccak as _keccak_fn
except ImportError:
    try:
        import sha3 as _sha3
        _keccak_fn = lambda data: _sha3.keccak_256(data).digest()  # noqa: E731
    except ImportError:
        from hashlib import sha3_256 as _sha3_256
        logger.warning("Using SHA3-256 as keccak fallback — signatures WILL be wrong. Install eth-hash.")
        _keccak_fn = lambda data: _sha3_256(data).digest()  # noqa: E731


def _keccak256(data: bytes) -> bytes:
    """Compute keccak-256 hash."""
    return _keccak_fn(data)


# ──────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────
ACTION_PLACE_ORDER = b"RISE_PERPS_PLACE_ORDER_V1"
ACTION_CANCEL_ORDER = b"RISE_PERPS_CANCEL_ORDER_V1"
ACTION_CANCEL_ALL_ORDERS = b"RISE_PERPS_CANCEL_ALL_ORDERS_V1"

ACTION_PLACE_ORDER_HASH = _keccak256(ACTION_PLACE_ORDER)
ACTION_CANCEL_ORDER_HASH = _keccak256(ACTION_CANCEL_ORDER)
ACTION_CANCEL_ALL_ORDERS_HASH = _keccak256(ACTION_CANCEL_ALL_ORDERS)

# Protocol header flags
V3_FLAG_PERMIT = 0x01
V3_FLAG_BUILDER = 0x02
V3_FLAG_CLIENT_ID = 0x04
V3_FLAG_PERMIT_ERC1271 = 0x09
V3_FLAG_TTL = 0x10

# Bitmap nonce limits
MAX_BITMAP_INDEX = 207

# Enums
SIDE_LONG = 0
SIDE_SHORT = 1
ORDER_TYPE_MARKET = 0
ORDER_TYPE_LIMIT = 1
TIF_GTC = 0
TIF_GTT = 1
TIF_FOK = 2
TIF_IOC = 3
STP_EXPIRE_MAKER = 0

# REST base URLs
_BASE_URLS = {
    "mainnet": "https://api.rise.trade",
    "testnet": "https://api.testnet.rise.trade",
}
_WS_URLS = {
    "mainnet": "wss://ws.rise.trade/ws",
    "testnet": "wss://ws.testnet.rise.trade/ws",
}


# ──────────────────────────────────────────────────────────────────────
# ABI encoding helpers (Solidity abi.encode for static types)
# ──────────────────────────────────────────────────────────────────────

def _abi_encode(types_values: list[tuple[str, Any]]) -> bytes:
    """Encode a list of (solidity_type, value) pairs using abi.encode rules.

    Supports: bytes32, uint8/16/64/128/256, int256.
    All values are zero-padded to 32 bytes (standard ABI encoding).
    """
    result = b""
    for typ, val in types_values:
        if typ == "bytes32":
            if isinstance(val, bytes):
                assert len(val) == 32, f"bytes32 must be 32 bytes, got {len(val)}"
                result += val
            else:
                hex_str = val[2:] if isinstance(val, str) and val.startswith("0x") else str(val)
                raw = bytes.fromhex(hex_str)
                assert len(raw) == 32
                result += raw
        elif typ.startswith("uint"):
            result += int(val).to_bytes(32, byteorder="big")
        elif typ.startswith("int"):
            v = int(val)
            if v < 0:
                v = (1 << 256) + v
            result += v.to_bytes(32, byteorder="big")
        else:
            raise ValueError(f"Unsupported ABI type: {typ}")
    return result


# ──────────────────────────────────────────────────────────────────────
# Order encoding helpers (port of risex-ts encoder.ts)
# ──────────────────────────────────────────────────────────────────────

def _encode_order_data(
    market_id: int,
    size_steps: int,
    price_ticks: int,
    side: int,
    post_only: bool,
    reduce_only: bool,
    stp_mode: int,
    order_type: int,
    time_in_force: int,
) -> int:
    """Encode order parameters into 88-bit compressed format.

    Bit layout (88 bits, big-endian):
      [87:70]  marketId      (16 bits)
      [69:38]  sizeSteps     (32 bits)
      [37:14]  priceTicks    (24 bits)
      [13:6]   order flags   (8 bits)
      [5:1]    headerVersion (5 bits, always 1)
      [0]      reserved      (1 bit, always 0)
    """
    order_flags = 0
    if side & 1:
        order_flags |= 0x01
    if post_only:
        order_flags |= 0x02
    if reduce_only:
        order_flags |= 0x04
    order_flags |= (stp_mode & 3) << 3
    order_flags |= (order_type & 1) << 5
    order_flags |= (time_in_force & 3) << 6

    header_version = 1
    data = 0
    data |= (market_id & 0xFFFF) << 70
    data |= (size_steps & 0xFFFFFFFF) << 38
    data |= (price_ticks & 0xFFFFFF) << 14
    data |= (order_flags & 0xFF) << 6
    data |= (header_version & 0x1F) << 1
    return data


def _compute_header_flags(builder_id: int, client_order_id: int, ttl_units: int) -> int:
    """Compute protocol header flags based on optional fields."""
    flags = V3_FLAG_PERMIT
    if builder_id != 0:
        flags |= V3_FLAG_BUILDER
    if client_order_id != 0:
        flags |= V3_FLAG_CLIENT_ID
    if ttl_units != 0:
        flags |= V3_FLAG_TTL
    return flags


def _encode_place_order_hash(
    market_id: int,
    size_steps: int,
    price_ticks: int,
    side: int,
    post_only: bool,
    reduce_only: bool,
    stp_mode: int = STP_EXPIRE_MAKER,
    order_type: int = ORDER_TYPE_LIMIT,
    time_in_force: int = TIF_GTC,
    builder_id: int = 0,
    client_order_id: int = 0,
    ttl_units: int = 0,
) -> bytes:
    """Compute the keccak256 hash for a place order action.

    hash = keccak256(abi.encode(actionTypeHash, headerFlags, orderData, builderID, clientOrderID, ttlUnits))
    """
    order_data = _encode_order_data(
        market_id, size_steps, price_ticks, side,
        post_only, reduce_only, stp_mode, order_type, time_in_force,
    )
    header_flags = _compute_header_flags(builder_id, client_order_id, ttl_units)

    encoded = _abi_encode([
        ("bytes32", ACTION_PLACE_ORDER_HASH),
        ("uint8", header_flags),
        ("uint256", order_data),
        ("uint16", builder_id),
        ("uint64", client_order_id),
        ("uint16", ttl_units),
    ])
    return _keccak256(encoded)


def _encode_cancel_order_hash(market_id: int, resting_order_id: int) -> bytes:
    """Compute the keccak256 hash for a cancel order action.

    hash = keccak256(abi.encode(actionTypeHash, uint256(marketID), uint256(restingOrderID)))
    """
    encoded = _abi_encode([
        ("bytes32", ACTION_CANCEL_ORDER_HASH),
        ("uint256", market_id),
        ("uint256", resting_order_id),
    ])
    return _keccak256(encoded)


def _encode_cancel_all_hash(market_id: int = 0) -> bytes:
    """Compute the keccak256 hash for a cancel-all action.

    hash = keccak256(abi.encode(actionTypeHash, uint256(marketID)))
    """
    encoded = _abi_encode([
        ("bytes32", ACTION_CANCEL_ALL_ORDERS_HASH),
        ("uint256", market_id),
    ])
    return _keccak256(encoded)


def _encode_leverage_hash(market_id: int, leverage: int) -> bytes:
    """Compute the keccak256 hash for a leverage update.

    hash = keccak256(abi.encode(uint256(marketId), uint128(leverage)))
    """
    encoded = _abi_encode([
        ("uint256", market_id),
        ("uint128", leverage),
    ])
    return _keccak256(encoded)


def _fix_signature_v(sig_bytes: bytes) -> bytes:
    """Fix EIP-712 signature V value: some signers produce v=0/1 instead of v=27/28."""
    sig = bytearray(sig_bytes)
    if len(sig) == 65 and sig[64] < 27:
        sig[64] += 27
    return bytes(sig)


# ──────────────────────────────────────────────────────────────────────
# EIP-712 type definitions
# ──────────────────────────────────────────────────────────────────────

_EIP712_DOMAIN_TYPE = [
    {"name": "name", "type": "string"},
    {"name": "version", "type": "string"},
    {"name": "chainId", "type": "uint256"},
    {"name": "verifyingContract", "type": "address"},
]

_VERIFY_WITNESS_TYPE = [
    {"name": "account", "type": "address"},
    {"name": "target", "type": "address"},
    {"name": "hash", "type": "bytes32"},
    {"name": "nonceAnchor", "type": "uint48"},
    {"name": "nonceBitmap", "type": "uint8"},
    {"name": "deadline", "type": "uint32"},
]


# ──────────────────────────────────────────────────────────────────────
# Client
# ──────────────────────────────────────────────────────────────────────

class RisexClient:
    """RISEx Exchange client with public data + perp trading support.

    Implements both ExchangeClient (sync) and AsyncExchangeClient protocols.
    """

    def __init__(
        self,
        account_address: str = "",
        signer_key: str = "",
        env: str = "mainnet",
    ) -> None:
        self._account_address = account_address.lower() if account_address else ""
        self._signer_key = signer_key
        self._env = env
        self._has_credentials = bool(signer_key and account_address)

        # Market caches (populated from /v1/markets)
        self._market_id_map: dict[str, int] = {}        # "BTC/USDC" -> 1
        self._id_to_symbol: dict[int, str] = {}          # 1 -> "BTC/USDC"
        self._step_size_cache: dict[str, Decimal] = {}   # symbol -> step_size
        self._step_price_cache: dict[str, Decimal] = {}  # symbol -> step_price
        self._min_order_size_cache: dict[str, Decimal] = {}  # symbol -> min base qty
        self._max_leverage_cache: dict[str, int] = {}    # symbol -> max leverage

        # Order tracking
        self._order_market_cache: dict[str, int] = {}    # order_id -> market_id
        self._fill_callbacks: list[tuple[str, object]] = []
        self._fill_ws_task: object = None

        # Portfolio cache: get_account_summary + fetch_positions both consume
        # the same /v1/portfolio/details response. 5 s TTL keeps the dashboard
        # fresh while preventing the per-bot status loop from hammering the
        # endpoint when several bots call simultaneously. Sync and async use
        # separate cache slots because they live on independent HTTP clients.
        self._portfolio_cache_sync: dict | None = None
        self._portfolio_cache_sync_ts: float = 0.0
        self._portfolio_cache_async: dict | None = None
        self._portfolio_cache_async_ts: float = 0.0

        # System config (loaded at init)
        self._chain_id: int | None = None
        self._auth_contract: str = ""
        self._router_contract: str = ""

        # EIP-712 domain (loaded from API or constructed)
        self._eip712_domain: dict | None = None

        # Signer address
        self._signer_address: str = ""
        if self._signer_key:
            self._signer_address = Account.from_key(self._signer_key).address

        # REST session
        base_url = _BASE_URLS.get(env, _BASE_URLS["mainnet"])
        self._base_url = base_url
        self._ws_url = _WS_URLS.get(env, _WS_URLS["mainnet"])

        self._session = requests.Session()
        self._session.verify = False
        self._session.headers.update({
            "Content-Type": "application/json",
            "Accept": "application/json",
        })

        # Pre-load system config + markets
        try:
            self._load_system_config()
        except Exception as exc:
            logger.warning("Failed to pre-load RISEx system config: %s", exc)
        try:
            self._load_markets()
        except Exception as exc:
            logger.warning("Failed to pre-load RISEx markets: %s", exc)

        if self._has_credentials:
            logger.info(
                "RISEx client initialised WITH trading (env=%s account=%s signer=%s)",
                env, self._account_address[:10] + "...", self._signer_address[:10] + "...",
            )
        else:
            logger.info("RISEx client initialised READ-ONLY (env=%s)", env)

    # ══════════════════════════════════════════════════════════════════
    # Initialisation helpers
    # ══════════════════════════════════════════════════════════════════

    def _load_system_config(self) -> None:
        """Fetch /v1/system/config and populate chain_id + contract addresses."""
        resp = self._session.get(f"{self._base_url}/v1/system/config", timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", {})

        chain_info = data.get("chain", {})
        self._chain_id = int(chain_info.get("chain_id", 0))

        addrs = data.get("addresses", {})
        self._auth_contract = addrs.get("auth", "")
        self._router_contract = addrs.get("router", "") or addrs.get("orders_manager", "")

        logger.info(
            "RISEx config: chain_id=%s auth=%s router=%s",
            self._chain_id, self._auth_contract[:12] + "...", self._router_contract[:12] + "...",
        )

    def _load_eip712_domain(self) -> None:
        """Fetch /v1/auth/eip712-domain and cache it."""
        resp = self._session.get(f"{self._base_url}/v1/auth/eip712-domain", timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        self._eip712_domain = {
            "name": data.get("name", "RISEx"),
            "version": data.get("version", "1"),
            "chainId": int(data.get("chain_id", self._chain_id or 0)),
            "verifyingContract": data.get("verifying_contract", self._auth_contract),
        }
        logger.info("RISEx EIP-712 domain loaded: %s", self._eip712_domain)

    def _get_eip712_domain(self) -> dict:
        """Return cached EIP-712 domain, loading if needed."""
        if self._eip712_domain is None:
            try:
                self._load_eip712_domain()
            except Exception:
                # Fallback to manually constructed domain
                self._eip712_domain = {
                    "name": "RISEx",
                    "version": "1",
                    "chainId": self._chain_id or 4153,
                    "verifyingContract": self._auth_contract,
                }
        return self._eip712_domain

    def _load_markets(self) -> None:
        """Fetch /v1/markets and populate caches."""
        resp = self._session.get(f"{self._base_url}/v1/markets", timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        markets = data.get("markets", [])

        for m in markets:
            market_id = int(m["market_id"])
            config = m.get("config", {})
            symbol = config.get("name", m.get("display_name", ""))
            if not symbol:
                continue

            self._market_id_map[symbol] = market_id
            self._id_to_symbol[market_id] = symbol
            self._step_size_cache[symbol] = Decimal(config.get("step_size", "0"))
            self._step_price_cache[symbol] = Decimal(config.get("step_price", "0"))
            self._min_order_size_cache[symbol] = Decimal(config.get("min_order_size", "0"))
            self._max_leverage_cache[symbol] = int(config.get("max_leverage", "1"))

        logger.info("RISEx markets loaded: %d", len(markets))

    def _require_trading(self) -> None:
        if not self._has_credentials:
            raise RuntimeError("RISEx trading not available — set RISEX_SIGNER_KEY and RISEX_ACCOUNT_ADDRESS")
        if self._chain_id is None:
            self._load_system_config()

    def _get_market_id(self, symbol: str) -> int:
        """Resolve symbol to RISEx market_id."""
        if symbol not in self._market_id_map:
            self._load_markets()
        mid = self._market_id_map.get(symbol)
        if mid is None:
            raise ValueError(f"Unknown RISEx symbol: {symbol}")
        return mid

    # ══════════════════════════════════════════════════════════════════
    # Price / size conversions
    # ══════════════════════════════════════════════════════════════════

    def _to_price_ticks(self, symbol: str, price: Decimal) -> int:
        """Convert a decimal price to integer price_ticks."""
        step = self._step_price_cache.get(symbol, Decimal("0.1"))
        return int(price / step)

    def _to_size_steps(self, symbol: str, size: Decimal) -> int:
        """Convert a decimal size to integer size_steps."""
        step = self._step_size_cache.get(symbol, Decimal("0.000001"))
        return int(size / step)

    def _from_price_ticks(self, symbol: str, ticks: int) -> Decimal:
        """Convert integer price_ticks to decimal price."""
        step = self._step_price_cache.get(symbol, Decimal("0.1"))
        return Decimal(ticks) * step

    def _from_size_steps(self, symbol: str, steps: int) -> Decimal:
        """Convert integer size_steps to decimal size."""
        step = self._step_size_cache.get(symbol, Decimal("0.000001"))
        return Decimal(steps) * step

    def _round_price(self, symbol: str, price: Decimal) -> Decimal:
        """Round price to the nearest step_price."""
        step = self._step_price_cache.get(symbol, Decimal("0.1"))
        return (price / step).to_integral_value(rounding=ROUND_DOWN) * step

    def _round_qty(self, symbol: str, qty: Decimal) -> Decimal:
        """Round quantity to the nearest step_size."""
        step = self._step_size_cache.get(symbol, Decimal("0.000001"))
        return (qty / step).to_integral_value(rounding=ROUND_DOWN) * step

    # ══════════════════════════════════════════════════════════════════
    # EIP-712 signing
    # ══════════════════════════════════════════════════════════════════

    def _sign_verify_witness(
        self,
        action_hash: bytes,
        target: str,
        nonce_anchor: int,
        nonce_bitmap_index: int,
        deadline: int,
    ) -> str:
        """Sign a VerifyWitness EIP-712 message and return base64 signature.

        Args:
            action_hash: 32-byte keccak256 hash of the encoded action data
            target: contract address the permit authorises interaction with
            nonce_anchor: bitmap nonce epoch
            nonce_bitmap_index: bit index within the nonce bitmap
            deadline: unix timestamp (seconds) when the permit expires
        """
        domain = self._get_eip712_domain()

        full_message = {
            "types": {
                "EIP712Domain": _EIP712_DOMAIN_TYPE,
                "VerifyWitness": _VERIFY_WITNESS_TYPE,
            },
            "primaryType": "VerifyWitness",
            "domain": domain,
            "message": {
                "account": self._account_address,
                "target": target,
                "hash": action_hash,  # bytes32 as raw bytes
                "nonceAnchor": nonce_anchor,
                "nonceBitmap": nonce_bitmap_index,
                "deadline": deadline,
            },
        }

        signable = encode_typed_data(full_message=full_message)
        signed = Account.sign_message(signable, self._signer_key)
        sig_bytes = _fix_signature_v(signed.signature)
        return base64.b64encode(sig_bytes).decode("ascii")

    def _get_nonce_state_sync(self) -> dict:
        """Fetch current nonce state from the API (sync)."""
        resp = self._session.get(
            f"{self._base_url}/v1/nonce-state/{self._account_address}", timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return {
            "nonce_anchor": int(data.get("nonce_anchor", "0")),
            "current_bitmap_index": int(data.get("current_bitmap_index", 0)),
        }

    def _create_permit_sync(self, action_hash: bytes, target: str | None = None) -> dict:
        """Create a complete permit_params dict (sync).

        Fetches nonce state, signs the VerifyWitness message, returns the
        dict ready to include in API requests.
        """
        self._require_trading()

        nonce = self._get_nonce_state_sync()
        nonce_anchor = nonce["nonce_anchor"]
        bitmap_index = nonce["current_bitmap_index"]
        if bitmap_index > MAX_BITMAP_INDEX:
            nonce_anchor += 1
            bitmap_index = 0

        deadline = int(time.time()) + 300  # 5 minutes

        contract = target or self._router_contract
        signature = self._sign_verify_witness(
            action_hash, contract, nonce_anchor, bitmap_index, deadline,
        )

        return {
            "account": self._account_address,
            "signer": self._signer_address,
            "nonce_anchor": nonce_anchor,
            "nonce_bitmap_index": bitmap_index,
            "deadline": deadline,
            "signature": signature,
        }

    async def _get_nonce_state_async(self) -> dict:
        """Fetch current nonce state from the API (async)."""
        client = await self._get_async_session()
        resp = await client.get(f"/v1/nonce-state/{self._account_address}")
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return {
            "nonce_anchor": int(data.get("nonce_anchor", "0")),
            "current_bitmap_index": int(data.get("current_bitmap_index", 0)),
        }

    async def _create_permit_async(self, action_hash: bytes, target: str | None = None) -> dict:
        """Create a complete permit_params dict (async)."""
        self._require_trading()

        nonce = await self._get_nonce_state_async()
        nonce_anchor = nonce["nonce_anchor"]
        bitmap_index = nonce["current_bitmap_index"]
        if bitmap_index > MAX_BITMAP_INDEX:
            nonce_anchor += 1
            bitmap_index = 0

        deadline = int(time.time()) + 300

        contract = target or self._router_contract
        signature = self._sign_verify_witness(
            action_hash, contract, nonce_anchor, bitmap_index, deadline,
        )

        return {
            "account": self._account_address,
            "signer": self._signer_address,
            "nonce_anchor": nonce_anchor,
            "nonce_bitmap_index": bitmap_index,
            "deadline": deadline,
            "signature": signature,
        }

    # ══════════════════════════════════════════════════════════════════
    # ExchangeClient protocol (sync)
    # ══════════════════════════════════════════════════════════════════

    @property
    def name(self) -> str:
        return "risex"

    @property
    def can_trade(self) -> bool:
        return self._has_credentials

    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        """Fetch orderbook, normalised to [[price_str, qty_str], ...]."""
        market_id = self._get_market_id(symbol)
        resp = self._session.get(
            f"{self._base_url}/v1/orderbook",
            params={"market_id": market_id, "limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})

        bids = [[b["price"], b["quantity"]] for b in data.get("bids", [])]
        asks = [[a["price"], a["quantity"]] for a in data.get("asks", [])]
        return {"bids": bids, "asks": asks}

    def fetch_markets(self) -> list[dict]:
        """Return list of available markets with normalised keys."""
        if not self._market_id_map:
            self._load_markets()

        markets = []
        for symbol, market_id in self._market_id_map.items():
            base = symbol.split("/")[0] if "/" in symbol else symbol
            markets.append({
                "symbol": symbol,
                "name": symbol,
                "asset": base,
                "market_id": market_id,
                "type": "perp",
                "tick_size": str(self._step_price_cache.get(symbol, Decimal(0))),
                "step_size": str(self._step_size_cache.get(symbol, Decimal(0))),
                "min_order_size": str(self._min_order_size_cache.get(symbol, Decimal(0))),
                "max_leverage": self._max_leverage_cache.get(symbol, 1),
            })
        return markets

    def get_min_order_size(self, symbol: str) -> Decimal:
        """Return the minimum order size in base qty."""
        if symbol not in self._min_order_size_cache:
            self._load_markets()
        return self._min_order_size_cache.get(symbol, Decimal(0))

    def get_tick_size(self, symbol: str) -> Decimal:
        """Return the price tick size."""
        if symbol not in self._step_price_cache:
            self._load_markets()
        return self._step_price_cache.get(symbol, Decimal("0.1"))

    def get_qty_step(self, symbol: str) -> Decimal:
        """Return the qty step size."""
        if symbol not in self._step_size_cache:
            self._load_markets()
        return self._step_size_cache.get(symbol, Decimal("0.000001"))

    def create_aggressive_limit_order(
        self, symbol: str, side: str, amount: Decimal, offset_ticks: int = 2,
        best_price: float | None = None, limit_price: float | None = None,
    ) -> dict:
        """Place an aggressive IOC limit order (sync version for ExchangeClient protocol)."""
        self._require_trading()
        market_id = self._get_market_id(symbol)

        if limit_price is not None:
            price = Decimal(str(limit_price))
        elif best_price is not None:
            tick = self.get_tick_size(symbol)
            price = Decimal(str(best_price))
            if side.lower() == "buy":
                price += tick * offset_ticks
            else:
                price -= tick * offset_ticks
        else:
            raise ValueError("Either best_price or limit_price must be provided")

        price = self._round_price(symbol, price)
        amount = self._round_qty(symbol, amount)

        # Pre-flight: rise.trade rejects orders below market.config.min_order_size
        # with HTTP 400 (and a confusing generic error). Fail fast with a clear
        # message so the operator can fix the bot config without poking the API.
        min_size = self._min_order_size_cache.get(symbol, Decimal("0"))
        if min_size > 0 and amount < min_size:
            raise ValueError(
                f"RISEx {symbol}: order size {amount} below min_order_size {min_size}. "
                f"Increase bot quantity or reduce twap_num_chunks."
            )

        price_ticks = self._to_price_ticks(symbol, price)
        size_steps = self._to_size_steps(symbol, amount)
        side_int = SIDE_LONG if side.lower() == "buy" else SIDE_SHORT

        action_hash = _encode_place_order_hash(
            market_id=market_id,
            size_steps=size_steps,
            price_ticks=price_ticks,
            side=side_int,
            post_only=False,
            reduce_only=False,
            order_type=ORDER_TYPE_LIMIT,
            time_in_force=TIF_IOC,
        )
        permit = self._create_permit_sync(action_hash)

        payload = {
            "market_id": market_id,
            "side": side_int,
            "order_type": ORDER_TYPE_LIMIT,
            "price_ticks": price_ticks,
            "size_steps": size_steps,
            "time_in_force": TIF_IOC,
            "post_only": False,
            "reduce_only": False,
            "stp_mode": STP_EXPIRE_MAKER,
            "ttl_units": 0,
            "client_order_id": "0",
            "builder_id": 0,
            "permit": permit,
        }

        resp = self._session.post(f"{self._base_url}/v1/orders/place", json=payload, timeout=15)
        if resp.status_code >= 400:
            _log_risex_http_error("orders/place sync IOC", resp, payload)
        resp.raise_for_status()
        result = resp.json().get("data", {})
        order_id = result.get("order_id", "")
        self._order_market_cache[order_id] = market_id

        logger.info("RISEx IOC order placed: %s side=%s size=%s price=%s → id=%s", symbol, side, amount, price, order_id)
        return {
            "id": order_id,
            "sc_order_id": result.get("sc_order_id", ""),
            "tx_hash": result.get("tx_hash", ""),
            "symbol": symbol,
            "side": side,
            "price": str(price),
            "amount": str(amount),
            "status": "PENDING",
        }

    def check_order_fill(self, order_id: str) -> dict:
        """Check if an order has been filled (sync)."""
        resp = self._session.get(
            f"{self._base_url}/v1/orders",
            params={"account": self._account_address, "limit": 50},
            timeout=10,
        )
        resp.raise_for_status()
        orders = resp.json().get("data", {}).get("orders", [])

        for o in orders:
            if o.get("order_id") == order_id:
                status = o.get("status", "").lower()
                filled_size = Decimal(o.get("filled_size", "0"))
                total_size = Decimal(o.get("size", "0"))
                is_filled = status in ("filled", "completed") or (filled_size > 0 and status == "cancelled")
                return {
                    "filled": is_filled,
                    "status": status,
                    "traded_qty": float(filled_size),
                    "total_qty": float(total_size),
                    "order_id": order_id,
                }

        return {"filled": False, "status": "unknown", "traded_qty": 0.0, "order_id": order_id}

    # ══════════════════════════════════════════════════════════════════
    # Portfolio (single source for balance + positions, RISEx OpenAPI
    # AccountService_GetPortfolioDetails — see
    # https://developer.rise.trade/reference/accountservice_getportfoliodetails)
    # ══════════════════════════════════════════════════════════════════

    _PORTFOLIO_CACHE_TTL_S = 5.0

    def _fetch_portfolio_sync(self, force: bool = False) -> dict:
        """Fetch /v1/portfolio/details, cached 5 s.

        Returns the parsed portfolio object (without the outer ``data``
        envelope), with both ``summary`` and ``positions``. Empty
        ``{"summary": {}, "positions": []}`` if no account configured.
        """
        if not self._account_address:
            return {"summary": {}, "positions": []}
        now = time.time()
        if not force and self._portfolio_cache_sync is not None and \
                now - self._portfolio_cache_sync_ts < self._PORTFOLIO_CACHE_TTL_S:
            return self._portfolio_cache_sync
        resp = self._session.get(
            f"{self._base_url}/v1/portfolio/details",
            params={"account": self._account_address},
            timeout=10,
        )
        resp.raise_for_status()
        body = resp.json()
        portfolio = _unwrap_data(body)
        if not isinstance(portfolio, dict):
            portfolio = {"summary": {}, "positions": []}
        self._portfolio_cache_sync = portfolio
        self._portfolio_cache_sync_ts = now
        return portfolio

    async def _fetch_portfolio_async(self, force: bool = False) -> dict:
        """Async pendant of _fetch_portfolio_sync — separate cache slot."""
        if not self._account_address:
            return {"summary": {}, "positions": []}
        now = time.time()
        if not force and self._portfolio_cache_async is not None and \
                now - self._portfolio_cache_async_ts < self._PORTFOLIO_CACHE_TTL_S:
            return self._portfolio_cache_async
        client = await self._get_async_session()
        resp = await client.get(
            "/v1/portfolio/details",
            params={"account": self._account_address},
        )
        resp.raise_for_status()
        body = resp.json()
        portfolio = _unwrap_data(body)
        if not isinstance(portfolio, dict):
            portfolio = {"summary": {}, "positions": []}
        self._portfolio_cache_async = portfolio
        self._portfolio_cache_async_ts = now
        return portfolio

    def fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        """Fetch open positions (sync) via /v1/portfolio/details."""
        if not self._account_address:
            return []
        try:
            portfolio = self._fetch_portfolio_sync()
        except Exception as exc:
            logger.warning("RISEx fetch_positions: portfolio fetch failed — %s", exc)
            return []
        raw = portfolio.get("positions") or []
        return self._normalise_positions(raw, symbols)

    def get_account_summary(self) -> dict:
        """Fetch portfolio summary in the GRVT/Extended-compatible shape.

        Backed by RISEx /v1/portfolio/details (single call, returns both the
        margin summary and the position list). Falls back to
        /v1/account/cross-margin-balance for a minimal balance string if the
        portfolio endpoint is unavailable. Returns:

          {
            total_equity, available_balance, unrealized_pnl, positions,
            # bonus fields the UI can ignore safely:
            margin_usage, account_leverage, in_liquidation, risk_level,
            realized_pnl, usdc_balance, free_collateral, total_account_value,
          }
        """
        empty = {
            "total_equity": "0", "available_balance": "0",
            "unrealized_pnl": "0", "positions": [],
        }
        if not self._account_address:
            return empty

        try:
            portfolio = self._fetch_portfolio_sync()
        except Exception as exc:
            logger.warning(
                "RISEx get_account_summary: portfolio fetch failed (%s) — "
                "falling back to cross-margin-balance only",
                exc,
            )
            return self._cross_margin_fallback()

        summary = portfolio.get("summary") or {}
        raw_positions = portfolio.get("positions") or []

        total_equity = (
            summary.get("total_account_value")
            or summary.get("cross_margin_balance")
            or "0"
        )
        available = (
            summary.get("free_collateral")
            or summary.get("collateral_margin_balance")
            or "0"
        )
        return {
            "total_equity": str(total_equity),
            "available_balance": str(available),
            "unrealized_pnl": str(summary.get("total_unrealized_pnl", "0")),
            "positions": self._normalise_positions(raw_positions),
            # Bonus fields — non-breaking, ignored by /account/all aggregator
            "margin_usage": str(summary.get("margin_usage", "0")),
            "account_leverage": str(summary.get("account_leverage", "0")),
            "in_liquidation": bool(summary.get("in_liquidation", False)),
            "risk_level": str(summary.get("risk_level", "NORMAL")),
            "realized_pnl": str(summary.get("realized_pnl", "0")),
            "usdc_balance": str(summary.get("usdc_balance", "0")),
            "free_collateral": str(summary.get("free_collateral", "0")),
            "total_account_value": str(summary.get("total_account_value", "0")),
        }

    def _cross_margin_fallback(self) -> dict:
        """Minimal balance-only fallback when /portfolio/details fails."""
        try:
            resp = self._session.get(
                f"{self._base_url}/v1/account/cross-margin-balance",
                params={"account": self._account_address},
                timeout=10,
            )
            resp.raise_for_status()
            body = resp.json()
            balance = _extract_balance(body) or "0"
        except Exception as exc:
            logger.warning("RISEx cross-margin-balance fallback also failed: %s", exc)
            balance = "0"
        return {
            "total_equity": balance,
            "available_balance": balance,
            "unrealized_pnl": "0",
            "positions": [],
        }

    # ══════════════════════════════════════════════════════════════════
    # Position normalisation
    # ══════════════════════════════════════════════════════════════════

    def _normalise_positions(self, raw_positions: list[dict], symbols: list[str] | None = None) -> list[dict]:
        """Convert raw API positions to the normalised dict the engine expects.

        Tolerates both schemas the RISEx server uses depending on the endpoint:
          - /v1/portfolio/details → apiPosition (side: int, mark_price + uPnL
            + liquidation_price already present, also `market_name`).
          - /v1/positions         → apiBuilderPosition (side: enum string
            "BUY"/"SELL", missing mark_price/uPnL/liq_price).

        We pass through every richer field that exists, leaving the consumer
        free to pick what it needs without breaking older code paths.
        """
        result = []
        for p in raw_positions:
            try:
                market_id = int(p.get("market_id", 0))
            except (TypeError, ValueError):
                continue
            symbol = self._id_to_symbol.get(market_id, f"MARKET_{market_id}")
            if symbols and symbol not in symbols:
                continue

            try:
                size = Decimal(str(p.get("size", "0")))
            except Exception:
                continue
            if size == 0:
                continue

            # Side may arrive as int (0/1) — apiPosition — or string
            # ("BUY"/"SELL"/"LONG"/"SHORT") — apiBuilderPosition. Default to
            # long when unspecified, leaning conservative.
            raw_side = p.get("side", 0)
            if isinstance(raw_side, str):
                side = "long" if raw_side.upper() in ("0", "BUY", "LONG") else "short"
            else:
                try:
                    side = "long" if int(raw_side) == 0 else "short"
                except (TypeError, ValueError):
                    side = "long"

            # Entry price is `avg_entry_price` in apiPosition; some other
            # response shapes have plain `entry_price`. Try both.
            entry_price = p.get("avg_entry_price") or p.get("entry_price") or "0"

            entry = {
                "symbol": symbol,
                "market_id": market_id,
                "side": side,
                "size": str(abs(size)),
                "entry_price": str(entry_price),
                "mark_price": str(p.get("mark_price", "0")),
                "unrealised_pnl": str(p.get("unrealized_pnl", "0")),
                # Keep both spellings — older code may read 'unrealized_pnl'.
                "unrealized_pnl": str(p.get("unrealized_pnl", "0")),
                "leverage": str(p.get("leverage", "1")),
                "liquidation_price": str(p.get("liquidation_price", "0")),
            }
            # Pass through optional rich fields without forcing them.
            for opt in (
                "market_name", "index_price", "margin_mode",
                "quote_amount", "isolated_usdc_balance",
                "initial_margin_requirement", "maintenance_margin_requirement",
                "margin_balance", "quote_balance", "adl_price",
                "in_isolated_liquidation", "free_isolated_usdc_balance",
                "last_funding_payment", "unsettled_funding",
            ):
                if opt in p:
                    entry[opt] = p[opt]
            result.append(entry)
        return result

    # ══════════════════════════════════════════════════════════════════
    # AsyncExchangeClient protocol (async)
    # ══════════════════════════════════════════════════════════════════

    async def _get_async_session(self):
        """Lazily create and return an httpx.AsyncClient."""
        if not hasattr(self, "_async_session") or self._async_session is None:
            import httpx
            self._async_session = httpx.AsyncClient(
                base_url=self._base_url,
                verify=False,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                timeout=15.0,
            )
        return self._async_session

    async def async_fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        """Async version of fetch_order_book."""
        market_id = self._get_market_id(symbol)
        client = await self._get_async_session()
        resp = await client.get("/v1/orderbook", params={"market_id": market_id, "limit": limit})
        resp.raise_for_status()
        data = resp.json().get("data", {})

        bids = [[b["price"], b["quantity"]] for b in data.get("bids", [])]
        asks = [[a["price"], a["quantity"]] for a in data.get("asks", [])]
        return {"bids": bids, "asks": asks}

    async def async_fetch_markets(self) -> list[dict]:
        """Async version of fetch_markets."""
        client = await self._get_async_session()
        resp = await client.get("/v1/markets")
        resp.raise_for_status()
        data = resp.json().get("data", {})
        markets_raw = data.get("markets", [])

        markets = []
        for m in markets_raw:
            config = m.get("config", {})
            symbol = config.get("name", m.get("display_name", ""))
            market_id = int(m["market_id"])
            base = symbol.split("/")[0] if "/" in symbol else symbol
            markets.append({
                "symbol": symbol,
                "name": symbol,
                "asset": base,
                "market_id": market_id,
                "type": "perp",
                "tick_size": config.get("step_price", "0"),
                "step_size": config.get("step_size", "0"),
                "min_order_size": config.get("min_order_size", "0"),
                "max_leverage": int(config.get("max_leverage", "1")),
                "last_price": m.get("last_price", "0"),
                "mark_price": m.get("mark_price", "0"),
                "index_price": m.get("index_price", "0"),
                "funding_rate_8h": m.get("funding_rate_8h", "0"),
                "open_interest": m.get("open_interest", "0"),
            })
        return markets

    async def async_get_min_order_size(self, symbol: str) -> Decimal:
        return self.get_min_order_size(symbol)

    async def async_get_tick_size(self, symbol: str) -> Decimal:
        return self.get_tick_size(symbol)

    async def async_create_post_only_order(
        self, symbol: str, side: str, amount: Decimal, price: Decimal,
        reduce_only: bool = False,
    ) -> dict:
        """Place a post-only limit order (maker only)."""
        self._require_trading()
        market_id = self._get_market_id(symbol)

        price = self._round_price(symbol, price)
        amount = self._round_qty(symbol, amount)

        # Pre-flight min size check — see create_aggressive_limit_order.
        min_size = self._min_order_size_cache.get(symbol, Decimal("0"))
        if min_size > 0 and amount < min_size:
            raise ValueError(
                f"RISEx {symbol}: order size {amount} below min_order_size {min_size}. "
                f"Increase bot quantity or reduce twap_num_chunks."
            )

        price_ticks = self._to_price_ticks(symbol, price)
        size_steps = self._to_size_steps(symbol, amount)
        side_int = SIDE_LONG if side.lower() == "buy" else SIDE_SHORT

        action_hash = _encode_place_order_hash(
            market_id=market_id,
            size_steps=size_steps,
            price_ticks=price_ticks,
            side=side_int,
            post_only=True,
            reduce_only=reduce_only,
            order_type=ORDER_TYPE_LIMIT,
            time_in_force=TIF_GTC,
        )
        permit = await self._create_permit_async(action_hash)

        payload = {
            "market_id": market_id,
            "side": side_int,
            "order_type": ORDER_TYPE_LIMIT,
            "price_ticks": price_ticks,
            "size_steps": size_steps,
            "time_in_force": TIF_GTC,
            "post_only": True,
            "reduce_only": reduce_only,
            "stp_mode": STP_EXPIRE_MAKER,
            "ttl_units": 0,
            "client_order_id": "0",
            "builder_id": 0,
            "permit": permit,
        }

        client = await self._get_async_session()
        resp = await client.post("/v1/orders/place", json=payload)
        if resp.status_code >= 400:
            _log_risex_http_error("orders/place async POST_ONLY", resp, payload)
        resp.raise_for_status()
        result = resp.json().get("data", {})
        order_id = result.get("order_id", "")
        self._order_market_cache[order_id] = market_id

        logger.info("RISEx POST_ONLY order: %s %s %s @ %s → id=%s", symbol, side, amount, price, order_id)
        return {
            "id": order_id,
            "sc_order_id": result.get("sc_order_id", ""),
            "tx_hash": result.get("tx_hash", ""),
            "symbol": symbol,
            "side": side,
            "price": str(price),
            "amount": str(amount),
            "status": "open",
        }

    async def async_create_ioc_order(
        self, symbol: str, side: str, amount: Decimal, price: Decimal,
        reduce_only: bool = False,
    ) -> dict:
        """Place an IOC (Immediate-or-Cancel) limit order (taker)."""
        self._require_trading()
        market_id = self._get_market_id(symbol)

        price = self._round_price(symbol, price)
        amount = self._round_qty(symbol, amount)

        # Pre-flight min size check — see create_aggressive_limit_order.
        min_size = self._min_order_size_cache.get(symbol, Decimal("0"))
        if min_size > 0 and amount < min_size:
            raise ValueError(
                f"RISEx {symbol}: order size {amount} below min_order_size {min_size}. "
                f"Increase bot quantity or reduce twap_num_chunks."
            )

        price_ticks = self._to_price_ticks(symbol, price)
        size_steps = self._to_size_steps(symbol, amount)
        side_int = SIDE_LONG if side.lower() == "buy" else SIDE_SHORT

        action_hash = _encode_place_order_hash(
            market_id=market_id,
            size_steps=size_steps,
            price_ticks=price_ticks,
            side=side_int,
            post_only=False,
            reduce_only=reduce_only,
            order_type=ORDER_TYPE_LIMIT,
            time_in_force=TIF_IOC,
        )
        permit = await self._create_permit_async(action_hash)

        payload = {
            "market_id": market_id,
            "side": side_int,
            "order_type": ORDER_TYPE_LIMIT,
            "price_ticks": price_ticks,
            "size_steps": size_steps,
            "time_in_force": TIF_IOC,
            "post_only": False,
            "reduce_only": reduce_only,
            "stp_mode": STP_EXPIRE_MAKER,
            "ttl_units": 0,
            "client_order_id": "0",
            "builder_id": 0,
            "permit": permit,
        }

        client = await self._get_async_session()
        resp = await client.post("/v1/orders/place", json=payload)
        if resp.status_code >= 400:
            _log_risex_http_error("orders/place async IOC", resp, payload)
        resp.raise_for_status()
        result = resp.json().get("data", {})
        order_id = result.get("order_id", "")
        self._order_market_cache[order_id] = market_id

        logger.info("RISEx IOC order: %s %s %s @ %s reduce_only=%s → id=%s", symbol, side, amount, price, reduce_only, order_id)
        return {
            "id": order_id,
            "sc_order_id": result.get("sc_order_id", ""),
            "tx_hash": result.get("tx_hash", ""),
            "symbol": symbol,
            "side": side,
            "price": str(price),
            "amount": str(amount),
            "status": "PENDING",
            "traded_qty": 0.0,
        }

    async def async_cancel_order(self, order_id: str) -> bool:
        """Cancel an open order by ID. Returns True if cancelled successfully."""
        self._require_trading()
        market_id = self._order_market_cache.get(order_id)

        # Find the resting_order_id from open orders
        client = await self._get_async_session()
        params: dict[str, Any] = {"account": self._account_address}
        if market_id is not None:
            params["market_id"] = market_id
        resp = await client.get("/v1/orders/open", params=params)
        resp.raise_for_status()
        open_orders = resp.json().get("data", {}).get("orders", [])

        resting_order_id = None
        for o in open_orders:
            if o.get("order_id") == order_id:
                resting_order_id = o.get("resting_order_id")
                if market_id is None:
                    market_id = int(o.get("market_id", 0))
                break

        if resting_order_id is None:
            logger.warning("RISEx cancel: order %s not found in open orders (may already be filled/cancelled)", order_id)
            return False

        if market_id is None:
            logger.error("RISEx cancel: could not determine market_id for order %s", order_id)
            return False

        action_hash = _encode_cancel_order_hash(market_id, int(resting_order_id))
        permit = await self._create_permit_async(action_hash)

        payload = {
            "market_id": market_id,
            "order_id": order_id,
            "permit": permit,
        }

        try:
            resp = await client.post("/v1/orders/cancel", json=payload)
            if resp.status_code >= 400:
                _log_risex_http_error("orders/cancel async", resp, payload)
            resp.raise_for_status()
            result = resp.json().get("data", {})
            logger.info("RISEx order cancelled: %s → %s", order_id, result)
            return True
        except Exception as exc:
            logger.warning("RISEx cancel failed for %s: %s", order_id, exc)
            return False

    async def async_cancel_all_orders(self, symbol: str | None = None) -> bool:
        """Cancel all open orders, optionally filtered by symbol."""
        self._require_trading()
        market_id = self._get_market_id(symbol) if symbol else 0

        action_hash = _encode_cancel_all_hash(market_id)
        permit = await self._create_permit_async(action_hash)

        payload = {
            "market_id": market_id,
            "permit": permit,
        }

        client = await self._get_async_session()
        try:
            resp = await client.post("/v1/orders/cancel-all", json=payload)
            if resp.status_code >= 400:
                _log_risex_http_error("orders/cancel-all async", resp, payload)
            resp.raise_for_status()
            result = resp.json().get("data", {})
            logger.info("RISEx cancel_all: market_id=%s → %s", market_id, result)
            return True
        except Exception as exc:
            logger.warning("RISEx cancel_all failed: %s", exc)
            return False

    async def async_check_order_fill(self, order_id: str) -> dict:
        """Check order fill status (async)."""
        client = await self._get_async_session()
        resp = await client.get("/v1/orders", params={"account": self._account_address, "limit": 50})
        resp.raise_for_status()
        orders = resp.json().get("data", {}).get("orders", [])

        for o in orders:
            if o.get("order_id") == order_id:
                status = o.get("status", "").lower()
                filled_size = Decimal(o.get("filled_size", "0"))
                total_size = Decimal(o.get("size", "0"))
                is_filled = status in ("filled", "completed") or (filled_size > 0 and status == "cancelled")
                return {
                    "filled": is_filled,
                    "status": status,
                    "traded_qty": float(filled_size),
                    "total_qty": float(total_size),
                    "order_id": order_id,
                }

        return {"filled": False, "status": "unknown", "traded_qty": 0.0, "order_id": order_id}

    async def async_fetch_positions(self, symbols: list[str] | None = None) -> list[dict]:
        """Fetch open positions (async) via /v1/portfolio/details.

        Uses the same portfolio endpoint as get_account_summary so the
        position list comes with the rich fields (mark_price, unrealized_pnl,
        liquidation_price) — apiBuilderPosition from /v1/positions does not
        carry those.
        """
        if not self._account_address:
            return []
        try:
            portfolio = await self._fetch_portfolio_async()
        except Exception as exc:
            logger.warning("RISEx async_fetch_positions: portfolio fetch failed — %s", exc)
            return []
        raw = portfolio.get("positions") or []
        return self._normalise_positions(raw, symbols)

    async def async_fetch_funding_rate(self, symbol: str) -> dict:
        """Fetch the current/latest funding rate for a perpetual symbol."""
        market_id = self._get_market_id(symbol)
        client = await self._get_async_session()

        # Markets endpoint includes live funding data
        resp = await client.get("/v1/markets")
        resp.raise_for_status()
        markets = resp.json().get("data", {}).get("markets", [])

        for m in markets:
            if int(m.get("market_id", 0)) == market_id:
                return {
                    "symbol": symbol,
                    "funding_rate": float(m.get("current_funding_rate", "0")),
                    "funding_rate_8h": float(m.get("funding_rate_8h", "0")),
                    "predicted_funding_rate": float(m.get("predicted_funding_rate", "0")),
                    "next_funding_time": m.get("next_funding_time"),
                    "accumulated_funding": m.get("accumulated_funding", "0"),
                }

        return {"symbol": symbol, "funding_rate": 0.0, "next_funding_time": None}

    async def async_set_leverage(self, symbol: str, leverage: int) -> dict:
        """Update leverage for a market (async)."""
        self._require_trading()
        market_id = self._get_market_id(symbol)

        # Leverage is encoded as wei (18 decimals)
        leverage_wei = leverage * (10 ** 18)
        action_hash = _encode_leverage_hash(market_id, leverage_wei)
        permit = await self._create_permit_async(action_hash)

        payload = {
            "market_id": market_id,
            "leverage": str(leverage_wei),
            "permit": permit,
        }

        client = await self._get_async_session()
        resp = await client.post("/v1/account/leverage", json=payload)
        if resp.status_code >= 400:
            _log_risex_http_error("account/leverage async", resp, payload)
        resp.raise_for_status()
        result = resp.json().get("data", {})
        logger.info("RISEx leverage set: %s → %dx result=%s", symbol, leverage, result)
        return {"symbol": symbol, "leverage": leverage, **result}

    async def async_get_open_orders(self, symbol: str | None = None) -> list[dict]:
        """Fetch open orders (async)."""
        client = await self._get_async_session()
        params: dict[str, Any] = {"account": self._account_address}
        if symbol:
            params["market_id"] = self._get_market_id(symbol)
        resp = await client.get("/v1/orders/open", params=params)
        resp.raise_for_status()
        return resp.json().get("data", {}).get("orders", [])

    # ══════════════════════════════════════════════════════════════════
    # WebSocket subscriptions
    # ══════════════════════════════════════════════════════════════════

    async def async_subscribe_fills(self, symbol: str, callback) -> None:
        """Subscribe to real-time fill events via WebSocket.

        Uses the RISEx WS with auth for private fill channel.
        """
        import websockets
        import json

        market_id = self._get_market_id(symbol)

        while True:
            try:
                async with websockets.connect(self._ws_url, close_timeout=5) as ws:
                    # Authenticate
                    nonce = int(time.time() * 1000)
                    expiration = int(time.time()) + 86400  # 24 hours
                    auth_domain = self._get_eip712_domain()

                    auth_message = {
                        "types": {
                            "EIP712Domain": _EIP712_DOMAIN_TYPE,
                            "Register": [
                                {"name": "signer", "type": "address"},
                                {"name": "message", "type": "string"},
                                {"name": "nonce", "type": "uint64"},
                            ],
                        },
                        "primaryType": "Register",
                        "domain": auth_domain,
                        "message": {
                            "signer": self._signer_address,
                            "message": "sign in with RISEx",
                            "nonce": nonce,
                        },
                    }
                    signable = encode_typed_data(full_message=auth_message)
                    signed = Account.sign_message(signable, self._signer_key)
                    auth_sig = "0x" + signed.signature.hex()

                    await ws.send(json.dumps({
                        "method": "auth",
                        "params": {
                            "account": self._account_address,
                            "signer": self._signer_address,
                            "message": "sign in with RISEx",
                            "nonce": nonce,
                            "expiration": expiration,
                            "signature": auth_sig,
                        },
                    }))

                    # Subscribe to fills
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "params": {
                            "channel": "fills",
                            "market_ids": [market_id],
                            "account": self._account_address,
                        },
                    }))

                    logger.info("RISEx WS fills subscribed: %s (market_id=%d)", symbol, market_id)

                    # Heartbeat + message loop
                    async def _heartbeat():
                        while True:
                            await asyncio.sleep(15)
                            try:
                                await ws.send(json.dumps({"op": "ping"}))
                            except Exception:
                                break

                    hb_task = asyncio.create_task(_heartbeat())
                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                                channel = msg.get("channel", "")
                                if channel == "fills":
                                    fill_data = msg.get("data", {})
                                    normalised = {
                                        "order_id": fill_data.get("order_id", ""),
                                        "filled_qty": float(fill_data.get("size", "0")),
                                        "remaining_qty": 0.0,
                                        "price": float(fill_data.get("price", "0")),
                                        "is_taker": True,
                                        "fee": fill_data.get("fee", "0"),
                                        "timestamp": fill_data.get("timestamp", ""),
                                    }
                                    await callback(normalised)
                            except Exception as exc:
                                logger.warning("RISEx WS fill parse error: %s", exc)
                    finally:
                        hb_task.cancel()

            except Exception as exc:
                logger.warning("RISEx WS fills disconnected: %s — reconnecting in 5s", exc)
                await asyncio.sleep(5)

    async def async_subscribe_funding_rate(self, symbol: str, callback) -> None:
        """Subscribe to funding rate updates (polling-based).

        RISEx funding rates are included in market data; we poll every 60s.
        """
        while True:
            try:
                data = await self.async_fetch_funding_rate(symbol)
                await callback(data)
            except Exception as exc:
                logger.warning("RISEx funding rate poll error: %s", exc)
            await asyncio.sleep(60)

    # ══════════════════════════════════════════════════════════════════
    # Account info helpers
    # ══════════════════════════════════════════════════════════════════

    async def async_get_balance(self) -> str:
        """Fetch the account balance string (async) via /v1/portfolio/details.

        Returns total_account_value as the primary balance, falling back to
        cross_margin_balance if the portfolio endpoint is reachable but the
        summary is shaped unexpectedly. On full failure, returns "0" and a
        warning log entry — callers must tolerate "0" gracefully.
        """
        if not self._account_address:
            return "0"
        try:
            portfolio = await self._fetch_portfolio_async()
        except Exception as exc:
            logger.warning(
                "RISEx async_get_balance: portfolio fetch failed (%s) — returning 0",
                exc,
            )
            return "0"
        summary = portfolio.get("summary") or {}
        balance = (
            summary.get("total_account_value")
            or summary.get("cross_margin_balance")
            or "0"
        )
        return str(balance)

    async def async_get_funding_payments(self, limit: int = 50) -> list[dict]:
        """Fetch funding payment history (async)."""
        client = await self._get_async_session()
        resp = await client.get(
            "/v1/account/funding-payments",
            params={"account": self._account_address, "limit": limit},
        )
        resp.raise_for_status()
        return resp.json().get("data", {}).get("payments", [])

    async def async_get_trade_history(self, symbol: str | None = None, limit: int = 50) -> list[dict]:
        """Fetch account trade history (async)."""
        client = await self._get_async_session()
        params: dict[str, Any] = {"account": self._account_address, "limit": limit}
        if symbol:
            params["market_id"] = self._get_market_id(symbol)
        resp = await client.get("/v1/trade-history", params=params)
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return data.get("fills", data.get("trades", []))

    def is_signer_registered(self) -> bool:
        """Check if the configured signer is registered on-chain (sync)."""
        if not self._account_address or not self._signer_address:
            return False
        resp = self._session.get(
            f"{self._base_url}/v1/auth/session-key-status",
            params={"account": self._account_address, "signer": self._signer_address},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json().get("data", {})
        return data.get("status") == 1

    def verify_signer(self) -> dict:
        """Verify the signer registration and return status info."""
        try:
            registered = self.is_signer_registered()
            return {
                "ok": registered,
                "account": self._account_address,
                "signer": self._signer_address,
                "error": None if registered else "Signer not registered — register via RISEx web UI",
            }
        except Exception as exc:
            return {
                "ok": False,
                "account": self._account_address,
                "signer": self._signer_address,
                "error": str(exc),
            }


# ══════════════════════════════════════════════════════════════════
# Module-level helpers
# ══════════════════════════════════════════════════════════════════

def _extract_balance(body: Any) -> str | None:
    """Return the cross-margin balance string from a /v1/account/cross-margin-balance response.

    rise.trade is inconsistent about envelope shape across endpoints. /v1/markets
    wraps in ``{"data": {"markets": [...]}}`` but /v1/account/cross-margin-balance
    is documented (TS SDK ``InfoClient.getBalance``) to return ``{"balance": "..."}``
    at the top level. Some deployments may still wrap. Accept both:

      {"balance": "123.45"}                 → "123.45"
      {"data": {"balance": "123.45"}}       → "123.45"
      anything else                         → None  (caller logs + falls back to "0")
    """
    if not isinstance(body, dict):
        return None
    # Direct hit (TS SDK shape)
    val = body.get("balance")
    if isinstance(val, (str, int, float)):
        return str(val)
    # Wrapped fallback
    inner = body.get("data")
    if isinstance(inner, dict):
        val = inner.get("balance")
        if isinstance(val, (str, int, float)):
            return str(val)
    return None


def _unwrap_data(body: Any) -> Any:
    """Strip the rise.trade ``{data, request_id}`` envelope if present.

    Live testing shows every endpoint actually wraps responses in
    ``{"data": <payload>, "request_id": "..."}`` even though the OpenAPI
    schemas describe only the inner payload. Accepts both shapes so the
    caller doesn't have to think about it.
    """
    if not isinstance(body, dict):
        return body
    if "data" in body and isinstance(body["data"], (dict, list)):
        return body["data"]
    return body


def _log_risex_http_error(operation: str, resp: Any, payload: dict | None = None) -> None:
    """Log structured details of a 4xx/5xx response from rise.trade.

    httpx/requests both swallow the response body before raise_for_status()
    fires, leaving us with a useless ``Client error '400 Bad Request'``.
    Capture the body (truncated to 1k chars) and the safe parts of the
    payload so we can diagnose without leaking the permit signature or
    the signer key. Field whitelist only — never dump the full payload.
    """
    try:
        body = resp.text[:1024]
    except Exception:
        body = "<unreadable>"
    safe: dict[str, Any] = {}
    if payload:
        for k in (
            "market_id", "side", "order_type", "post_only", "reduce_only",
            "price_ticks", "size_steps", "time_in_force", "client_order_id",
            "ttl_units", "stp_mode", "builder_id",
            "order_id", "leverage",
        ):
            if k in payload:
                safe[k] = payload[k]
    logger.error(
        "RISEx %s HTTP %s — body=%r payload=%r",
        operation, getattr(resp, "status_code", "?"), body, safe,
    )
