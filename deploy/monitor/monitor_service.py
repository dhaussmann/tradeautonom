"""Shared Orderbook Monitoring Service (OMS).

Centralizes WebSocket connections to exchange orderbook feeds.
Multiple bot containers can read from this single service instead
of each maintaining their own WS connections (reduces rate-limit pressure).

Endpoints:
  GET /book/{exchange}/{symbol}  — latest orderbook snapshot (JSON)
  GET /health                    — service health check
  GET /status                    — overview of all tracked feeds
  GET /tracked                   — auto-discovered pairs grouped by base token
  GET /nado/symbols              — Nado symbol status (active/inactive)
"""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
import websockets
import yaml
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from websockets.extensions.permessage_deflate import ClientPerMessageDeflateFactory

# Import Nado watchdog modules
try:
    from nado_watchdog import NadoWatchdog, SymbolState, get_watchdog
    from nado_persistence import NadoPersistence, get_persistence
    from nado_discovery import NadoDiscovery, get_discovery
    from nado_websocket import NadoWebSocketManager, get_websocket_manager, handle_nado_book_message
    NADO_WATCHDOG_AVAILABLE = True
except ImportError as e:
    NADO_WATCHDOG_AVAILABLE = False
    print(f"Warning: Nado watchdog modules not available: {e}")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("oms")

app = FastAPI(title="Orderbook Monitor Service", version="1.0.0")

# ── SSL context ──────────────────────────────────────────────────────
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


@dataclass
class BookSnapshot:
    bids: list[list] = field(default_factory=list)
    asks: list[list] = field(default_factory=list)
    timestamp_ms: float = 0.0
    connected: bool = False
    update_count: int = 0


# In-memory cache: (exchange, symbol) → BookSnapshot
_books: dict[tuple[str, str], BookSnapshot] = {}
_tasks: list[asyncio.Task] = []

# WS subscriber registry: WebSocket → set of (exchange, symbol) subscriptions
_ws_subscribers: dict[WebSocket, set[tuple[str, str]]] = {}
# Reverse alias map: per-client mapping from resolved_key → original_key
# so broadcasts can send the symbol alias each client originally subscribed with.
_ws_alias_map: dict[WebSocket, dict[tuple[str, str], tuple[str, str]]] = {}

# ── Exchange WS configs ──────────────────────────────────────────────

_EXTENDED_WS_ALL = "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/orderbooks"

_GRVT_WS_ENDPOINTS = {
    "dev": "wss://market-data.dev.gravitymarkets.io/ws/full",
    "staging": "wss://market-data.stg.gravitymarkets.io/ws/full",
    "testnet": "wss://market-data.testnet.grvt.io/ws/full",
    "prod": "wss://market-data.grvt.io/ws/full",
}

# Configuration via environment
import os

_TRACKED_PAIRS = os.environ.get("OMS_TRACKED_PAIRS", "auto")
_GRVT_ENV = os.environ.get("OMS_GRVT_ENV", "prod")
_NADO_ENV = os.environ.get("OMS_NADO_ENV", "mainnet")
_NADO_GATEWAY = os.environ.get("OMS_NADO_GATEWAY", "")
_MIN_EXCHANGES = int(os.environ.get("OMS_MIN_EXCHANGES", "2"))
_VARIATIONAL_STATS_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"
_EXTENDED_MARKETS_URL = "https://api.starknet.extended.exchange/api/v1/info/markets"
_GRVT_INSTRUMENTS_URL = "https://market-data.grvt.io/full/v1/all_instruments"

# Auto-discovery result cache
_discovered_pairs: dict[str, dict[str, str]] = {}  # base → {exchange: symbol}
_max_leverage: dict[str, dict[str, int]] = {}      # exchange → {base_token: max_leverage}
_min_order_size: dict[str, dict[str, float]] = {}  # exchange → {base_token: min_qty}
_qty_step: dict[str, dict[str, float]] = {}        # exchange → {base_token: step_size}

# ── Arbitrage scanner config ──────────────────────────────────────────
_ARB_SCAN_INTERVAL_S = float(os.environ.get("OMS_ARB_SCAN_INTERVAL_S", "0.2"))
_ARB_MAX_NOTIONAL_USD = float(os.environ.get("OMS_ARB_MAX_NOTIONAL_USD", "50000"))
_ARB_EXCHANGES = set(os.environ.get("OMS_ARB_EXCHANGES", "grvt,extended,nado").split(","))
_ARB_EXCLUDED_TOKENS = set(os.environ.get("OMS_ARB_EXCLUDED_TOKENS", "WTI,MEGA,AMZN,AAPL,TSLA,HOOD,META,USDJPY").split(","))
_TAKER_FEE_PCT = {"extended": 0.0225, "nado": 0.035, "grvt": 0.039}
_ARB_FEE_BUFFER_BPS = float(os.environ.get("OMS_ARB_FEE_BUFFER_BPS", "1.0"))

# ── Nado Watchdog Configuration ───────────────────────────────────────
_nado_config: dict = {}
_nado_watchdog: Any = None
_nado_persistence: Any = None
_nado_discovery: Any = None
_nado_ws_manager: Any = None


def _load_nado_config() -> dict:
    """Load Nado watchdog configuration from YAML file."""
    config_path = "/app/nado_config.yaml"
    try:
        with open(config_path, 'r') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        logger.warning(f"Nado config not found at {config_path}, using defaults")
        return {
            "default_active_symbols": [],
            "watchdog": {
                "suspect_threshold_seconds": 120,
                "retry_max_attempts": 10,
                "retry_base_delay_seconds": 10,
                "retry_max_delay_seconds": 7200,
                "reactivation_check_interval_hours": 24,
                "state_persistence_path": "/app/data/nado_watchdog_state.json",
            },
            "discovery": {
                "interval_hours": 24,
                "test_duration_seconds": 30,
                "nado_api_url": "https://gateway.prod.nado.xyz",
            },
            "websocket": {
                "endpoint": "wss://gateway.prod.nado.xyz/v1/subscribe",
                "subscription_delay_ms": 50,
            },
        }
    except Exception as e:
        logger.error(f"Failed to load Nado config: {e}")
        return {}


# ── Concurrency control for Nado WS connections ───────────────────────
# Nado uses separate WS per symbol; limit concurrent connection attempts
# to avoid overwhelming the server and hitting rate limits.
# Reduced to 2 due to Nado server capacity constraints.
_NADO_WS_SEMAPHORE = asyncio.Semaphore(2)

# ── Concurrency control for GRVT WS connections ───────────────────────
# GRVT also uses separate WS per symbol; limit concurrent connections
# to avoid rate limiting (90 symbols, limit to 10 concurrent).
_GRVT_WS_SEMAPHORE = asyncio.Semaphore(10)


def _min_profit_bps(buy_exch: str, sell_exch: str) -> float:
    """Compute minimum profitable spread in bps for a given exchange pair.

    Formula: (buy_fee + sell_fee) * 2 * 100 + buffer_bps
    Fees are applied on open AND close, on both sides.
    """
    buy_fee = _TAKER_FEE_PCT.get(buy_exch, 0.04)
    sell_fee = _TAKER_FEE_PCT.get(sell_exch, 0.04)
    return (buy_fee + sell_fee) * 2 * 100 + _ARB_FEE_BUFFER_BPS


@dataclass
class ArbOpportunity:
    token: str
    buy_exchange: str
    buy_symbol: str
    sell_exchange: str
    sell_symbol: str
    buy_price_bbo: float
    sell_price_bbo: float
    bbo_spread_bps: float
    buy_fill_vwap: float
    sell_fill_vwap: float
    net_profit_bps: float
    fee_threshold_bps: float
    max_qty: float
    max_notional_usd: float
    timestamp_ms: float
    buy_max_leverage: int = 1
    sell_max_leverage: int = 1
    buy_min_order_size: float = 0.0
    sell_min_order_size: float = 0.0
    buy_qty_step: float = 0.0
    sell_qty_step: float = 0.0


# Current arbitrage opportunities: token → list of ArbOpportunity
_arb_opportunities: dict[str, list[ArbOpportunity]] = {}
# WS clients subscribed to arb broadcasts
_arb_subscribers: set[WebSocket] = set()
# /ws/arb watchers: WS → set of (token, buy_exchange, sell_exchange) being monitored
_arb_watch_subscribers: dict[WebSocket, set[tuple[str, str, str]]] = {}
# /ws/arb opportunity subscribers: WS → filter config (min_profit_bps, exchanges)
_arb_opp_subscribers: dict[WebSocket, dict] = {}

_NADO_GATEWAYS = {
    "mainnet": "https://gateway.prod.nado.xyz",
    "testnet": "https://gateway.sepolia.nado.xyz",
}
_NADO_WS_ENDPOINTS = {
    "mainnet": "wss://gateway.prod.nado.xyz/v1/subscribe",
    "testnet": "wss://gateway.sepolia.nado.xyz/v1/subscribe",
}

# Nado symbol → product_id cache (populated at startup)
_nado_product_ids: dict[str, int] = {}


# ── FastAPI endpoints ────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "feeds": len(_books), "timestamp": time.time()}


