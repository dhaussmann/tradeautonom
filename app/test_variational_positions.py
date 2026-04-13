#!/usr/bin/env python3
"""Test script: Query Variational positions exactly like the bot does.

Replicates VariationalClient._sync_get() + async_fetch_positions() logic
to provoke and diagnose HTTP 403 errors.

Usage (inside container):
    python /app/app/test_variational_positions.py <JWT_TOKEN>
"""

import base64
import json
import sys
import time

from curl_cffi import requests as _cffi_requests

# ── Config ─────────────────────────────────────────────────────────
BASE_URL = "https://omni.variational.io/api"
SYMBOL_FILTER = "P-TAO-USDC-3600"
POLL_INTERVAL = 1  # seconds between queries (aggressive to provoke 403)
MAX_ITERATIONS = 300  # stop after N iterations


def _extract_address(token: str) -> str:
    """Extract wallet address from JWT — same as VariationalClient._extract_address_from_jwt."""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        data = json.loads(base64.b64decode(payload_b64))
        addr = data.get("address", "")
        if addr:
            return addr
    except Exception as exc:
        print(f"[WARN] Could not extract address from JWT: {exc}")
    return ""


# ── Position query — exact copy of VariationalClient logic ────────

def _headers(wallet_address: str) -> dict:
    """Same as VariationalClient._headers()."""
    return {
        "Content-Type": "application/json",
        "vr-connected-address": wallet_address,
    }


def _cookies(jwt_token: str) -> dict:
    """Same as VariationalClient._cookies()."""
    return {"vr-token": jwt_token}


def sync_get(session, url: str, wallet_address: str, jwt_token: str):
    """Same as VariationalClient._sync_get() for the trading API."""
    resp = session.get(
        url,
        headers=_headers(wallet_address),
        cookies=_cookies(jwt_token),
        timeout=15,
    )
    return resp


def parse_positions(positions_raw, symbol_filter: str | None = None) -> list[dict]:
    """Same as VariationalClient.async_fetch_positions() parsing logic."""
    if not isinstance(positions_raw, list):
        positions_raw = positions_raw.get("positions", []) if isinstance(positions_raw, dict) else []

    result = []
    for pos in positions_raw:
        pi = pos.get("position_info", pos)
        inst = pi.get("instrument", {})
        underlying = inst.get("underlying", "") if isinstance(inst, dict) else str(inst)
        fi = inst.get("funding_interval_s", 3600) if isinstance(inst, dict) else 3600
        settle = inst.get("settlement_asset", "USDC") if isinstance(inst, dict) else "USDC"
        full_symbol = f"P-{underlying}-{settle}-{fi}"

        size = float(pi.get("qty", pi.get("size", 0)))
        side_val = pi.get("side", "long" if size > 0 else "short")
        entry_price = float(pi.get("avg_entry_price", pi.get("entry_price", 0)))

        if symbol_filter:
            if full_symbol != symbol_filter:
                try:
                    parts = symbol_filter.split("-")
                    if len(parts) >= 2 and parts[1].upper() != underlying.upper():
                        continue
                except Exception:
                    if underlying.upper() not in symbol_filter.upper():
                        continue

        result.append({
            "symbol": full_symbol,
            "size": abs(size),
            "side": side_val,
            "entry_price": entry_price,
            "unrealized_pnl": float(pos.get("upnl", 0)),
        })
    return result


def main():
    if len(sys.argv) < 2:
        print("Usage: python /app/app/test_variational_positions.py <JWT_TOKEN>")
        sys.exit(1)

    jwt_token = sys.argv[1]
    print(f"[OK] JWT token (len={len(jwt_token)})")

    wallet_address = _extract_address(jwt_token)
    if not wallet_address:
        print("[ERROR] Could not extract wallet address from JWT")
        sys.exit(1)

    print(f"[OK] Wallet: {wallet_address[:6]}...{wallet_address[-4:]}")
    print(f"[OK] Symbol: {SYMBOL_FILTER}")
    print(f"[OK] Poll: {POLL_INTERVAL}s, max {MAX_ITERATIONS} iterations")
    print(f"[OK] URL: {BASE_URL}/positions")
    print("=" * 70)

    session = _cffi_requests.Session(impersonate="chrome")

    success_count = 0
    error_count = 0
    error_403_count = 0

    for iteration in range(1, MAX_ITERATIONS + 1):
        t0 = time.time()
        try:
            resp = sync_get(session, f"{BASE_URL}/positions", wallet_address, jwt_token)
            elapsed_ms = (time.time() - t0) * 1000
            status = resp.status_code

            if status == 200:
                success_count += 1
                data = resp.json()
                positions = parse_positions(data, SYMBOL_FILTER)
                if positions:
                    p = positions[0]
                    print(f"[{iteration:4d}] {status} OK  {elapsed_ms:6.0f}ms | {p['symbol']} size={p['size']:.6f} side={p['side']} entry={p['entry_price']:.4f} upnl={p['unrealized_pnl']:.4f}")
                else:
                    n = len(data) if isinstance(data, list) else "?"
                    print(f"[{iteration:4d}] {status} OK  {elapsed_ms:6.0f}ms | No match (total: {n})")
            elif status == 403:
                error_403_count += 1
                error_count += 1
                print(f"[{iteration:4d}] {status} 403 {elapsed_ms:6.0f}ms | >>> FORBIDDEN <<< {resp.text[:200]}")
            else:
                error_count += 1
                print(f"[{iteration:4d}] {status} ERR {elapsed_ms:6.0f}ms | {resp.text[:200]}")

        except Exception as exc:
            elapsed_ms = (time.time() - t0) * 1000
            error_count += 1
            print(f"[{iteration:4d}] EXC     {elapsed_ms:6.0f}ms | {type(exc).__name__}: {exc}")

        if iteration % 20 == 0:
            total = success_count + error_count
            print(f"  --- Summary: {success_count}/{total} OK, {error_403_count} x 403, {error_count - error_403_count} other ---")

        time.sleep(POLL_INTERVAL)

    print("=" * 70)
    print(f"DONE: {success_count} OK, {error_403_count} x 403, {error_count - error_403_count} other / {MAX_ITERATIONS}")


if __name__ == "__main__":
    main()
