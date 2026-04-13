#!/usr/bin/env python3
"""Long-running test: Does the CF Worker proxy avoid Variational 403 errors?

Usage:
    VARIATIONAL_JWT='ey...' python3 tests/test_cf_worker_proxy.py
    VARIATIONAL_JWT='ey...' python3 tests/test_cf_worker_proxy.py --interval 10
"""

import argparse
import os
import signal
import sys
import time
from datetime import datetime

import requests

WORKER_URL = "https://proxy.defitool.de/api/portfolio"
DIRECT_URL = "https://omni.variational.io/api/portfolio"


def build_headers_and_cookies(token: str) -> tuple[dict, dict]:
    import base64, json
    headers = {"Content-Type": "application/json"}
    cookies = {"vr-token": token}
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        data = json.loads(base64.b64decode(payload_b64))
        addr = data.get("address", "")
        if addr:
            headers["vr-connected-address"] = addr
    except Exception:
        pass
    return headers, cookies


def probe(url: str, headers: dict, cookies: dict) -> tuple[int, float, str]:
    """Send GET request, return (status_code, latency_ms, body_preview)."""
    t0 = time.monotonic()
    try:
        resp = requests.get(url, headers=headers, cookies=cookies, timeout=15)
        latency = (time.monotonic() - t0) * 1000
        body = resp.text[:200]
        return resp.status_code, latency, body
    except requests.RequestException as exc:
        latency = (time.monotonic() - t0) * 1000
        return -1, latency, str(exc)[:200]


def main():
    parser = argparse.ArgumentParser(description="CF Worker 403 evaluation test")
    parser.add_argument("--interval", type=float, default=5.0, help="Seconds between probes (default 5)")
    args = parser.parse_args()

    token = os.environ.get("VARIATIONAL_JWT", "")
    if not token:
        print("ERROR: Set VARIATIONAL_JWT environment variable")
        sys.exit(1)

    headers, cookies = build_headers_and_cookies(token)

    stats = {"worker_total": 0, "worker_403": 0, "worker_ok": 0, "worker_other": 0}
    running = True

    def on_signal(*_):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    print(f"Polling: {WORKER_URL}")
    print(f"Interval: {args.interval}s")
    print("=" * 80)

    while running:
        now = datetime.now().strftime("%H:%M:%S")

        w_status, w_ms, body = probe(WORKER_URL, headers, cookies)
        stats["worker_total"] += 1
        if w_status == 403:
            stats["worker_403"] += 1
        elif 200 <= w_status < 300:
            stats["worker_ok"] += 1
        else:
            stats["worker_other"] += 1

        total = stats["worker_total"]
        ok_rate = stats["worker_ok"] / total * 100 if total else 0

        print(f"\n[{now}] GET {WORKER_URL}")
        print(f"  Status: {w_status}  Latency: {w_ms:.0f}ms  (OK: {stats['worker_ok']}/{total} = {ok_rate:.1f}%  403s: {stats['worker_403']})")
        print(f"  Response: {body}")

        time.sleep(args.interval)

    # Final summary
    total = stats["worker_total"]
    print("\n" + "=" * 80)
    print(f"SUMMARY after {total} requests:")
    print(f"  OK (2xx):  {stats['worker_ok']}  ({stats['worker_ok']/total*100:.1f}%)" if total else "")
    print(f"  403:       {stats['worker_403']}  ({stats['worker_403']/total*100:.1f}%)" if total else "")
    print(f"  Other:     {stats['worker_other']}  ({stats['worker_other']/total*100:.1f}%)" if total else "")
    print("=" * 80)


if __name__ == "__main__":
    main()