@app.get("/status")
def status():
    result = {}
    now_ms = time.time() * 1000
    for (exch, sym), snap in _books.items():
        age_ms = round(now_ms - snap.timestamp_ms) if snap.timestamp_ms else None
        result[f"{exch}:{sym}"] = {
            "connected": snap.connected,
            "has_data": bool(snap.bids and snap.asks),
            "age_ms": age_ms,
            "updates": snap.update_count,
            "bid_levels": len(snap.bids),
            "ask_levels": len(snap.asks),
        }
    return result


# ── Nado Watchdog API Endpoints ───────────────────────────────────────

@app.get("/nado/symbols")
async def nado_symbols():
    """Get status of all Nado symbols managed by watchdog."""
    if not NADO_WATCHDOG_AVAILABLE or _nado_watchdog is None:
        raise HTTPException(status_code=503, detail="Nado watchdog not available")
    
    stats = _nado_watchdog.get_statistics()
    now = time.time()
    
    result = {
        "timestamp": datetime.utcnow().isoformat(),
        "summary": stats,
        "symbols": {},
    }
    
    # Group symbols by state
    for state in SymbolState:
        symbols = _nado_watchdog.get_symbols_by_state(state)
        result["symbols"][state.value] = []
        
        for symbol in symbols:
            info = _nado_watchdog.get_symbol(symbol)
            if info:
                symbol_data = {
                    "symbol": symbol,
                    "product_id": info.product_id,
                    "state": info.state.value,
                    "update_count": info.update_count,
                }
                
                # Add timing info if available
                if info.last_update:
                    seconds_since = now - info.last_update.timestamp()
                    symbol_data["seconds_since_update"] = round(seconds_since, 1)
                
                if info.retry_attempt > 0:
                    symbol_data["retry_attempt"] = info.retry_attempt
                
                result["symbols"][state.value].append(symbol_data)
    
    return result


@app.get("/nado/symbol/{symbol}")
async def nado_symbol_detail(symbol: str):
    """Get detailed information about a specific Nado symbol."""
    if not NADO_WATCHDOG_AVAILABLE or _nado_watchdog is None:
        raise HTTPException(status_code=503, detail="Nado watchdog not available")
    
    info = _nado_watchdog.get_symbol(symbol)
    if not info:
        raise HTTPException(status_code=404, detail=f"Symbol {symbol} not found")
    
    now = time.time()
    
    result = {
        "symbol": symbol,
        "product_id": info.product_id,
        "state": info.state.value,
        "added_at": info.added_at.isoformat() if info.added_at else None,
        "update_count": info.update_count,
    }
    
    # Timing info
    if info.last_update:
        seconds_since = now - info.last_update.timestamp()
        result["last_update"] = info.last_update.isoformat()
        result["seconds_since_update"] = round(seconds_since, 1)
    
    # Retry info
    if info.retry_attempt > 0:
        result["retry"] = {
            "attempt": info.retry_attempt,
            "max_attempts": _nado_watchdog.retry_max_attempts,
        }
        if info.retry_next_at:
            result["retry"]["next_at"] = info.retry_next_at.isoformat()
    
    # Inactive info
    if info.inactive_since:
        result["inactive_since"] = info.inactive_since.isoformat()
    
    # Book data
    key = ("nado", symbol)
    if key in _books:
        book = _books[key]
        result["book"] = {
            "bid_levels": len(book.bids),
            "ask_levels": len(book.asks),
            "has_data": bool(book.bids and book.asks),
            "connected": book.connected,
        }
    
    return result


@app.post("/nado/force-discovery")
async def nado_force_discovery():
    """Manually trigger symbol discovery and testing."""
    if not NADO_WATCHDOG_AVAILABLE or _nado_discovery is None:
        raise HTTPException(status_code=503, detail="Nado discovery not available")
    
    logger.info("Manual discovery triggered via API")
    result = await _nado_discovery.run_full_cycle()
    
    return {
        "success": True,
        "timestamp": datetime.utcnow().isoformat(),
        "result": result,
    }


@app.get("/nado/health")
async def nado_health():
    """Get Nado watchdog system health status."""
    if not NADO_WATCHDOG_AVAILABLE:
        return {
            "available": False,
            "message": "Nado watchdog modules not loaded",
        }
    
    if _nado_watchdog is None:
        return {
            "available": False,
            "message": "Nado watchdog not initialized",
        }
    
    ws_stats = {}
    if _nado_ws_manager:
        ws_stats = _nado_ws_manager.get_connection_stats()
    
    persistence_info = {}
    if _nado_persistence:
        persistence_info = _nado_persistence.get_state_file_info()
    
    return {
        "available": True,
        "timestamp": datetime.utcnow().isoformat(),
        "statistics": _nado_watchdog.get_statistics(),
        "websocket": ws_stats,
        "persistence": persistence_info,
    }


def _resolve_variational_alias(exchange: str, symbol: str) -> tuple[str, str]:
    """Resolve Variational symbol aliases.

    Trading API always uses funding_interval=3600, but Stats API returns the
    real interval (e.g. 28800).  If the exact key is not in _books, try to
    find a matching ticker with a different interval.
    E.g. P-DOT-USDC-3600 → P-DOT-USDC-28800
    """
    key = (exchange, symbol)
    if key in _books:
        return key
    if exchange == "variational" and symbol.startswith("P-"):
        # Extract ticker prefix: P-DOT-USDC
        parts = symbol.rsplit("-", 1)  # ['P-DOT-USDC', '3600']
        if len(parts) == 2:
            prefix = parts[0]  # e.g. 'P-DOT-USDC'
            for (ex, sym) in _books:
                if ex == "variational" and sym.startswith(prefix + "-") and sym != symbol:
                    logger.debug("OMS: alias %s → %s", symbol, sym)
                    return (ex, sym)
    return key


@app.get("/book/{exchange}/{symbol:path}")
def get_book(exchange: str, symbol: str):
    key = _resolve_variational_alias(exchange, symbol)
    snap = _books.get(key)
    if snap is None:
        raise HTTPException(status_code=404, detail=f"No feed for {exchange}:{symbol}")
    return {
        "exchange": exchange,
        "symbol": symbol,
        "bids": snap.bids[:20],
        "asks": snap.asks[:20],
        "timestamp_ms": snap.timestamp_ms,
        "connected": snap.connected,
        "updates": snap.update_count,
    }


@app.get("/tracked")
def tracked():
    return _discovered_pairs


# ── Arbitrage scanner ──────────────────────────────────────────────


def _estimate_fill_price(levels: list[list], qty: float) -> float:
    """Walk orderbook levels and return VWAP fill price for given quantity."""
    remaining = qty
    total_cost = 0.0
    for price, size in levels:
        p = float(price)
        s = float(size)
        fill = min(remaining, s)
        total_cost += fill * p
        remaining -= fill
        if remaining <= 0:
            break
    filled = qty - remaining
    return total_cost / filled if filled > 0 else 0.0


def _binary_search_arb_qty(
    buy_book: dict,
    sell_book: dict,
    mid_price: float,
    upper_notional: float,
    min_profit_bps: float,
    min_qty: float = 0.001,
    iterations: int = 12,
) -> tuple[float, float, float]:
    """Find the maximum quantity where cross-venue arb profit > min_profit_bps.

    Returns (max_qty, buy_fill_vwap, sell_fill_vwap).
    """
    if mid_price <= 0:
        return 0.0, 0.0, 0.0

    hi = upper_notional / mid_price
    lo = min_qty

    if hi <= lo:
        return 0.0, 0.0, 0.0

    best_qty = 0.0
    best_buy = 0.0
    best_sell = 0.0

    for _ in range(iterations):
        mid = (lo + hi) / 2.0
        buy_fill = _estimate_fill_price(buy_book.get("asks", []), mid)
        sell_fill = _estimate_fill_price(sell_book.get("bids", []), mid)

        if buy_fill <= 0 or sell_fill <= 0:
            hi = mid
            continue

        profit_bps = (sell_fill - buy_fill) / buy_fill * 10000
        if profit_bps >= min_profit_bps:
            best_qty = mid
            best_buy = buy_fill
            best_sell = sell_fill
            lo = mid  # can go bigger
        else:
            hi = mid  # too much slippage, go smaller

    return best_qty, best_buy, best_sell


