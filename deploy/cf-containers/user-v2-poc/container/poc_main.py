"""
Phase F.0 feasibility PoC for UserContainer-v2.

FastAPI server with five endpoints that prove whether the Python stack V1
relies on actually works under Cloudflare Containers' Linux userspace.

Endpoints:
  GET /health                 liveness
  GET /test/extended          x10-python-trading-starknet imports + curve math
  GET /test/curl_cffi         curl_cffi hits Variational public stats
  GET /test/oms_ws            WebSocket to oms-v2.defitool.de, 1 book message
  GET /test/grvt              grvt-pysdk imports + public REST probe
  GET /test/modules           list top-level importable modules (debug)
  GET /test/all               run all four and return a summary
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
import time
import traceback
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("user-v2-poc")

app = FastAPI(title="UserContainer-v2 PoC")


@app.get("/")
async def root() -> dict[str, Any]:
    return {
        "service": "user-v2-poc",
        "python_version": sys.version,
        "endpoints": [
            "/health",
            "/test/extended",
            "/test/curl_cffi",
            "/test/oms_ws",
            "/test/grvt",
            "/test/modules",
            "/test/all",
        ],
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "ts": time.time()}


# ── Probe 0: installed module / package discovery ──────────────────

def _probe_modules() -> dict[str, Any]:
    """List which known-relevant packages are installed + their version."""
    out: dict[str, Any] = {}
    candidates = [
        "x10", "x10.perpetual", "x10.perpetual.accounts",
        "grvt", "pysdk", "grvt_pysdk",
        "curl_cffi", "eth_account", "websockets",
        "fastapi", "uvicorn", "httpx",
        "fast_stark_crypto", "starkex_resources",
    ]
    for name in candidates:
        try:
            m = importlib.import_module(name)
            version = getattr(m, "__version__", None)
            out[name] = {"ok": True, "version": version, "file": getattr(m, "__file__", None)}
        except Exception as exc:
            out[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return out


@app.get("/test/modules")
async def test_modules() -> JSONResponse:
    return JSONResponse(_probe_modules())


@app.get("/test/x10_layout")
async def test_x10_layout() -> JSONResponse:
    """Introspect x10 SDK module layout as installed."""
    import os
    out: dict[str, Any] = {}
    try:
        import x10.perpetual as _xp
        px = os.path.dirname(_xp.__file__)
        out["x10.perpetual_path"] = px
        out["entries"] = sorted(os.listdir(px))
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return JSONResponse(out)


@app.get("/test/pysdk_layout")
async def test_pysdk_layout() -> JSONResponse:
    """Introspect pysdk (GRVT SDK) module layout as installed."""
    import os
    out: dict[str, Any] = {}
    try:
        import pysdk as _ps
        px = os.path.dirname(_ps.__file__)
        out["pysdk_path"] = px
        out["entries"] = sorted(os.listdir(px))
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return JSONResponse(out)


# ── Probe 1: Extended (x10-python-trading-starknet) ────────────────

def _probe_extended() -> dict[str, Any]:
    """Verify x10 SDK imports and Starknet curve sign works."""
    t0 = time.perf_counter()
    try:
        import x10  # noqa: F401
        version = getattr(x10, "__version__", "unknown")

        # Discover what's actually under x10. Different versions of the SDK
        # expose different submodules — we just need one that gives us Starknet
        # signing. V1 app/extended_client.py uses these imports; try variants.
        tried: list[dict[str, Any]] = []
        sign_callable = None

        # Variant A: pre-1.4 layout
        try:
            from x10.perpetual.accounts import StarkPerpetualAccount  # type: ignore
            tried.append({"try": "x10.perpetual.accounts.StarkPerpetualAccount", "ok": True})
        except Exception as exc:
            tried.append({"try": "x10.perpetual.accounts", "ok": False, "err": str(exc)})

        # Variant B: 1.4+ flat layout (seen in our installed version)
        try:
            from x10.perpetual.accounts import StarkPerpetualAccount  # noqa: F811,F401
        except Exception:
            pass

        # Variant C: look for starkex signing helpers
        try:
            from x10.utils.starkex import sign  # type: ignore
            sign_callable = sign
            tried.append({"try": "x10.utils.starkex.sign", "ok": True})
        except Exception as exc:
            tried.append({"try": "x10.utils.starkex.sign", "ok": False, "err": str(exc)})

        # Variant D: fast_stark_crypto directly (underlying C lib)
        try:
            import fast_stark_crypto
            sign_fn = getattr(fast_stark_crypto, "sign", None) or getattr(fast_stark_crypto, "sign_message", None)
            if sign_fn is not None and sign_callable is None:
                sign_callable = sign_fn
                tried.append({"try": "fast_stark_crypto.sign", "ok": True, "attr": sign_fn.__name__})
            else:
                tried.append({"try": "fast_stark_crypto attrs", "ok": True, "dir": [x for x in dir(fast_stark_crypto) if not x.startswith("_")][:20]})
        except Exception as exc:
            tried.append({"try": "fast_stark_crypto", "ok": False, "err": str(exc)})

        # Run the curve sign if we found something callable
        curve_ok = False
        sig_preview = None
        if sign_callable is not None:
            try:
                test_hash = int("0x" + "1" * 62, 16)
                test_priv = int("0x" + "2" * 62, 16)
                sig = sign_callable(test_priv, test_hash)
                if isinstance(sig, tuple) and len(sig) == 2:
                    r, s = sig
                    curve_ok = isinstance(r, int) and isinstance(s, int) and r > 0 and s > 0
                    sig_preview = {"r_hex": hex(r)[:14], "s_hex": hex(s)[:14]}
                elif sig is not None:
                    curve_ok = True
                    sig_preview = {"type": type(sig).__name__, "repr": str(sig)[:80]}
            except Exception as exc:
                tried.append({"try": "sign() call", "ok": False, "err": str(exc)})

        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {
            "ok": curve_ok,
            "x10_version": version,
            "sig_preview": sig_preview,
            "tried": tried,
            "elapsed_ms": round(elapsed_ms, 2),
        }
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc().splitlines()[-5:],
            "elapsed_ms": round(elapsed_ms, 2),
        }


@app.get("/test/extended")
async def test_extended() -> JSONResponse:
    return JSONResponse(_probe_extended())


# ── Probe 2: curl_cffi (Variational Cloudflare TLS-bypass) ─────────

def _probe_curl_cffi() -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        from curl_cffi import requests as cc_requests  # type: ignore

        url = "https://omni-client-api.prod.ap-northeast-1.variational.io/metadata/stats"
        resp = cc_requests.get(url, impersonate="chrome120", timeout=15)
        size = len(resp.content)
        status = resp.status_code
        snippet_ok = False
        try:
            data = resp.json()
            snippet_ok = isinstance(data, dict) and ("listings" in data or len(data) > 0)
        except Exception:
            pass

        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {
            "ok": status == 200 and snippet_ok,
            "status_code": status,
            "body_bytes": size,
            "content_type": resp.headers.get("content-type", ""),
            "looks_like_stats_payload": snippet_ok,
            "elapsed_ms": round(elapsed_ms, 2),
        }
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc().splitlines()[-5:],
            "elapsed_ms": round(elapsed_ms, 2),
        }


@app.get("/test/curl_cffi")
async def test_curl_cffi() -> JSONResponse:
    return JSONResponse(_probe_curl_cffi())


# ── Probe 3: OMS-v2 WebSocket reachability ─────────────────────────

async def _probe_oms_ws() -> dict[str, Any]:
    """Open WebSocket to oms-v2.defitool.de, subscribe to GRVT BTC, receive 1 book.

    Timeouts/connection errors here would indicate outbound WS from CF
    Containers to another Cloudflare Worker zone is blocked or slow.
    """
    import websockets

    t0 = time.perf_counter()
    url = "wss://oms-v2.defitool.de/ws"
    try:
        async with websockets.connect(
            url,
            open_timeout=20,
            close_timeout=5,
            ping_interval=None,
        ) as ws:
            t_connect_ms = (time.perf_counter() - t0) * 1000
            await ws.send(json.dumps({
                "action": "subscribe",
                "exchange": "grvt",
                "symbol": "BTC_USDT_Perp",
            }))

            got_ack = False
            got_book = False
            sample_book: dict[str, Any] | None = None
            t_ack_ms = 0.0
            t_book_ms = 0.0

            deadline = time.perf_counter() + 10.0
            msgs_seen = 0
            while time.perf_counter() < deadline and msgs_seen < 20:
                remaining = max(0.1, deadline - time.perf_counter())
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                msgs_seen += 1
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if msg.get("type") == "subscribed":
                    got_ack = True
                    t_ack_ms = (time.perf_counter() - t0) * 1000
                elif msg.get("type") == "book":
                    got_book = True
                    t_book_ms = (time.perf_counter() - t0) * 1000
                    sample_book = {
                        "exchange": msg.get("exchange"),
                        "symbol": msg.get("symbol"),
                        "timestamp_ms": msg.get("timestamp_ms"),
                        "best_bid": msg.get("bids", [[None, None]])[0],
                        "best_ask": msg.get("asks", [[None, None]])[0],
                        "mid_price": msg.get("mid_price"),
                        "has_enrichment": isinstance(msg.get("bid_qty_cumsum"), list),
                    }
                    break

            elapsed_ms = (time.perf_counter() - t0) * 1000
            return {
                "ok": got_ack and got_book,
                "t_connect_ms": round(t_connect_ms, 2),
                "got_subscribed_ack": got_ack,
                "t_ack_ms": round(t_ack_ms, 2),
                "got_book": got_book,
                "t_book_ms": round(t_book_ms, 2),
                "messages_seen": msgs_seen,
                "sample": sample_book,
                "elapsed_ms": round(elapsed_ms, 2),
            }
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc().splitlines()[-5:],
            "elapsed_ms": round(elapsed_ms, 2),
        }


@app.get("/test/oms_ws")
async def test_oms_ws() -> JSONResponse:
    return JSONResponse(await _probe_oms_ws())


# ── Probe 4: GRVT SDK ─────────────────────────────────────────────

def _probe_grvt() -> dict[str, Any]:
    """Verify grvt-pysdk imports and can hit a public GRVT endpoint.

    The installed package is `grvt-pysdk` on PyPI but the actual import name
    may be `grvt`, `pysdk`, or `grvt_pysdk`. Try all and record.
    """
    t0 = time.perf_counter()
    try:
        import_tried: list[dict[str, Any]] = []
        imported_as = None
        # V1 app/grvt_client.py uses `from pysdk.grvt_ccxt import GrvtCcxt`.
        for name in ["pysdk", "pysdk.grvt_ccxt", "grvt"]:
            try:
                m = importlib.import_module(name)
                imported_as = name
                import_tried.append({"try": name, "ok": True, "version": getattr(m, "__version__", None)})
                if name == "pysdk.grvt_ccxt":
                    break  # found the one V1 actually uses
            except Exception as exc:
                import_tried.append({"try": name, "ok": False, "err": f"{type(exc).__name__}: {exc}"})

        # Public market-data REST: all_instruments. No auth required.
        import requests
        resp = requests.post(
            "https://market-data.grvt.io/full/v1/all_instruments",
            headers={"Content-Type": "application/json"},
            json={"is_active": True},
            timeout=15,
        )
        status = resp.status_code
        data = resp.json() if status == 200 else None
        n_instruments = len(data.get("result", [])) if isinstance(data, dict) else 0

        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {
            "ok": imported_as is not None and status == 200 and n_instruments > 0,
            "imported_as": imported_as,
            "import_tried": import_tried,
            "status_code": status,
            "instruments": n_instruments,
            "elapsed_ms": round(elapsed_ms, 2),
        }
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000
        return {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc().splitlines()[-5:],
            "elapsed_ms": round(elapsed_ms, 2),
        }


@app.get("/test/grvt")
async def test_grvt() -> JSONResponse:
    return JSONResponse(_probe_grvt())


# ── Combined summary ───────────────────────────────────────────────

@app.get("/test/all")
async def test_all() -> JSONResponse:
    loop = asyncio.get_running_loop()
    fut_ext = loop.run_in_executor(None, _probe_extended)
    fut_cffi = loop.run_in_executor(None, _probe_curl_cffi)
    fut_grvt = loop.run_in_executor(None, _probe_grvt)
    fut_ws = asyncio.create_task(_probe_oms_ws())

    results = {
        "extended": await fut_ext,
        "curl_cffi": await fut_cffi,
        "grvt": await fut_grvt,
        "oms_ws": await fut_ws,
    }
    overall = all(v.get("ok") for v in results.values())
    return JSONResponse({"overall_ok": overall, **results})


if __name__ == "__main__":
    uvicorn.run("poc_main:app", host="0.0.0.0", port=8000, log_level="info")
