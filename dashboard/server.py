"""Account Dashboard Server — read-only view of positions, trades, orders & fees."""

import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings
from app.grvt_client import GrvtClient
from app.extended_client import ExtendedClient

logger = logging.getLogger("dashboard")

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
_settings: Settings | None = None
_grvt: GrvtClient | None = None
_extended: ExtendedClient | None = None

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _settings, _grvt, _extended
    _settings = Settings()

    # Init GRVT client
    try:
        _grvt = GrvtClient(_settings)
        logger.info("GRVT client initialised")
    except Exception as exc:
        logger.warning("GRVT client init failed (will be unavailable): %s", exc)
        _grvt = None

    # Init Extended client
    try:
        _extended = ExtendedClient(
            base_url=_settings.extended_api_base_url,
            api_key=_settings.extended_api_key,
            public_key=_settings.extended_public_key,
            private_key=_settings.extended_private_key,
            vault=_settings.extended_vault,
        )
        logger.info("Extended client initialised")
    except Exception as exc:
        logger.warning("Extended client init failed (will be unavailable): %s", exc)
        _extended = None

    yield


app = FastAPI(title="TradeAutonom Dashboard", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _ts_to_iso(ts_ms: int | float | str | None) -> str | None:
    """Convert millisecond timestamp to ISO string."""
    if ts_ms is None:
        return None
    try:
        ms = int(ts_ms)
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return str(ts_ms)


def _ns_to_iso(ts_ns: int | float | str | None) -> str | None:
    """Convert nanosecond timestamp to ISO string."""
    if ts_ns is None:
        return None
    try:
        ns = int(ts_ns)
        return datetime.fromtimestamp(ns / 1_000_000_000, tz=timezone.utc).isoformat()
    except (ValueError, TypeError, OSError):
        return str(ts_ns)


def _safe_float(val, default=0.0) -> float:
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


# ---------------------------------------------------------------------------
# GRVT data fetchers
# ---------------------------------------------------------------------------
def _grvt_account_summary() -> dict | None:
    if not _grvt:
        return None
    try:
        raw = _grvt.get_account_summary()
        result = raw.get("result", raw) if isinstance(raw, dict) else {}
        return {
            "exchange": "GRVT",
            "total_equity": _safe_float(result.get("total_equity")),
            "available_balance": _safe_float(result.get("available_balance")),
            "unrealized_pnl": _safe_float(result.get("unrealized_pnl")),
            "initial_margin": _safe_float(result.get("initial_margin")),
            "maintenance_margin": _safe_float(result.get("maintenance_margin")),
        }
    except Exception as exc:
        logger.warning("GRVT account summary failed: %s", exc)
        return None


def _grvt_positions() -> list[dict]:
    if not _grvt:
        return []
    try:
        raw = _grvt.get_account_summary()
        result = raw.get("result", raw) if isinstance(raw, dict) else {}
        positions = result.get("positions", [])
        out = []
        for p in positions:
            size = _safe_float(p.get("size"))
            if size == 0:
                continue
            out.append({
                "exchange": "GRVT",
                "instrument": p.get("instrument", ""),
                "side": "LONG" if size > 0 else "SHORT",
                "size": abs(size),
                "entry_price": _safe_float(p.get("entry_price")),
                "mark_price": _safe_float(p.get("mark_price")),
                "unrealized_pnl": _safe_float(p.get("unrealized_pnl")),
                "realized_pnl": _safe_float(p.get("realized_pnl")),
                "total_pnl": _safe_float(p.get("total_pnl")),
                "leverage": _safe_float(p.get("leverage")),
                "cumulative_fee": _safe_float(p.get("cumulative_fee")),
            })
        return out
    except Exception as exc:
        logger.warning("GRVT positions failed: %s", exc)
        return []


def _grvt_fill_history() -> list[dict]:
    """Fetch fill history from GRVT."""
    if not _grvt:
        return []
    try:
        from pysdk.grvt_ccxt_env import get_grvt_endpoint
        path = get_grvt_endpoint(_grvt._env, "FILL_HISTORY")
        payload = {
            "sub_account_id": _settings.grvt_trading_account_id,
            "kind": ["PERPETUAL"],
            "limit": 500,
        }
        raw = _grvt._api._auth_and_post(path, payload)
        fills = raw.get("result", [])
        out = []
        for f in fills:
            out.append({
                "exchange": "GRVT",
                "time": _ns_to_iso(f.get("event_time")),
                "instrument": f.get("instrument", ""),
                "side": "BUY" if f.get("is_buyer") else "SELL",
                "price": _safe_float(f.get("price")),
                "qty": _safe_float(f.get("size")),
                "value": _safe_float(f.get("price")) * _safe_float(f.get("size")),
                "fee": _safe_float(f.get("fee")),
                "fee_rate": _safe_float(f.get("fee_rate")),
                "realized_pnl": _safe_float(f.get("realized_pnl")),
                "is_taker": f.get("is_taker", False),
                "trade_id": f.get("trade_id", ""),
                "order_id": f.get("order_id", ""),
            })
        return out
    except Exception as exc:
        logger.warning("GRVT fill history failed: %s", exc)
        return []


def _grvt_order_history() -> list[dict]:
    """Fetch order history from GRVT."""
    if not _grvt:
        return []
    try:
        from pysdk.grvt_ccxt_env import get_grvt_endpoint
        path = get_grvt_endpoint(_grvt._env, "ORDER_HISTORY")
        payload = {
            "sub_account_id": _settings.grvt_trading_account_id,
            "kind": ["PERPETUAL"],
            "limit": 500,
        }
        raw = _grvt._api._auth_and_post(path, payload)
        orders = raw.get("result", [])
        out = []
        for o in orders:
            state = o.get("state", {})
            metadata = o.get("metadata", {})
            legs = o.get("legs", [])
            leg = legs[0] if legs else {}
            out.append({
                "exchange": "GRVT",
                "time": _ns_to_iso(metadata.get("create_time")),
                "instrument": leg.get("instrument", ""),
                "status": state.get("status", ""),
                "side": "BUY" if leg.get("is_buying_asset") else "SELL",
                "price": _safe_float(leg.get("limit_price")),
                "qty": _safe_float(leg.get("size")),
                "filled_qty": _safe_float(state.get("traded_size", [0])[0] if state.get("traded_size") else 0),
                "paid_fee": _safe_float(state.get("fee", 0)),
                "order_type": metadata.get("order_type", ""),
                "time_in_force": o.get("time_in_force", ""),
            })
        return out
    except Exception as exc:
        logger.warning("GRVT order history failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Extended data fetchers
# ---------------------------------------------------------------------------
def _extended_account_summary() -> dict | None:
    if not _extended:
        return None
    try:
        raw = _extended.get_account_summary()
        return {
            "exchange": "Extended",
            "total_equity": _safe_float(raw.get("total_equity")),
            "available_balance": _safe_float(raw.get("available_balance")),
            "unrealized_pnl": _safe_float(raw.get("unrealized_pnl")),
            "initial_margin": 0.0,
            "maintenance_margin": 0.0,
        }
    except Exception as exc:
        logger.warning("Extended account summary failed: %s", exc)
        return None


def _extended_positions() -> list[dict]:
    if not _extended:
        return []
    try:
        positions = _extended.fetch_positions()
        out = []
        for p in positions:
            size = abs(_safe_float(p.get("size")))
            if size == 0:
                continue
            out.append({
                "exchange": "Extended",
                "instrument": p.get("instrument", ""),
                "side": p.get("side", "LONG"),
                "size": size,
                "entry_price": _safe_float(p.get("entry_price")),
                "mark_price": _safe_float(p.get("mark_price")),
                "unrealized_pnl": _safe_float(p.get("unrealized_pnl")),
                "realized_pnl": 0.0,
                "total_pnl": _safe_float(p.get("unrealized_pnl")),
                "leverage": _safe_float(p.get("leverage")),
                "cumulative_fee": 0.0,
            })
        return out
    except Exception as exc:
        logger.warning("Extended positions failed: %s", exc)
        return []


def _extended_positions_history() -> list[dict]:
    if not _extended:
        return []
    try:
        url = f"{_extended._base_url}/user/positions/history"
        resp = _extended._session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status", "").upper() != "OK":
            return []
        out = []
        for p in data.get("data", []):
            out.append({
                "exchange": "Extended",
                "instrument": p.get("market", ""),
                "side": p.get("side", ""),
                "size": _safe_float(p.get("size")),
                "entry_price": _safe_float(p.get("openPrice")),
                "exit_price": _safe_float(p.get("exitPrice")),
                "realized_pnl": _safe_float(p.get("realisedPnl")),
                "leverage": _safe_float(p.get("leverage")),
                "exit_type": p.get("exitType", ""),
                "opened": _ts_to_iso(p.get("createdTime")),
                "closed": _ts_to_iso(p.get("closedTime")),
            })
        return out
    except Exception as exc:
        logger.warning("Extended positions history failed: %s", exc)
        return []


def _extended_trades() -> list[dict]:
    if not _extended:
        return []
    try:
        url = f"{_extended._base_url}/user/trades"
        resp = _extended._session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status", "").upper() != "OK":
            return []
        out = []
        for t in data.get("data", []):
            out.append({
                "exchange": "Extended",
                "time": _ts_to_iso(t.get("createdTime")),
                "instrument": t.get("market", ""),
                "side": t.get("side", ""),
                "price": _safe_float(t.get("price")),
                "qty": _safe_float(t.get("qty")),
                "value": _safe_float(t.get("value")),
                "fee": _safe_float(t.get("fee")),
                "fee_rate": 0.0,
                "realized_pnl": 0.0,
                "is_taker": t.get("isTaker", False),
                "trade_id": str(t.get("id", "")),
                "order_id": str(t.get("orderId", "")),
            })
        return out
    except Exception as exc:
        logger.warning("Extended trades failed: %s", exc)
        return []


def _extended_order_history() -> list[dict]:
    if not _extended:
        return []
    try:
        url = f"{_extended._base_url}/user/orders/history"
        resp = _extended._session.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status", "").upper() != "OK":
            return []
        out = []
        for o in data.get("data", []):
            out.append({
                "exchange": "Extended",
                "time": _ts_to_iso(o.get("createdTime")),
                "instrument": o.get("market", ""),
                "status": o.get("status", ""),
                "side": o.get("side", ""),
                "price": _safe_float(o.get("price")),
                "qty": _safe_float(o.get("qty")),
                "filled_qty": _safe_float(o.get("filledQty")),
                "paid_fee": _safe_float(o.get("payedFee")),
                "order_type": o.get("type", ""),
                "time_in_force": o.get("timeInForce", ""),
            })
        return out
    except Exception as exc:
        logger.warning("Extended order history failed: %s", exc)
        return []


def _extended_fees() -> list[dict]:
    if not _extended:
        return []
    try:
        # Fetch fees for common markets
        markets = ["SOL-USD", "BTC-USD", "ETH-USD"]
        out = []
        for market in markets:
            try:
                fee_data = _extended.fetch_fees(market)
                if fee_data:
                    out.append({
                        "exchange": "Extended",
                        "market": fee_data.get("market", market),
                        "maker_fee_rate": _safe_float(fee_data.get("makerFeeRate")),
                        "taker_fee_rate": _safe_float(fee_data.get("takerFeeRate")),
                    })
            except Exception:
                pass
        return out
    except Exception as exc:
        logger.warning("Extended fees failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "service": "dashboard"}


@app.get("/api/overview")
async def api_overview():
    accounts = []
    grvt_summary = _grvt_account_summary()
    if grvt_summary:
        accounts.append(grvt_summary)
    ext_summary = _extended_account_summary()
    if ext_summary:
        accounts.append(ext_summary)

    total_equity = sum(a["total_equity"] for a in accounts)
    total_balance = sum(a["available_balance"] for a in accounts)
    total_upnl = sum(a["unrealized_pnl"] for a in accounts)

    return {
        "accounts": accounts,
        "total_equity": total_equity,
        "total_available_balance": total_balance,
        "total_unrealized_pnl": total_upnl,
    }


@app.get("/api/positions")
async def api_positions():
    positions = _grvt_positions() + _extended_positions()
    return {"positions": positions, "count": len(positions)}


@app.get("/api/positions/history")
async def api_positions_history():
    history = _extended_positions_history()
    return {"positions": history, "count": len(history)}


@app.get("/api/trades")
async def api_trades():
    trades = _grvt_fill_history() + _extended_trades()
    # Sort by time descending
    trades.sort(key=lambda t: t.get("time") or "", reverse=True)
    total_fees = sum(t.get("fee", 0) for t in trades)
    total_pnl = sum(t.get("realized_pnl", 0) for t in trades)
    return {
        "trades": trades,
        "count": len(trades),
        "total_fees": total_fees,
        "total_realized_pnl": total_pnl,
    }


@app.get("/api/orders")
async def api_orders():
    orders = _grvt_order_history() + _extended_order_history()
    orders.sort(key=lambda o: o.get("time") or "", reverse=True)
    total_fees = sum(o.get("paid_fee", 0) for o in orders)
    return {"orders": orders, "count": len(orders), "total_fees": total_fees}


@app.get("/api/fees")
async def api_fees():
    fee_rates = _extended_fees()

    # Calculate totals from trades
    grvt_trades = _grvt_fill_history()
    ext_trades = _extended_trades()
    grvt_total_fee = sum(t.get("fee", 0) for t in grvt_trades)
    ext_total_fee = sum(t.get("fee", 0) for t in ext_trades)

    return {
        "fee_rates": fee_rates,
        "total_fees_paid": {
            "grvt": grvt_total_fee,
            "extended": ext_total_fee,
            "total": grvt_total_fee + ext_total_fee,
        },
    }


@app.get("/ui")
async def serve_ui():
    return FileResponse(STATIC_DIR / "dashboard.html")