def _find_arb_for_token(token: str, exchange_map: dict[str, str], override_min_bps: float | None = None) -> list[ArbOpportunity]:
    """Check all exchange pairs for a token and return actionable arb opportunities."""
    now_ms = time.time() * 1000
    opps: list[ArbOpportunity] = []

    # Filter to arb-eligible exchanges with live data
    eligible: list[tuple[str, str, dict]] = []
    for exch, sym in exchange_map.items():
        if exch not in _ARB_EXCHANGES:
            continue
        snap = _books.get((exch, sym))
        if snap is None or not snap.bids or not snap.asks or not snap.connected:
            continue
        book = {"bids": snap.bids, "asks": snap.asks}
        eligible.append((exch, sym, book))

    if len(eligible) < 2:
        return opps

    # Compare all pairs
    for i in range(len(eligible)):
        for j in range(i + 1, len(eligible)):
            exch_a, sym_a, book_a = eligible[i]
            exch_b, sym_b, book_b = eligible[j]

            best_bid_a = float(book_a["bids"][0][0])
            best_ask_a = float(book_a["asks"][0][0])
            best_bid_b = float(book_b["bids"][0][0])
            best_ask_b = float(book_b["asks"][0][0])

            # Check both directions: buy A sell B, and buy B sell A
            for buy_exch, buy_sym, buy_book, buy_ask, sell_exch, sell_sym, sell_book, sell_bid in [
                (exch_a, sym_a, book_a, best_ask_a, exch_b, sym_b, book_b, best_bid_b),
                (exch_b, sym_b, book_b, best_ask_b, exch_a, sym_a, book_a, best_bid_a),
            ]:
                if sell_bid <= buy_ask:
                    continue  # no BBO arb

                bbo_spread_bps = (sell_bid - buy_ask) / buy_ask * 10000
                mid_price = (buy_ask + sell_bid) / 2.0

                # Pair-specific fee threshold
                full_fee_bps = _min_profit_bps(buy_exch, sell_exch)
                pair_min_bps = override_min_bps if override_min_bps is not None else full_fee_bps

                # Binary search for max executable quantity
                max_qty, buy_vwap, sell_vwap = _binary_search_arb_qty(
                    buy_book=buy_book,
                    sell_book=sell_book,
                    mid_price=mid_price,
                    upper_notional=_ARB_MAX_NOTIONAL_USD,
                    min_profit_bps=pair_min_bps,
                )

                if max_qty <= 0:
                    continue  # insufficient depth for profitable arb

                net_profit_bps = (sell_vwap - buy_vwap) / buy_vwap * 10000
                max_notional = max_qty * mid_price

                buy_lev = _max_leverage.get(buy_exch, {}).get(token, 1)
                sell_lev = _max_leverage.get(sell_exch, {}).get(token, 1)
                buy_min = _min_order_size.get(buy_exch, {}).get(token, 0.0)
                sell_min = _min_order_size.get(sell_exch, {}).get(token, 0.0)
                buy_step = _qty_step.get(buy_exch, {}).get(token, 0.0)
                sell_step = _qty_step.get(sell_exch, {}).get(token, 0.0)

                opps.append(ArbOpportunity(
                    token=token,
                    buy_exchange=buy_exch,
                    buy_symbol=buy_sym,
                    sell_exchange=sell_exch,
                    sell_symbol=sell_sym,
                    buy_price_bbo=buy_ask,
                    sell_price_bbo=sell_bid,
                    bbo_spread_bps=round(bbo_spread_bps, 2),
                    buy_fill_vwap=round(buy_vwap, 6),
                    sell_fill_vwap=round(sell_vwap, 6),
                    net_profit_bps=round(net_profit_bps, 2),
                    fee_threshold_bps=round(full_fee_bps, 2),
                    max_qty=round(max_qty, 6),
                    max_notional_usd=round(max_notional, 2),
                    timestamp_ms=now_ms,
                    buy_max_leverage=buy_lev,
                    sell_max_leverage=sell_lev,
                    buy_min_order_size=buy_min,
                    sell_min_order_size=sell_min,
                    buy_qty_step=buy_step,
                    sell_qty_step=sell_step,
                ))

    return opps


async def _scan_arbitrage() -> None:
    """Background task: continuously scan for cross-exchange arb opportunities."""
    logger.info("OMS: Arbitrage scanner started (interval=%.1fs, fees=%s, buffer=%.1fbps, exchanges=%s)",
                _ARB_SCAN_INTERVAL_S, _TAKER_FEE_PCT, _ARB_FEE_BUFFER_BPS, ",".join(sorted(_ARB_EXCHANGES)))

    while True:
        try:
            all_opps: dict[str, list[ArbOpportunity]] = {}
            new_broadcasts: list[ArbOpportunity] = []

            for token, exchange_map in _discovered_pairs.items():
                if token in _ARB_EXCLUDED_TOKENS:
                    continue
                opps = _find_arb_for_token(token, exchange_map)
                if opps:
                    all_opps[token] = opps

            # Detect new/changed opportunities for WS broadcast
            prev_keys = set()
            for token_opps in _arb_opportunities.values():
                for o in token_opps:
                    prev_keys.add((o.token, o.buy_exchange, o.sell_exchange))

            for token, opps in all_opps.items():
                for o in opps:
                    k = (o.token, o.buy_exchange, o.sell_exchange)
                    if k not in prev_keys:
                        new_broadcasts.append(o)

            _arb_opportunities.clear()
            _arb_opportunities.update(all_opps)

            # Broadcast new opportunities (legacy subscribe_arb on /ws)
            if new_broadcasts and _arb_subscribers:
                for opp in new_broadcasts:
                    payload = {"type": "arb", **_arb_opp_to_dict(opp)}
                    for ws in list(_arb_subscribers):
                        asyncio.create_task(_safe_ws_send(ws, payload))

            # Broadcast ALL current opportunities to /ws/arb subscribers
            if _arb_opp_subscribers:
                all_opp_list = []
                for token_opps in all_opps.values():
                    all_opp_list.extend(token_opps)
                for ws, filt in list(_arb_opp_subscribers.items()):
                    min_bps = filt.get("min_profit_bps")
                    exchanges = filt.get("exchanges")
                    for opp in all_opp_list:
                        if exchanges:
                            if opp.buy_exchange not in exchanges or opp.sell_exchange not in exchanges:
                                continue
                        if min_bps is not None and opp.net_profit_bps < min_bps:
                            continue
                        payload = {"type": "arb_opportunity", **_arb_opp_to_dict(opp)}
                        asyncio.create_task(_safe_ws_send(ws, payload))

            total = sum(len(v) for v in _arb_opportunities.values())
            if total > 0:
                logger.debug("OMS: Arb scan — %d opportunities across %d tokens", total, len(_arb_opportunities))

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("OMS: Arb scanner error: %s", exc)

        await asyncio.sleep(_ARB_SCAN_INTERVAL_S)


def _arb_opp_to_dict(opp: ArbOpportunity) -> dict:
    """Convert ArbOpportunity to a JSON-serializable dict."""
    return {
        "token": opp.token,
        "buy_exchange": opp.buy_exchange,
        "buy_symbol": opp.buy_symbol,
        "sell_exchange": opp.sell_exchange,
        "sell_symbol": opp.sell_symbol,
        "buy_price_bbo": opp.buy_price_bbo,
        "sell_price_bbo": opp.sell_price_bbo,
        "bbo_spread_bps": opp.bbo_spread_bps,
        "buy_fill_vwap": opp.buy_fill_vwap,
        "sell_fill_vwap": opp.sell_fill_vwap,
        "net_profit_bps": opp.net_profit_bps,
        "fee_threshold_bps": opp.fee_threshold_bps,
        "max_qty": opp.max_qty,
        "max_notional_usd": opp.max_notional_usd,
        "timestamp_ms": opp.timestamp_ms,
        "buy_max_leverage": opp.buy_max_leverage,
        "sell_max_leverage": opp.sell_max_leverage,
        "buy_min_order_size": opp.buy_min_order_size,
        "sell_min_order_size": opp.sell_min_order_size,
        "buy_qty_step": opp.buy_qty_step,
        "sell_qty_step": opp.sell_qty_step,
    }


@app.get("/arb/opportunities")
def arb_opportunities(token: str | None = None, min_profit_bps: float | None = None):
    """Return current arbitrage opportunities, optionally filtered by token.

    If min_profit_bps is specified, opportunities are re-scanned on-the-fly
    using that threshold instead of the default full-fee threshold.
    This allows DNA bots to request opportunities at lower thresholds
    (e.g. half-neutral or custom spread modes).
    """
    if min_profit_bps is not None:
        # Live re-scan with custom threshold
        result = []
        for t, exchange_map in _discovered_pairs.items():
            if t in _ARB_EXCLUDED_TOKENS:
                continue
            if token and t != token.upper():
                continue
            opps = _find_arb_for_token(t, exchange_map, override_min_bps=min_profit_bps)
            result.extend(_arb_opp_to_dict(o) for o in opps)
        result.sort(key=lambda x: -x["net_profit_bps"])
        return result

    if token:
        opps = _arb_opportunities.get(token.upper(), [])
        return [_arb_opp_to_dict(o) for o in opps]
    result = []
    for token_opps in _arb_opportunities.values():
        result.extend(_arb_opp_to_dict(o) for o in token_opps)
    result.sort(key=lambda x: -x["net_profit_bps"])
    return result


@app.get("/arb/config")
def arb_config():
    """Return current arbitrage scanner configuration."""
    return {
        "scan_interval_s": _ARB_SCAN_INTERVAL_S,
        "max_notional_usd": _ARB_MAX_NOTIONAL_USD,
        "exchanges": sorted(_ARB_EXCHANGES),
        "taker_fees_pct": _TAKER_FEE_PCT,
        "fee_buffer_bps": _ARB_FEE_BUFFER_BPS,
        "min_profit_bps": {
            f"{a}_{b}": round(_min_profit_bps(a, b), 1)
            for a in sorted(_ARB_EXCHANGES) for b in sorted(_ARB_EXCHANGES) if a < b
        },
        "tokens_tracked": len(_discovered_pairs),
        "active_opportunities": sum(len(v) for v in _arb_opportunities.values()),
    }


