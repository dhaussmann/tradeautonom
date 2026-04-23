#!/usr/bin/env python3
"""
OMS-v2 PoC stability watchdog.

Polls /health + /markets every INTERVAL_S seconds and writes one CSV line
per poll plus a log line per anomaly. Designed to run for hours or days.

Anomalies flagged:
  - ws_state != "connected"
  - reconnect_attempts increased since last poll
  - markets_tracked dropped by more than DROP_THRESHOLD
  - total_updates did not grow between polls (stream silent)
  - uptime_ms reset (DO was evicted / restarted)
  - /health or /markets HTTP status != 200
  - age_ms for all sampled hot markets > STALE_AGE_MS

Usage:
  python3 watchdog.py
  python3 watchdog.py --host oms-v2-poc.defitool.de --interval 30

  # Bypass WARP / Zero Trust Gateway via explicit IP resolution:
  python3 watchdog.py --resolve-via 1.1.1.1

Outputs (relative to CWD):
  watchdog.csv       one row per poll
  watchdog.log       human-readable per-poll line + anomalies
"""
from __future__ import annotations

import argparse
import csv
import json
import socket
import ssl
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.client import HTTPSConnection
from pathlib import Path
from typing import Any

DEFAULT_HOST = "oms-v2-poc.defitool.de"
DEFAULT_INTERVAL = 30  # seconds
HOT_MARKETS = ("BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD")
STALE_AGE_MS = 5_000
DROP_THRESHOLD = 5  # markets dropping by more than this triggers an anomaly


@dataclass
class PollResult:
    ts: datetime
    ok: bool
    ws_state: str = ""
    reconnect_attempts: int = 0
    markets_tracked: int = 0
    total_updates: int = 0
    uptime_ms: int = 0
    last_message_age_ms: int | None = None
    hot_ages_ms: dict[str, int | None] = field(default_factory=dict)
    error: str = ""


def resolve(host: str, via: str | None) -> str:
    """Return an IP for `host`. If `via` is set, query that DNS server directly."""
    if via is None:
        return host  # let the system resolver handle it
    # Use dnspython if available, otherwise socket.gethostbyname (system resolver)
    try:
        import subprocess

        result = subprocess.run(
            ["dig", "+short", f"@{via}", host],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        lines = [
            line.strip()
            for line in result.stdout.splitlines()
            if line.strip() and not line.strip().endswith(".")
        ]
        if lines:
            return lines[0]
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # fall back to system
    return socket.gethostbyname(host)


class _SniHTTPSConnection(HTTPSConnection):
    """HTTPSConnection that connects to IP but uses `host` for SNI + cert verification."""

    def __init__(self, host_for_sni: str, ip: str, port: int, timeout: float):
        super().__init__(ip, port, timeout=timeout)
        self._host_for_sni = host_for_sni
        self.host = ip  # for connect()

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        ctx = ssl.create_default_context()
        # TLS handshake with SNI = our virtual hostname, so the cert validates
        self.sock = ctx.wrap_socket(sock, server_hostname=self._host_for_sni)


def http_get_json(
    host: str, ip: str, path: str, timeout: float = 10.0
) -> tuple[int, dict[str, Any] | None, str]:
    """GET https://{host}{path}, connecting to {ip}. Returns (status, json, err)."""
    if ip == host:
        # No explicit resolution; use the default connector.
        conn: HTTPSConnection = HTTPSConnection(host, 443, timeout=timeout)
    else:
        conn = _SniHTTPSConnection(host, ip, 443, timeout=timeout)
    try:
        conn.request("GET", path, headers={"Host": host, "User-Agent": "oms-v2-watchdog"})
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="replace")
        if resp.status != 200:
            return resp.status, None, f"status={resp.status} body={body[:200]}"
        try:
            return 200, json.loads(body), ""
        except json.JSONDecodeError as exc:
            return 200, None, f"json_decode={exc} body={body[:200]}"
    except Exception as exc:
        return 0, None, f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


def poll(host: str, ip: str) -> PollResult:
    now = datetime.now(timezone.utc)
    status, health, err = http_get_json(host, ip, "/health")
    if status != 200 or health is None:
        return PollResult(ts=now, ok=False, error=f"/health {err}")

    _, markets, err_m = http_get_json(host, ip, "/markets")
    hot_ages: dict[str, int | None] = {}
    if markets and isinstance(markets.get("markets"), list):
        by_sym = {m["symbol"]: m for m in markets["markets"] if isinstance(m, dict)}
        for sym in HOT_MARKETS:
            entry = by_sym.get(sym)
            hot_ages[sym] = entry["age_ms"] if entry else None
    else:
        for sym in HOT_MARKETS:
            hot_ages[sym] = None

    last_msg_ms = health.get("last_message_ms")
    now_ms = int(time.time() * 1000)
    last_age = (now_ms - last_msg_ms) if last_msg_ms else None

    return PollResult(
        ts=now,
        ok=True,
        ws_state=str(health.get("ws_state", "")),
        reconnect_attempts=int(health.get("reconnect_attempts", 0)),
        markets_tracked=int(health.get("markets_tracked", 0)),
        total_updates=int(health.get("total_updates", 0)),
        uptime_ms=int(health.get("uptime_ms", 0)),
        last_message_age_ms=last_age,
        hot_ages_ms=hot_ages,
        error=err_m,
    )


