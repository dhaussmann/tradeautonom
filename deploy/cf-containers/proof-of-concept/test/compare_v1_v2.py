#!/usr/bin/env python3
"""
Compare V1 (Photon OMS on 192.168.133.100:8099) vs V2 (CF-PoC OMS) book
outputs for the same Extended markets, side by side.

For each sampled market:
  - fetches /book/extended/{market} from V1
  - fetches /book/{market} from V2
  - reports top bid / top ask / spread / age_ms diff

Usage:
  python3 compare_v1_v2.py
  python3 compare_v1_v2.py --markets BTC-USD,ETH-USD,SOL-USD
  python3 compare_v1_v2.py --resolve-via 1.1.1.1  # bypass WARP
  python3 compare_v1_v2.py --duration 300 --interval 10

Exit code is 0 on success. Non-zero if any sampled market fails to fetch from
either side.
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection
from typing import Any, Iterable

V1_HOST = "192.168.133.100"
V1_PORT = 8099
V2_HOST = "oms-v2-poc.defitool.de"
DEFAULT_MARKETS = [
    "BTC-USD",
    "ETH-USD",
    "SOL-USD",
    "BNB-USD",
    "AAVE-USD",
    "ASTER-USD",
    "AVAX-USD",
    "ADA-USD",
]


@dataclass
class Side:
    ok: bool
    top_bid: float | None = None
    top_bid_qty: float | None = None
    top_ask: float | None = None
    top_ask_qty: float | None = None
    bid_levels: int = 0
    ask_levels: int = 0
    age_ms: int | None = None
    updates: int = 0
    error: str = ""


def resolve(host: str, via: str | None) -> str:
    if via is None:
        return host
    try:
        result = subprocess.run(
            ["dig", "+short", f"@{via}", host],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in result.stdout.splitlines():
            line = line.strip()
            if line and not line.endswith("."):
                return line
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return socket.gethostbyname(host)


def fetch_v1(market: str) -> Side:
    conn = HTTPConnection(V1_HOST, V1_PORT, timeout=5)
    try:
        conn.request("GET", f"/book/extended/{market}")
        resp = conn.getresponse()
        if resp.status != 200:
            return Side(ok=False, error=f"v1 status={resp.status}")
        d = json.loads(resp.read().decode("utf-8"))
        bids = d.get("bids", [])
        asks = d.get("asks", [])
        tb = bids[0] if bids else (None, None)
        ta = asks[0] if asks else (None, None)
        ts = d.get("timestamp_ms", 0)
        age = int(time.time() * 1000 - ts) if ts else None
        return Side(
            ok=True,
            top_bid=float(tb[0]) if tb[0] is not None else None,
            top_bid_qty=float(tb[1]) if tb[1] is not None else None,
            top_ask=float(ta[0]) if ta[0] is not None else None,
            top_ask_qty=float(ta[1]) if ta[1] is not None else None,
            bid_levels=len(bids),
            ask_levels=len(asks),
            age_ms=age,
            updates=int(d.get("updates", 0)),
        )
    except Exception as exc:
        return Side(ok=False, error=f"v1 {type(exc).__name__}: {exc}")
    finally:
        conn.close()


class _SniHTTPSConnection(HTTPSConnection):
    """HTTPSConnection that connects to IP but uses the hostname for SNI + cert check."""

    def __init__(self, host_for_sni: str, ip: str, port: int, timeout: float):
        super().__init__(ip, port, timeout=timeout)
        self._host_for_sni = host_for_sni
        self.host = ip

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(sock, server_hostname=self._host_for_sni)


def fetch_v2(market: str, ip: str) -> Side:
    if ip == V2_HOST:
        conn: HTTPSConnection = HTTPSConnection(V2_HOST, 443, timeout=10)
    else:
        conn = _SniHTTPSConnection(V2_HOST, ip, 443, timeout=10)
    try:
        conn.request(
            "GET",
            f"/book/{market}",
            headers={"Host": V2_HOST, "User-Agent": "oms-v2-compare"},
        )
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        if resp.status != 200:
            return Side(ok=False, error=f"v2 status={resp.status} {body[:100]}")
        d = json.loads(body)
        bids = d.get("bids", [])
        asks = d.get("asks", [])
        tb = bids[0] if bids else (None, None)
        ta = asks[0] if asks else (None, None)
        return Side(
            ok=True,
            top_bid=float(tb[0]) if tb[0] is not None else None,
            top_bid_qty=float(tb[1]) if tb[1] is not None else None,
            top_ask=float(ta[0]) if ta[0] is not None else None,
            top_ask_qty=float(ta[1]) if ta[1] is not None else None,
            bid_levels=len(bids),
            ask_levels=len(asks),
            age_ms=d.get("age_ms"),
            updates=int(d.get("updates", 0)),
        )
    except Exception as exc:
        return Side(ok=False, error=f"v2 {type(exc).__name__}: {exc}")
    finally:
        conn.close()


def fmt(v: float | None, fmt_spec: str = ",.4f") -> str:
    if v is None:
        return "—"
    return format(v, fmt_spec)


def compare_row(market: str, v1: Side, v2: Side) -> dict[str, Any]:
    bid_diff = None
    ask_diff = None
    mid_diff_bp = None
    if v1.ok and v2.ok and v1.top_bid and v2.top_bid and v1.top_ask and v2.top_ask:
        v1_mid = (v1.top_bid + v1.top_ask) / 2
        v2_mid = (v2.top_bid + v2.top_ask) / 2
        if v1_mid > 0:
            mid_diff_bp = (v2_mid - v1_mid) / v1_mid * 10_000
        bid_diff = v2.top_bid - v1.top_bid
        ask_diff = v2.top_ask - v1.top_ask
    return {
        "market": market,
        "v1_ok": v1.ok,
        "v2_ok": v2.ok,
        "v1_bid": v1.top_bid,
        "v2_bid": v2.top_bid,
        "bid_diff": bid_diff,
        "v1_ask": v1.top_ask,
        "v2_ask": v2.top_ask,
        "ask_diff": ask_diff,
        "mid_diff_bp": mid_diff_bp,
        "v1_age_ms": v1.age_ms,
        "v2_age_ms": v2.age_ms,
        "v1_updates": v1.updates,
        "v2_updates": v2.updates,
        "v1_levels": f"{v1.bid_levels}/{v1.ask_levels}",
        "v2_levels": f"{v2.bid_levels}/{v2.ask_levels}",
        "v1_error": v1.error,
        "v2_error": v2.error,
    }


def print_round(rows: Iterable[dict[str, Any]]) -> None:
    print(
        f"\n{'market':<14} {'v1_bid':>12} {'v2_bid':>12} {'bid_diff':>9} "
        f"{'v1_ask':>12} {'v2_ask':>12} {'ask_diff':>9} "
        f"{'mid_bp':>7} {'v1_age':>7} {'v2_age':>7} {'levels':>11}"
    )
    print("-" * 130)
    for r in rows:
        line = (
            f"{r['market']:<14} "
            f"{fmt(r['v1_bid']):>12} {fmt(r['v2_bid']):>12} "
            f"{fmt(r['bid_diff']):>9} "
            f"{fmt(r['v1_ask']):>12} {fmt(r['v2_ask']):>12} "
            f"{fmt(r['ask_diff']):>9} "
            f"{fmt(r['mid_diff_bp'], '+.2f'):>7} "
            f"{str(r['v1_age_ms'] or '—'):>7} "
            f"{str(r['v2_age_ms'] or '—'):>7} "
            f"{r['v1_levels']}|{r['v2_levels']:<7}"
        )
        if r["v1_error"] or r["v2_error"]:
            line += f"  ERR: {r['v1_error'] or r['v2_error']}"
        print(line)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--markets", default=",".join(DEFAULT_MARKETS))
    p.add_argument("--resolve-via", default=None)
    p.add_argument("--interval", type=float, default=0)
    p.add_argument("--duration", type=float, default=0)
    args = p.parse_args()

    markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    ip = resolve(V2_HOST, args.resolve_via)
    print(f"[compare] V1={V1_HOST}:{V1_PORT}  V2={V2_HOST} ({ip})  markets={len(markets)}")

    start = time.time()
    any_error = False
    while True:
        ts = datetime.now(timezone.utc).isoformat()
        print(f"\n=== {ts} ===")
        rows = []
        for m in markets:
            v1 = fetch_v1(m)
            v2 = fetch_v2(m, ip)
            r = compare_row(m, v1, v2)
            rows.append(r)
            if not v1.ok or not v2.ok:
                any_error = True
        print_round(rows)
        if args.interval == 0 or (args.duration and time.time() - start >= args.duration):
            break
        time.sleep(args.interval)
    return 1 if any_error else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[compare] interrupted")
        sys.exit(130)