# ── WebSocket broadcast ──────────────────────────────────────────────

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """WebSocket endpoint for real-time orderbook streaming.

    Protocol (JSON):
      Bot → OMS:  {"action": "subscribe",   "exchange": "extended", "symbol": "SOL-USD"}
      Bot → OMS:  {"action": "unsubscribe", "exchange": "extended", "symbol": "SOL-USD"}
      OMS → Bot:  {"type": "book", "exchange": "...", "symbol": "...", "bids": [...], "asks": [...], "timestamp_ms": ...}
    """
    await ws.accept()
    _ws_subscribers[ws] = set()
    _ws_alias_map[ws] = {}
    client_id = id(ws)
    logger.info("OMS WS: client %d connected (total: %d)", client_id, len(_ws_subscribers))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"error": "invalid JSON"})
                continue

            action = msg.get("action", "")
            exch = msg.get("exchange", "")
            sym = msg.get("symbol", "")
            key = (exch, sym)

            if action == "subscribe" and exch and sym:
                resolved_key = _resolve_variational_alias(exch, sym)
                _ws_subscribers[ws].add(resolved_key)
                if resolved_key != key:
                    _ws_alias_map[ws][resolved_key] = key
                    logger.info("OMS WS: client %d subscribed %s:%s (alias → %s:%s)", client_id, exch, sym, *resolved_key)
                else:
                    logger.info("OMS WS: client %d subscribed %s:%s", client_id, exch, sym)
                # Send initial snapshot immediately
                snap = _books.get(resolved_key)
                if snap and (snap.bids or snap.asks):
                    await ws.send_json({
                        "type": "book",
                        "exchange": exch,
                        "symbol": sym,
                        "bids": snap.bids[:20],
                        "asks": snap.asks[:20],
                        "timestamp_ms": snap.timestamp_ms,
                    })
                else:
                    await ws.send_json({"type": "subscribed", "exchange": exch, "symbol": sym, "has_data": False})

            elif action == "unsubscribe" and exch and sym:
                _ws_subscribers[ws].discard(key)
                logger.info("OMS WS: client %d unsubscribed %s:%s", client_id, exch, sym)

            elif action == "subscribe_arb":
                _arb_subscribers.add(ws)
                logger.info("OMS WS: client %d subscribed to arb alerts", client_id)
                # Send current opportunities as initial snapshot
                for token_opps in _arb_opportunities.values():
                    for opp in token_opps:
                        await ws.send_json({"type": "arb", **_arb_opp_to_dict(opp)})

            elif action == "unsubscribe_arb":
                _arb_subscribers.discard(ws)
                logger.info("OMS WS: client %d unsubscribed from arb alerts", client_id)

            else:
                await ws.send_json({"error": f"unknown action: {action}"})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("OMS WS: client %d error: %s", client_id, exc)
    finally:
        _ws_subscribers.pop(ws, None)
        _ws_alias_map.pop(ws, None)
        _arb_subscribers.discard(ws)
        logger.info("OMS WS: client %d disconnected (remaining: %d)", client_id, len(_ws_subscribers))


@app.websocket("/ws/arb")
async def ws_arb_endpoint(ws: WebSocket):
    """WebSocket endpoint for arb position monitoring AND opportunity streaming.

    Protocol (JSON):
      Client → OMS:  {"action": "watch",   "token": "SOL", "buy_exchange": "extended", "sell_exchange": "grvt"}
      Client → OMS:  {"action": "unwatch", "token": "SOL", "buy_exchange": "extended", "sell_exchange": "grvt"}
      Client → OMS:  {"action": "subscribe_opportunities", "min_profit_bps": 0, "exchanges": ["extended", "nado"]}
      Client → OMS:  {"action": "unsubscribe_opportunities"}
      OMS → Client:  {"type": "arb_opportunity", "token": "...", "net_profit_bps": ..., ...}  (every scan cycle)
      OMS → Client:  {"type": "arb_status", "token": "...", "spread_bps": ..., "fee_threshold_bps": ..., "profitable": true, ...}
      OMS → Client:  {"type": "arb_close",  "token": "...", "spread_bps": ..., "fee_threshold_bps": ..., "profitable": false, "reason": "spread_below_fees", ...}
    """
    await ws.accept()
    _arb_watch_subscribers[ws] = set()
    client_id = id(ws)
    logger.info("OMS WS/arb: client %d connected (total watchers: %d)", client_id, len(_arb_watch_subscribers))

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"error": "invalid JSON"})
                continue

            action = msg.get("action", "")
            token = msg.get("token", "")
            buy_exch = msg.get("buy_exchange", "")
            sell_exch = msg.get("sell_exchange", "")

            if action == "watch" and token and buy_exch and sell_exch:
                key = (token, buy_exch, sell_exch)
                _arb_watch_subscribers[ws].add(key)
                logger.info("OMS WS/arb: client %d watching %s %s→%s", client_id, token, buy_exch, sell_exch)

                # Send immediate snapshot
                exch_map = _discovered_pairs.get(token, {})
                buy_sym = exch_map.get(buy_exch)
                sell_sym = exch_map.get(sell_exch)
                if buy_sym and sell_sym:
                    buy_snap = _books.get((buy_exch, buy_sym))
                    sell_snap = _books.get((sell_exch, sell_sym))
                    if buy_snap and sell_snap and buy_snap.asks and sell_snap.bids:
                        buy_ask = float(buy_snap.asks[0][0])
                        sell_bid = float(sell_snap.bids[0][0])
                        spread_bps = (sell_bid - buy_ask) / buy_ask * 10000 if buy_ask > 0 else 0.0
                        threshold = _min_profit_bps(buy_exch, sell_exch)
                        await ws.send_json({
                            "type": "arb_status",
                            "token": token,
                            "buy_exchange": buy_exch,
                            "sell_exchange": sell_exch,
                            "buy_ask": buy_ask,
                            "sell_bid": sell_bid,
                            "spread_bps": round(spread_bps, 2),
                            "fee_threshold_bps": round(threshold, 1),
                            "profitable": spread_bps >= threshold,
                            "timestamp_ms": time.time() * 1000,
                        })
                    else:
                        await ws.send_json({"type": "watching", "token": token, "buy_exchange": buy_exch, "sell_exchange": sell_exch, "has_data": False})
                else:
                    await ws.send_json({"type": "watching", "token": token, "buy_exchange": buy_exch, "sell_exchange": sell_exch, "has_data": False})

            elif action == "unwatch" and token and buy_exch and sell_exch:
                key = (token, buy_exch, sell_exch)
                _arb_watch_subscribers[ws].discard(key)
                logger.info("OMS WS/arb: client %d unwatched %s %s→%s", client_id, token, buy_exch, sell_exch)

            elif action == "subscribe_opportunities":
                filt = {}
                if "min_profit_bps" in msg:
                    filt["min_profit_bps"] = float(msg["min_profit_bps"])
                if "exchanges" in msg and isinstance(msg["exchanges"], list):
                    filt["exchanges"] = set(msg["exchanges"])
                _arb_opp_subscribers[ws] = filt
                logger.info("OMS WS/arb: client %d subscribed to opportunities (filter=%s)", client_id, filt)
                # Send immediate snapshot of current opportunities
                for token_opps in _arb_opportunities.values():
                    for opp in token_opps:
                        if filt.get("exchanges") and (opp.buy_exchange not in filt["exchanges"] or opp.sell_exchange not in filt["exchanges"]):
                            continue
                        if filt.get("min_profit_bps") is not None and opp.net_profit_bps < filt["min_profit_bps"]:
                            continue
                        await ws.send_json({"type": "arb_opportunity", **_arb_opp_to_dict(opp)})

            elif action == "unsubscribe_opportunities":
                _arb_opp_subscribers.pop(ws, None)
                logger.info("OMS WS/arb: client %d unsubscribed from opportunities", client_id)

            else:
                await ws.send_json({"error": f"unknown action: {action}"})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("OMS WS/arb: client %d error: %s", client_id, exc)
    finally:
        _arb_watch_subscribers.pop(ws, None)
        _arb_opp_subscribers.pop(ws, None)
        logger.info("OMS WS/arb: client %d disconnected (remaining: %d watchers, %d opp_subs)",
                    client_id, len(_arb_watch_subscribers), len(_arb_opp_subscribers))


def _notify_subscribers(key: tuple[str, str]) -> None:
    """Fire-and-forget broadcast of a book update to all subscribed WS clients."""
    if not _ws_subscribers:
        return
    snap = _books.get(key)
    if snap is None:
        return
    bids = snap.bids[:20]
    asks = snap.asks[:20]
    ts = snap.timestamp_ms
    for ws, subs in list(_ws_subscribers.items()):
        if key in subs:
            # Remap symbol back to the alias this client subscribed with
            alias = _ws_alias_map.get(ws, {}).get(key, key)
            payload = {
                "type": "book",
                "exchange": alias[0],
                "symbol": alias[1],
                "bids": bids,
                "asks": asks,
                "timestamp_ms": ts,
            }
            asyncio.create_task(_safe_ws_send(ws, payload))

    # Real-time arb watcher notifications
    _notify_arb_watchers(key)