def detect_anomalies(prev: PollResult | None, cur: PollResult) -> list[str]:
    out: list[str] = []
    if not cur.ok:
        out.append(f"POLL_FAILED: {cur.error}")
        return out

    if cur.ws_state != "connected":
        out.append(f"WS_NOT_CONNECTED: state={cur.ws_state!r}")

    if cur.last_message_age_ms is not None and cur.last_message_age_ms > 30_000:
        out.append(f"NO_MESSAGES_30S: last_message_age_ms={cur.last_message_age_ms}")

    hot_stale = [
        sym
        for sym, age in cur.hot_ages_ms.items()
        if age is not None and age > STALE_AGE_MS
    ]
    if len(hot_stale) == len(HOT_MARKETS):
        out.append(f"ALL_HOT_MARKETS_STALE: {cur.hot_ages_ms}")

    if prev is not None and prev.ok:
        if cur.reconnect_attempts > prev.reconnect_attempts:
            out.append(
                f"RECONNECT: {prev.reconnect_attempts} -> {cur.reconnect_attempts}"
            )
        if cur.uptime_ms < prev.uptime_ms:
            out.append(
                f"UPTIME_RESET: {prev.uptime_ms} -> {cur.uptime_ms} (DO restarted)"
            )
        if cur.markets_tracked < prev.markets_tracked - DROP_THRESHOLD:
            out.append(
                f"MARKETS_DROPPED: {prev.markets_tracked} -> {cur.markets_tracked}"
            )
        if cur.total_updates <= prev.total_updates and cur.uptime_ms >= prev.uptime_ms:
            # same DO instance, no new messages
            out.append(
                f"STREAM_SILENT: total_updates {prev.total_updates} -> {cur.total_updates}"
            )
    return out


def human(result: PollResult) -> str:
    if not result.ok:
        return f"[FAIL] {result.error}"
    hot = " ".join(
        f"{sym}={age}ms" if age is not None else f"{sym}=-"
        for sym, age in result.hot_ages_ms.items()
    )
    return (
        f"ws={result.ws_state:<11} "
        f"markets={result.markets_tracked:>3} "
        f"updates={result.total_updates:>7} "
        f"reconn={result.reconnect_attempts} "
        f"uptime={result.uptime_ms // 1000}s "
        f"last_msg={result.last_message_age_ms}ms "
        f"hot=[{hot}]"
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--interval", type=float, default=DEFAULT_INTERVAL)
    p.add_argument(
        "--resolve-via",
        default=None,
        help="DNS server to resolve through (e.g. 1.1.1.1). Bypasses local DNS filters.",
    )
    p.add_argument("--csv", default="watchdog.csv")
    p.add_argument("--log", default="watchdog.log")
    p.add_argument(
        "--duration",
        type=float,
        default=0,
        help="Stop after N seconds (0 = forever)",
    )
    args = p.parse_args()

    ip = resolve(args.host, args.resolve_via)
    print(f"[watchdog] host={args.host} -> ip={ip}  interval={args.interval}s", flush=True)

    csv_path = Path(args.csv)
    log_path = Path(args.log)
    new_csv = not csv_path.exists() or csv_path.stat().st_size == 0

    start = time.time()
    prev: PollResult | None = None

    with csv_path.open("a", newline="") as csv_f, log_path.open("a") as log_f:
        writer = csv.writer(csv_f)
        if new_csv:
            writer.writerow(
                [
                    "ts",
                    "ok",
                    "ws_state",
                    "reconnect_attempts",
                    "markets_tracked",
                    "total_updates",
                    "uptime_ms",
                    "last_message_age_ms",
                    *(f"age_{s}" for s in HOT_MARKETS),
                    "anomalies",
                    "error",
                ]
            )
            csv_f.flush()

        while True:
            cur = poll(args.host, ip)
            anomalies = detect_anomalies(prev, cur)

            row = [
                cur.ts.isoformat(),
                int(cur.ok),
                cur.ws_state,
                cur.reconnect_attempts,
                cur.markets_tracked,
                cur.total_updates,
                cur.uptime_ms,
                cur.last_message_age_ms if cur.last_message_age_ms is not None else "",
                *(
                    cur.hot_ages_ms.get(s) if cur.hot_ages_ms.get(s) is not None else ""
                    for s in HOT_MARKETS
                ),
                ";".join(anomalies),
                cur.error,
            ]
            writer.writerow(row)
            csv_f.flush()

            line = f"{cur.ts.isoformat()}  {human(cur)}"
            print(line, flush=True)
            log_f.write(line + "\n")
            for a in anomalies:
                msg = f"{cur.ts.isoformat()}  !! {a}"
                print(msg, flush=True)
                log_f.write(msg + "\n")
            log_f.flush()

            prev = cur
            if args.duration and (time.time() - start) >= args.duration:
                print("[watchdog] duration reached, exiting", flush=True)
                return 0
            time.sleep(args.interval)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[watchdog] interrupted", flush=True)
        sys.exit(130)