def _notify_arb_watchers(updated_key: tuple[str, str]) -> None:
    """Check if a book update affects any watched arb positions and notify in real-time."""
    if not _arb_watch_subscribers:
        return
    exch_updated, sym_updated = updated_key
    now_ms = time.time() * 1000

    for ws, watched in list(_arb_watch_subscribers.items()):
        for token, buy_exch, sell_exch in list(watched):
            # Only fire if this book update is relevant to this watched position
            exch_map = _discovered_pairs.get(token, {})
            buy_sym = exch_map.get(buy_exch)
            sell_sym = exch_map.get(sell_exch)
            if not buy_sym or not sell_sym:
                continue
            if (exch_updated, sym_updated) != (buy_exch, buy_sym) and \
               (exch_updated, sym_updated) != (sell_exch, sell_sym):
                continue  # update not relevant to this position

            buy_snap = _books.get((buy_exch, buy_sym))
            sell_snap = _books.get((sell_exch, sell_sym))
            if not buy_snap or not sell_snap or not buy_snap.asks or not sell_snap.bids:
                continue

            buy_ask = float(buy_snap.asks[0][0])
            sell_bid = float(sell_snap.bids[0][0])
            spread_bps = (sell_bid - buy_ask) / buy_ask * 10000 if buy_ask > 0 else 0.0
            threshold = _min_profit_bps(buy_exch, sell_exch)
            profitable = spread_bps >= threshold

            msg_type = "arb_status" if profitable else "arb_close"
            payload = {
                "type": msg_type,
                "token": token,
                "buy_exchange": buy_exch,
                "sell_exchange": sell_exch,
                "buy_ask": buy_ask,
                "sell_bid": sell_bid,
                "spread_bps": round(spread_bps, 2),
                "fee_threshold_bps": round(threshold, 1),
                "profitable": profitable,
                "timestamp_ms": now_ms,
            }
            if not profitable:
                payload["reason"] = "spread_below_fees"
            asyncio.create_task(_safe_ws_send(ws, payload))


async def _safe_ws_send(ws: WebSocket, payload: dict) -> None:
    """Send to a WS client, removing it on failure."""
    try:
        await ws.send_json(payload)
    except Exception:
        _ws_subscribers.pop(ws, None)
        _arb_watch_subscribers.pop(ws, None)
        _arb_opp_subscribers.pop(ws, None)


# ── Auto-discovery ─────────────────────────────────────────────────

async def _discover_pairs() -> list[tuple[str, str]]:
    """Fetch market lists from all 4 exchanges, normalise base tokens,
    and return pairs that appear on >= _MIN_EXCHANGES exchanges."""
    exchange_maps: dict[str, dict[str, str]] = {
        "extended": {}, "grvt": {}, "nado": {}, "variational": {},
    }
    lev_extended: dict[str, int] = {}
    lev_grvt: dict[str, int] = {}
    lev_nado: dict[str, int] = {}
    mins_extended: dict[str, float] = {}
    mins_grvt: dict[str, float] = {}
    mins_nado: dict[str, float] = {}
    step_extended: dict[str, float] = {}
    step_grvt: dict[str, float] = {}
    step_nado: dict[str, float] = {}

    async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
        # Extended
        try:
            resp = await client.get(_EXTENDED_MARKETS_URL)
            resp.raise_for_status()
            for m in resp.json().get("data", []):
                if m.get("status") != "ACTIVE":
                    continue
                name = m["name"]
                base = name.split("-")[0].upper()
                if "_" in base:
                    continue  # skip equity tokens like AAPL_24_5
                if base.startswith("1000"):
                    base = base[4:]
                exchange_maps["extended"][base] = name
                tc = m.get("tradingConfig", {})
                ml = tc.get("maxLeverage")
                if ml:
                    lev_extended[base] = int(float(ml))
                ms = tc.get("minOrderSize")
                if ms:
                    mins_extended[base] = float(ms)
                qs = tc.get("minOrderSizeChange")
                if qs:
                    step_extended[base] = float(qs)
            logger.info("OMS: Extended discovery: %d markets", len(exchange_maps["extended"]))
        except Exception as exc:
            logger.error("OMS: Extended discovery failed: %s", exc)

        # GRVT
        try:
            resp = await client.post(
                _GRVT_INSTRUMENTS_URL,
                json={"is_active": True},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
            for i in resp.json().get("result", []):
                inst = i.get("instrument", "")
                base = i.get("base", "").upper()
                if base:
                    exchange_maps["grvt"][base] = inst
                    lev_grvt[base] = 10  # GRVT perps max 10x
                    ms = i.get("min_size")
                    if ms:
                        mins_grvt[base] = float(ms)
                        step_grvt[base] = float(ms)  # GRVT: min_size == qty step
            logger.info("OMS: GRVT discovery: %d instruments", len(exchange_maps["grvt"]))
        except Exception as exc:
            logger.error("OMS: GRVT discovery failed: %s", exc)

        # Nado
        try:
            resp = await client.get(
                (_NADO_GATEWAY or _NADO_GATEWAYS.get(_NADO_ENV, _NADO_GATEWAYS["mainnet"])) + "/symbols",
                headers={"Accept-Encoding": "gzip"},
            )
            resp.raise_for_status()
            for s in resp.json():
                sym = s["symbol"]
                if not sym.endswith("-PERP"):
                    continue
                base = sym.replace("-PERP", "").upper()
                if base.startswith("K"):
                    base = base[1:]  # kBONK → BONK, kPEPE → PEPE
                exchange_maps["nado"][base] = sym
                _nado_product_ids[sym] = s["product_id"]
                ml = s.get("max_leverage") or s.get("maxLeverage")
                lev_nado[base] = int(float(ml)) if ml else 20  # fallback 20x
                si = s.get("size_increment", "0")
                if si and si != "0":
                    step_nado[base] = float(si) / 1e18
                    mins_nado[base] = float(si) / 1e18  # use step as min
                ms_raw = s.get("min_size", "0")
                if ms_raw and ms_raw != "0":
                    mins_nado[base] = float(ms_raw) / 1e18  # USD notional → stored raw
            logger.info("OMS: Nado discovery: %d perps", len(exchange_maps["nado"]))
        except Exception as exc:
            logger.error("OMS: Nado discovery failed: %s", exc)

        # Variational
        try:
            resp = await client.get(_VARIATIONAL_STATS_URL)
            resp.raise_for_status()
            for listing in resp.json().get("listings", []):
                ticker = listing.get("ticker", "").upper()
                fi = listing.get("funding_interval_s", 3600)
                sym = f"P-{ticker}-USDC-{fi}"
                exchange_maps["variational"][ticker] = sym
            logger.info("OMS: Variational discovery: %d listings", len(exchange_maps["variational"]))
        except Exception as exc:
            logger.error("OMS: Variational discovery failed: %s", exc)

    # Find overlap
    all_bases = set()
    for m in exchange_maps.values():
        all_bases |= set(m.keys())

    pairs: list[tuple[str, str]] = []
    for base in sorted(all_bases):
        found = {}
        for exch, m in exchange_maps.items():
            if base in m:
                found[exch] = m[base]
        if len(found) >= _MIN_EXCHANGES:
            _discovered_pairs[base] = found
            for exch, sym in found.items():
                pairs.append((exch, sym))

    # Populate global caches
    _max_leverage["extended"] = lev_extended
    _max_leverage["grvt"] = lev_grvt
    _max_leverage["nado"] = lev_nado
    _min_order_size["extended"] = mins_extended
    _min_order_size["grvt"] = mins_grvt
    _min_order_size["nado"] = mins_nado
    _qty_step["extended"] = step_extended
    _qty_step["grvt"] = step_grvt
    _qty_step["nado"] = step_nado
    logger.info(
        "OMS: Auto-discovery complete — %d base tokens on >= %d exchanges, %d total feeds, "
        "leverage: ext=%d grvt=%d nado=%d, min_sizes: ext=%d grvt=%d nado=%d",
        len(_discovered_pairs), _MIN_EXCHANGES, len(pairs),
        len(lev_extended), len(lev_grvt), len(lev_nado),
        len(mins_extended), len(mins_grvt), len(mins_nado),
    )
    return pairs


# ── WS feed management ──────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    # Determine pairs to track
    if _TRACKED_PAIRS.strip().lower() in ("", "auto"):
        logger.info("OMS: Auto-discovery mode (min_exchanges=%d)", _MIN_EXCHANGES)
        pairs = await _discover_pairs()
    else:
        pairs = []
        for pair_str in _TRACKED_PAIRS.split(","):
            pair_str = pair_str.strip()
            if not pair_str or ":" not in pair_str:
                continue
            exch, sym = pair_str.split(":", 1)
            pairs.append((exch, sym))

    # Group variational symbols for single shared poll
    variational_symbols: list[str] = []
    grvt_symbols: list[str] = []
    nado_symbols: list[str] = []
    extended_started = False

    for exch, sym in pairs:
        key = (exch, sym)
        _books[key] = BookSnapshot()

        if exch == "extended":
            if not extended_started:
                # Single shared WS connection for all Extended markets
                task = asyncio.create_task(_run_extended_ws_all(), name="ws-extended-all")
                _tasks.append(task)
                logger.info("OMS: started single shared Extended WS feed")
                extended_started = True
            continue  # book entry already created above
        elif exch == "grvt":
            grvt_symbols.append(sym)
            continue  # started as single shared task below
        elif exch == "nado":
            nado_symbols.append(sym)
            continue  # started as single shared task below
        elif exch == "variational":
            variational_symbols.append(sym)
            continue  # started as single shared task below
        else:
            logger.warning("OMS: unknown exchange '%s', skipping", exch)
            continue

    # Single shared GRVT WS connection for all symbols
    if grvt_symbols:
        task = asyncio.create_task(_run_grvt_ws_all(grvt_symbols), name="ws-grvt-all")
        _tasks.append(task)
        logger.info("OMS: started single shared GRVT WS feed for %d symbols", len(grvt_symbols))

    # Single shared Nado WS connection for all symbols
    # NOTE: New watchdog-based implementation - see below
    # Legacy implementation kept for fallback
    if nado_symbols and not NADO_WATCHDOG_AVAILABLE:
        task = asyncio.create_task(_run_nado_ws_all(nado_symbols), name="ws-nado-all")
        _tasks.append(task)
        logger.info("OMS: started single shared Nado WS feed for %d symbols", len(nado_symbols))

    # Single shared Variational poll for all symbols
    if variational_symbols:
        task = asyncio.create_task(
            _run_variational_poll_all(variational_symbols),
            name="poll-variational-all",
        )
        _tasks.append(task)
        logger.info("OMS: started shared Variational poll for %d symbols", len(variational_symbols))

    # Initialize Nado Watchdog System
    if NADO_WATCHDOG_AVAILABLE:
        await _init_nado_watchdog()

    # Start arbitrage scanner (delay 10s to let feeds connect first)
    async def _delayed_arb_start():
        await asyncio.sleep(10)
        await _scan_arbitrage()
    arb_task = asyncio.create_task(_delayed_arb_start(), name="arb-scanner")
    _tasks.append(arb_task)
    logger.info("OMS: arbitrage scanner scheduled (starts in 10s)")


async def _init_nado_watchdog() -> None:
    """Initialize Nado watchdog system with dynamic symbol management."""
    global _nado_config, _nado_watchdog, _nado_persistence, _nado_discovery, _nado_ws_manager
    
    logger.info("OMS: Initializing Nado Watchdog System")
    
    # Load configuration
    _nado_config = _load_nado_config()
    wd_config = _nado_config.get("watchdog", {})
    disc_config = _nado_config.get("discovery", {})
    ws_config = _nado_config.get("websocket", {})
    
    # Initialize watchdog directly (not via singleton getter to allow config)
    from nado_watchdog import NadoWatchdog
    _nado_watchdog = NadoWatchdog(
        suspect_threshold_seconds=wd_config.get("suspect_threshold_seconds", 120),
        retry_max_attempts=wd_config.get("retry_max_attempts", 10),
        retry_base_delay_seconds=wd_config.get("retry_base_delay_seconds", 10),
        retry_max_delay_seconds=wd_config.get("retry_max_delay_seconds", 7200),
        reactivation_check_interval_hours=wd_config.get("reactivation_check_interval_hours", 24),
    )
    
    # Initialize persistence directly
    from nado_persistence import NadoPersistence
    state_path = wd_config.get("state_persistence_path", "/app/data/nado_watchdog_state.json")
    _nado_persistence = NadoPersistence(state_path)
    _nado_persistence.initialize(_nado_watchdog)
    
    # Try to load persisted state
    loaded = await _nado_persistence.load_state()
    
    if not loaded or not _nado_watchdog.get_all_symbols():
        # No persisted state, initialize with default symbols
        default_symbols = _nado_config.get("default_active_symbols", [])
        logger.info(f"OMS: Initializing watchdog with {len(default_symbols)} default symbols")
        
        # We need to fetch product IDs for the default symbols
        # For now, use hardcoded mapping from our earlier test
        product_map = {
            "BTC-PERP": 2, "ETH-PERP": 4, "SOL-PERP": 8, "XRP-PERP": 10,
            "BNB-PERP": 14, "HYPE-PERP": 16, "ZEC-PERP": 18, "FARTCOIN-PERP": 22,
            "SUI-PERP": 24, "XAUT-PERP": 28, "PUMP-PERP": 30, "TAO-PERP": 32,
            "XMR-PERP": 34, "LIT-PERP": 36, "kPEPE-PERP": 38, "PENGU-PERP": 40,
            "USELESS-PERP": 42, "SKR-PERP": 44, "UNI-PERP": 46, "ASTER-PERP": 48,
            "XPL-PERP": 50, "DOGE-PERP": 52, "WLFI-PERP": 54, "kBONK-PERP": 56,
            "ZRO-PERP": 58, "ADA-PERP": 60, "ARB-PERP": 62, "AVAX-PERP": 64,
            "AXS-PERP": 66, "BCH-PERP": 68, "BERA-PERP": 70, "ENA-PERP": 72,
            "LINK-PERP": 74, "LTC-PERP": 76, "NEAR-PERP": 78, "ONDO-PERP": 80,
            "SKY-PERP": 82, "VIRTUAL-PERP": 84, "JUP-PERP": 86, "XAG-PERP": 88,
            "WTI-PERP": 90, "EURUSD-PERP": 92, "GBPUSD-PERP": 94, "USDJPY-PERP": 96,
            "QQQ-PERP": 98, "SPY-PERP": 100, "AAPL-PERP": 102, "AMZN-PERP": 104,
            "GOOGL-PERP": 106, "META-PERP": 108, "MSFT-PERP": 110, "NVDA-PERP": 112,
            "TSLA-PERP": 114, "AAVE-PERP": 26,
        }
        
        for symbol in default_symbols:
            if symbol in product_map:
                _nado_watchdog.add_symbol(symbol, product_map[symbol], SymbolState.ACTIVE)
                # Initialize book snapshot
                _books[("nado", symbol)] = BookSnapshot()
    else:
        logger.info(f"OMS: Loaded {_nado_watchdog.get_statistics()['total']} symbols from persisted state")
        # Initialize book snapshots for loaded symbols
        for symbol in _nado_watchdog.get_all_symbols():
            _books[("nado", symbol)] = BookSnapshot()
    
    # Start auto-persistence
    await _nado_persistence.start_auto_persist(interval_seconds=60.0)
    logger.info("OMS: Nado auto-persistence started")
    
    # Initialize WebSocket manager directly
    from nado_websocket import NadoWebSocketManager
    _nado_ws_manager = NadoWebSocketManager(
        endpoint=ws_config.get("endpoint", "wss://gateway.prod.nado.xyz/v1/subscribe"),
        subscription_delay_ms=ws_config.get("subscription_delay_ms", 50),
        watchdog=_nado_watchdog,
    )
    
    # Register message handler (pass watchdog for update tracking)
    _nado_ws_manager.register_message_handler(
        lambda symbol, msg: handle_nado_book_message(symbol, msg, _books, _nado_watchdog)
    )
    
    # Start WebSocket manager
    await _nado_ws_manager.start()
    logger.info("OMS: Nado WebSocket manager started")
    
    # Initialize discovery directly
    from nado_discovery import NadoDiscovery
    _nado_discovery = NadoDiscovery(
        api_url=disc_config.get("nado_api_url", "https://gateway.prod.nado.xyz"),
        test_duration_seconds=disc_config.get("test_duration_seconds", 30),
        watchdog=_nado_watchdog,
    )
    
    # Run initial discovery in background (non-blocking)
    async def _background_discovery():
        await asyncio.sleep(30)  # Wait for initial connections
        logger.info("OMS: Running initial Nado discovery")
        await _nado_discovery.run_full_cycle()
    
    discovery_task = asyncio.create_task(_background_discovery(), name="nado-discovery-initial")
    _tasks.append(discovery_task)
    
    # Start scheduled discovery
    async def _scheduled_discovery():
        await _nado_discovery.start_scheduled_discovery(
            interval_hours=disc_config.get("interval_hours", 24)
        )
    
    scheduled_task = asyncio.create_task(_scheduled_discovery(), name="nado-discovery-scheduled")
    _tasks.append(scheduled_task)
    
    logger.info("OMS: Nado Watchdog System initialized successfully")


@app.on_event("shutdown")
async def shutdown():
    # Stop Nado watchdog system first
    if NADO_WATCHDOG_AVAILABLE:
        logger.info("OMS: Shutting down Nado watchdog system")
        if _nado_ws_manager:
            await _nado_ws_manager.stop()
        if _nado_persistence:
            await _nado_persistence.stop_auto_persist()
    
    # Cancel all tasks
    for task in _tasks:
        task.cancel()
    if _tasks:
        await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
    logger.info("OMS: shutdown complete")


# ── Extended WS — single shared connection for all markets ───────────

async def _run_extended_ws_all() -> None:
    """Single WS connection to Extended that receives all tracked markets."""
    reconnect_delay = 1.0

    while True:
        try:
            logger.info("OMS: Extended WS connecting (all markets): %s", _EXTENDED_WS_ALL)
            ws = await websockets.connect(_EXTENDED_WS_ALL, ssl=_SSL_CTX, close_timeout=5, open_timeout=15)
            reconnect_delay = 1.0
            logger.info("OMS: Extended WS connected (all markets)")
            # Mark all tracked Extended books as connected
            for key in list(_books):
                if key[0] == "extended":
                    _books[key].connected = True
            try:
                async for raw in ws:
                    _handle_extended_msg(raw)
            except websockets.ConnectionClosed:
                logger.warning("OMS: Extended WS disconnected")
            finally:
                for key in list(_books):
                    if key[0] == "extended":
                        _books[key].connected = False
                await ws.close()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("OMS: Extended WS error: %s — retry in %.0fs", exc, reconnect_delay)
            for key in list(_books):
                if key[0] == "extended":
                    _books[key].connected = False
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30.0)


def _handle_extended_msg(raw: str) -> None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    data = msg.get("data")
    if not data:
        return

    symbol = data.get("m")
    if not symbol:
        return
    key = ("extended", symbol)
    snap = _books.get(key)
    if snap is None:
        return

    msg_type = msg.get("type", "SNAPSHOT")
    bids_raw = data.get("b", [])
    asks_raw = data.get("a", [])

    if msg_type == "SNAPSHOT":
        bids = [[float(b["p"]), float(b["q"])] for b in bids_raw if "p" in b]
        asks = [[float(a["p"]), float(a["q"])] for a in asks_raw if "p" in a]
        if bids:
            snap.bids = sorted(bids, key=lambda x: -x[0])
        if asks:
            snap.asks = sorted(asks, key=lambda x: x[0])
    else:
        # DELTA
        if bids_raw:
            _apply_delta(snap.bids, bids_raw, reverse=True)
        if asks_raw:
            _apply_delta(snap.asks, asks_raw, reverse=False)

    snap.timestamp_ms = time.time() * 1000
    snap.update_count += 1
    _notify_subscribers(key)


def _apply_delta(levels: list, deltas: list, reverse: bool) -> None:
    """Apply incremental deltas using cumulative (c) field."""
    for d in deltas:
        price = float(d.get("p", 0))
        cum_qty = float(d.get("c", 0))
        if price <= 0:
            continue
        # Find and update existing level
        found = False
        for i, lvl in enumerate(levels):
            if abs(lvl[0] - price) < 1e-12:
                if cum_qty <= 0:
                    levels.pop(i)
                else:
                    levels[i] = [price, cum_qty]
                found = True
                break
        if not found and cum_qty > 0:
            levels.append([price, cum_qty])
    levels.sort(key=lambda x: -x[0] if reverse else x[0])


# ── GRVT WS ─────────────────────────────────────────────────────────

_X18_FLOAT = 1e18

# Cache for GRVT symbol to selector mapping
_grvt_symbol_to_selector: dict[str, str] = {}

async def _run_grvt_ws_all(symbols: list[str]) -> None:
    """Single WS connection to GRVT for all tracked symbols.
    
    GRVT supports subscribing to multiple selectors in one connection.
    Format: SYMBOL@INTERVAL-DEPTH (e.g., BTC_USDT_Perp@500-10)
    """
    ws_url = _GRVT_WS_ENDPOINTS.get(_GRVT_ENV, _GRVT_WS_ENDPOINTS["prod"])
    reconnect_delay = 1.0
    
    # Build selector list for all symbols
    selectors = [f"{sym}@500-10" for sym in symbols]
    for sym in symbols:
        _grvt_symbol_to_selector[sym.lower()] = f"{sym}@500-10"
    
    while True:
        try:
            logger.info("OMS: GRVT WS connecting (all %d markets): %s", len(symbols), ws_url)
            ws = await websockets.connect(ws_url, ssl=_SSL_CTX, close_timeout=5)
            
            # Subscribe to all symbols at once
            sub_msg = json.dumps({
                "jsonrpc": "2.0",
                "method": "subscribe",
                "params": {"stream": "v1.book.s", "selectors": selectors},
                "id": 1,
            })
            await ws.send(sub_msg)
            
            try:
                resp_raw = await asyncio.wait_for(ws.recv(), timeout=10)
                resp = json.loads(resp_raw)
                if "error" in resp:
                    raise RuntimeError(f"GRVT subscribe error: {resp['error']}")
                logger.info("OMS: GRVT WS subscribed to %d symbols", len(symbols))
            except asyncio.TimeoutError:
                logger.warning("OMS: GRVT WS subscribe timeout")
                await ws.close()
                raise
            
            reconnect_delay = 1.0
            # Mark all GRVT books as connected
            for sym in symbols:
                key = ("grvt", sym)
                if key in _books:
                    _books[key].connected = True
            
            try:
                async for raw in ws:
                    _handle_grvt_msg(raw)
            except websockets.ConnectionClosed:
                logger.warning("OMS: GRVT WS disconnected")
            finally:
                for sym in symbols:
                    key = ("grvt", sym)
                    if key in _books:
                        _books[key].connected = False
                await ws.close()
                
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("OMS: GRVT WS error: %s — retry in %.0fs", exc, reconnect_delay)
            for sym in symbols:
                key = ("grvt", sym)
                if key in _books:
                    _books[key].connected = False
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30.0)


def _handle_grvt_msg(raw: str) -> None:
    """Handle GRVT message and route to correct symbol book."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return
    
    feed = msg.get("feed")
    if not feed:
        return
    
    # Extract symbol from feed (GRVT format: symbol in the feed object)
    # The selector format is SYMBOL@INTERVAL-DEPTH, we need to extract symbol
    # GRVT messages include the symbol in the instrument field
    instrument = feed.get("instrument")
    if not instrument:
        return
    
    symbol = instrument
    key = ("grvt", symbol)
    snap = _books.get(key)
    if snap is None:
        return
    
    bids_raw = feed.get("bids", [])
    asks_raw = feed.get("asks", [])
    bids = [[float(b["price"]), float(b["size"])] for b in bids_raw if "price" in b]
    asks = [[float(a["price"]), float(a["size"])] for a in asks_raw if "price" in a]

    if not bids and not asks:
        return

    if bids:
        snap.bids = sorted(bids, key=lambda x: -x[0])
    if asks:
        snap.asks = sorted(asks, key=lambda x: x[0])
    snap.timestamp_ms = time.time() * 1000
    snap.update_count += 1
    snap.has_data = True
    _notify_subscribers(key)


# ── Nado WS ──────────────────────────────────────────────────────────

async def _resolve_nado_product_ids(symbols: list[str]) -> dict[str, int]:
    """Resolve symbols → product_ids via Nado REST /symbols endpoint."""
    # Check cache first
    result = {}
    missing = []
    for sym in symbols:
        if sym in _nado_product_ids:
            result[sym] = _nado_product_ids[sym]
        else:
            missing.append(sym)
    
    if missing:
        gateway = _NADO_GATEWAY or _NADO_GATEWAYS.get(_NADO_ENV, _NADO_GATEWAYS["mainnet"])
        url = f"{gateway}/symbols"
        try:
            headers = {"Accept-Encoding": "gzip"}
            async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                symbols_data = resp.json()
            for s in symbols_data:
                _nado_product_ids[s["symbol"]] = s["product_id"]
            logger.info("OMS: Nado symbols loaded: %d products", len(symbols_data))
            # Fill in missing symbols
            for sym in missing:
                if sym in _nado_product_ids:
                    result[sym] = _nado_product_ids[sym]
        except Exception as exc:
            logger.error("OMS: Nado symbol resolution failed: %s", exc)
    
    return result


async def _run_nado_ws_all(symbols: list[str]) -> None:
    """Single WS connection to Nado for all tracked symbols.
    
    Nado supports subscribing to multiple products on one connection.
    We subscribe to all products and route messages based on product_id.
    """
    ws_url = _NADO_WS_ENDPOINTS.get(_NADO_ENV, _NADO_WS_ENDPOINTS["mainnet"])
    rest_base = _NADO_GATEWAY or _NADO_GATEWAYS.get(_NADO_ENV, _NADO_GATEWAYS["mainnet"])
    reconnect_delay = 1.0
    
    # Resolve product IDs for all symbols
    symbol_to_product = await _resolve_nado_product_ids(symbols)
    logger.info("OMS: Nado symbol_to_product mapping: %d entries", len(symbol_to_product))
    if not symbol_to_product:
        logger.error("OMS: Nado could not resolve any product IDs — feed disabled")
        return
    
    # Build reverse mapping: product_id -> symbol
    product_to_symbol = {pid: sym for sym, pid in symbol_to_product.items()}
    logger.debug("OMS: Nado product_to_symbol: %s", product_to_symbol)
    
    # Nado requires permessage-deflate compression
    _nado_extensions = [ClientPerMessageDeflateFactory()]
    
    while True:
        ws = None
        try:
            logger.info("OMS: Nado WS connecting (all %d markets): %s", len(symbols), ws_url)
            ws = await websockets.connect(ws_url, ssl=_SSL_CTX, extensions=_nado_extensions, close_timeout=5)
            
            # Subscribe to each product individually (Nado format)
            # Nado doesn't support batch subscribe, so we send multiple subscribe messages
            subscribed_count = 0
            for sym, product_id in symbol_to_product.items():
                sub_msg = json.dumps({
                    "method": "subscribe",
                    "stream": {"type": "book_depth", "product_id": product_id},
                    "id": product_id,
                })
                await ws.send(sub_msg)
                subscribed_count += 1
                # Small delay between subscriptions to avoid overwhelming server
                if subscribed_count < len(symbol_to_product):
                    await asyncio.sleep(0.05)
            
            logger.info("OMS: Nado WS subscribed to %d products", subscribed_count)
            
            # Fetch REST snapshots for all symbols
            logger.info("OMS: Nado starting REST snapshot fetch for %d symbols", len(symbol_to_product))
            snapshot_ts_map: dict[int, str] = {}
            last_max_ts_map: dict[int, str] = {}
            snap_count = 0
            
            for sym, product_id in symbol_to_product.items():
                key = ("nado", sym)
                snap_count += 1
                if snap_count % 10 == 0:
                    logger.info("OMS: Nado fetched %d/%d snapshots", snap_count, len(symbol_to_product))
                try:
                    async with httpx.AsyncClient(timeout=10.0, verify=False) as hclient:
                        r = await hclient.post(
                            f"{rest_base}/query",
                            json={"type": "market_liquidity", "product_id": product_id, "depth": 100},
                            headers={"Accept-Encoding": "gzip"},
                        )
                        r.raise_for_status()
                        snap_data = r.json().get("data", {})
                    snapshot_ts = snap_data.get("timestamp", "0")
                    snapshot_ts_map[product_id] = snapshot_ts
                    last_max_ts_map[product_id] = "0"  # Don't set to snapshot_ts
                    
                    book = _books[key]
                    book.bids.clear()
                    book.asks.clear()
                    for p, q in snap_data.get("bids", []):
                        sz = float(q) / _X18_FLOAT
                        if sz > 0:
                            book.bids.append([float(p) / _X18_FLOAT, sz])
                    for p, q in snap_data.get("asks", []):
                        sz = float(q) / _X18_FLOAT
                        if sz > 0:
                            book.asks.append([float(p) / _X18_FLOAT, sz])
                    book.bids.sort(key=lambda x: -x[0])
                    book.asks.sort(key=lambda x: x[0])
                except Exception as exc:
                    logger.warning("OMS: Nado snapshot failed for %s: %s — using empty book", sym, exc)
                    snapshot_ts_map[product_id] = "0"
                    last_max_ts_map[product_id] = "0"
            
            reconnect_delay = 1.0
            # Mark all Nado books as connected
            connected_count = 0
            for sym in symbols:
                key = ("nado", sym)
                if key in _books:
                    _books[key].connected = True
                    connected_count += 1
            logger.info("OMS: Nado marked %d books as connected, entering message loop", connected_count)
            
            msg_count = 0
            try:
                logger.info("OMS: Nado starting message loop...")
                async for raw in ws:
                    msg = json.loads(raw)
                    if "bids" not in msg and "asks" not in msg:
                        continue
                    
                    msg_count += 1
                    if msg_count <= 10:
                        logger.info("OMS: Nado received book message %d: product_id=%s", msg_count, msg.get("product_id"))
                    
                    # Extract product_id from message and route to correct book
                    msg_product_id = msg.get("product_id")
                    if msg_product_id is None:
                        continue
                    
                    # Look up symbol for this product_id
                    sym = product_to_symbol.get(msg_product_id)
                    if sym is None:
                        if msg_count <= 5:
                            logger.warning("OMS: Nado unknown product_id: %s", msg_product_id)
                        continue
                    
                    key = ("nado", sym)
                    if key not in _books:
                        continue
                    
                    msg_max_ts = msg.get("max_timestamp", "0")
                    msg_last_max_ts = msg.get("last_max_timestamp", "0")
                    snapshot_ts = snapshot_ts_map.get(msg_product_id, "0")
                    last_max_ts = last_max_ts_map.get(msg_product_id, "0")
                    
                    # Skip events at or before snapshot timestamp
                    if msg_max_ts <= snapshot_ts:
                        continue
                    
                    # Check sequence gap
                    if last_max_ts != "0" and msg_last_max_ts != last_max_ts:
                        # Sequence gap detected - need to reconnect
                        logger.warning("OMS: Nado seq gap detected for %s — reconnecting", sym)
                        raise RuntimeError("Sequence gap")
                    
                    _handle_nado_msg(key, raw)
                    last_max_ts_map[msg_product_id] = msg_max_ts
                        
            except websockets.ConnectionClosed:
                logger.warning("OMS: Nado WS disconnected")
            except RuntimeError as exc:
                if "Sequence gap" in str(exc):
                    logger.warning("OMS: Nado sequence gap — reconnecting")
                else:
                    raise
            finally:
                for sym in symbols:
                    key = ("nado", sym)
                    if key in _books:
                        _books[key].connected = False
                if ws:
                    try:
                        await ws.close()
                    except Exception:
                        pass
                    
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("OMS: Nado WS error: %s — retry in %.0fs", exc, reconnect_delay)
            for sym in symbols:
                key = ("nado", sym)
                if key in _books:
                    _books[key].connected = False
            await asyncio.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 30.0)


def _handle_nado_msg(key: tuple, raw: str) -> None:
    """Parse Nado book_depth message (incremental deltas, x18 format)."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return

    data = msg.get("data") or (msg if "bids" in msg else None)
    if not data:
        return

    bids_raw = data.get("bids", [])
    asks_raw = data.get("asks", [])
    if not bids_raw and not asks_raw:
        return

    if key not in _books:
        logger.warning("OMS: Nado message for unknown key: %s", key)
        return

    snap = _books[key]
    old_update_count = snap.update_count
    
    # Apply deltas
    if bids_raw:
        _apply_nado_delta(snap.bids, bids_raw, reverse=True)
    if asks_raw:
        _apply_nado_delta(snap.asks, asks_raw, reverse=False)

    # Update metadata
    snap.timestamp_ms = time.time() * 1000
    snap.update_count = old_update_count + 1
    snap.has_data = True
    snap.connected = True
    
    _notify_subscribers(key)
    
    if snap.update_count <= 3:
        logger.info("OMS: Nado updated %s: update #%d, bids=%d, asks=%d", 
                    key[1], snap.update_count, len(snap.bids), len(snap.asks))


def _apply_nado_delta(book: list, updates: list, reverse: bool) -> None:
    """Apply Nado incremental book_depth updates.

    Each update is [price_x18, size_x18].
    size == 0 → remove level, otherwise upsert.
    """
    price_map = {level[0]: level for level in book}
    for entry in updates:
        if len(entry) < 2:
            continue
        price = float(entry[0]) / _X18_FLOAT
        size = float(entry[1]) / _X18_FLOAT
        if size <= 0:
            price_map.pop(price, None)
        else:
            price_map[price] = [price, size]
    book.clear()
    book.extend(sorted(price_map.values(), key=lambda x: -x[0] if reverse else x[0]))


# ── Variational REST Poll (shared single-request) ───────────────────

async def _run_variational_poll_all(symbols: list[str]) -> None:
    """Single shared poll that fetches the Variational Stats API once
    and distributes data to all tracked Variational symbols.

    This avoids N separate HTTP requests per cycle (one per symbol).
    """
    # Build ticker → symbol lookup
    ticker_to_sym: dict[str, str] = {}
    for sym in symbols:
        parts = sym.upper().split("-")
        ticker = parts[1] if len(parts) >= 3 and parts[0] == "P" else sym
        ticker_to_sym[ticker] = sym

    logger.info(
        "OMS: Variational shared poll for %d symbols (tickers: %s)",
        len(symbols), ", ".join(sorted(ticker_to_sym.keys())[:10]) + ("..." if len(ticker_to_sym) > 10 else ""),
    )

    while True:
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                resp = await client.get(_VARIATIONAL_STATS_URL)
                resp.raise_for_status()
                stats = resp.json()

            now_ms = time.time() * 1000
            updated = 0

            for listing in stats.get("listings", []):
                ticker = listing.get("ticker", "").upper()
                sym = ticker_to_sym.get(ticker)
                if sym is None:
                    continue

                key = ("variational", sym)
                snap = _books.get(key)
                if snap is None:
                    continue

                quotes = listing.get("quotes", {})
                bids = []
                asks = []
                for size_key, notional in [("size_1k", 1000), ("size_100k", 100000), ("size_1m", 1000000)]:
                    q = quotes.get(size_key)
                    if q and q.get("bid") and q.get("ask"):
                        bids.append([float(q["bid"]), notional])
                        asks.append([float(q["ask"]), notional])

                if bids:
                    snap.bids = sorted(bids, key=lambda x: -x[0])
                    snap.asks = sorted(asks, key=lambda x: x[0])
                    snap.timestamp_ms = now_ms
                    snap.update_count += 1
                    snap.connected = True
                    _notify_subscribers(key)
                    updated += 1

            if updated == 0:
                logger.warning("OMS: Variational poll — 0 symbols updated out of %d", len(symbols))

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("OMS: Variational poll error: %s", exc)
            for sym in symbols:
                key = ("variational", sym)
                if key in _books:
                    _books[key].connected = False

        await asyncio.sleep(1.2)
