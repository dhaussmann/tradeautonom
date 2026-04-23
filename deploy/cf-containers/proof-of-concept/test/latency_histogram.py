#!/usr/bin/env python3
"""
Sample /book/{market} N times and print a simple age_ms histogram.

Use this to compare V2 PoC vs V1 Photon OMS latency side-by-side over
a few hundred samples.

Usage:
  python3 latency_histogram.py                         # default: V2 BTC-USD 60 samples
  python3 latency_histogram.py --market SOL-USD --samples 200
  python3 latency_histogram.py --source v1             # query Photon OMS
  python3 latency_histogram.py --resolve-via 1.1.1.1   # bypass WARP for V2
"""
from __future__ import annotations

import argparse
import json
import socket
import ssl
import subprocess
import sys
import time
from http.client import HTTPConnection, HTTPSConnection

V2_HOST = "oms-v2-poc.defitool.de"
V1_HOST = "192.168.133.100"
V1_PORT = 8099


class _SniHTTPSConnection(HTTPSConnection):
    def __init__(self, host_for_sni: str, ip: str, port: int, timeout: float):
        super().__init__(ip, port, timeout=timeout)
        self._host_for_sni = host_for_sni
        self.host = ip

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        ctx = ssl.create_default_context()
        self.sock = ctx.wrap_socket(sock, server_hostname=self._host_for_sni)


def resolve(host: str, via: str | None) -> str:
    if via is None:
        return host
    try:
        r = subprocess.run(
            ["dig", "+short", f"@{via}", host],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        for line in r.stdout.splitlines():
            line = line.strip()
            if line and not line.endswith("."):
                return line
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return socket.gethostbyname(host)


def sample_v2(market: str, ip: str) -> int | None:
    conn: HTTPSConnection
    if ip == V2_HOST:
        conn = HTTPSConnection(V2_HOST, 443, timeout=5)
    else:
        conn = _SniHTTPSConnection(V2_HOST, ip, 443, 5)
    try:
        conn.request(
            "GET", f"/book/{market}", headers={"Host": V2_HOST, "User-Agent": "oms-v2-latency"}
        )
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        d = json.loads(resp.read().decode("utf-8"))
        return d.get("age_ms")
    except Exception:
        return None
    finally:
        conn.close()


def sample_v1(market: str) -> int | None:
    conn = HTTPConnection(V1_HOST, V1_PORT, timeout=2)
    try:
        conn.request("GET", f"/book/extended/{market}")
        resp = conn.getresponse()
        if resp.status != 200:
            return None
        d = json.loads(resp.read().decode("utf-8"))
        ts = d.get("timestamp_ms", 0)
        if not ts:
            return None
        return int(time.time() * 1000 - ts)
    except Exception:
        return None
    finally:
        conn.close()


def histogram(samples: list[int], buckets: list[int]) -> None:
    """Simple ASCII histogram. `buckets` is an increasing list of upper bounds (ms)."""
    if not samples:
        print("(no valid samples)")
        return
    counts = [0] * (len(buckets) + 1)
    for s in samples:
        placed = False
        for i, b in enumerate(buckets):
            if s <= b:
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    max_count = max(counts) or 1
    print(f"n={len(samples)}  min={min(samples)}  max={max(samples)}  median={sorted(samples)[len(samples)//2]}")
    p90 = sorted(samples)[int(len(samples) * 0.9)]
    p99 = sorted(samples)[min(int(len(samples) * 0.99), len(samples) - 1)]
    print(f"p90={p90}  p99={p99}  mean={sum(samples)/len(samples):.1f}")
    print()
    for i, b in enumerate(buckets):
        label = f"<={b:>5}ms"
        bar = "#" * int(40 * counts[i] / max_count)
        print(f"  {label}: {counts[i]:>4}  {bar}")
    label = f" >{buckets[-1]:>5}ms"
    bar = "#" * int(40 * counts[-1] / max_count)
    print(f"  {label}: {counts[-1]:>4}  {bar}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source", choices=("v1", "v2"), default="v2")
    p.add_argument("--market", default="BTC-USD")
    p.add_argument("--samples", type=int, default=60)
    p.add_argument("--interval", type=float, default=1.0)
    p.add_argument("--resolve-via", default=None)
    args = p.parse_args()

    if args.source == "v2":
        ip = resolve(V2_HOST, args.resolve_via)
        print(f"source=V2  host={V2_HOST}  ip={ip}  market={args.market}  samples={args.samples}")
        sampler = lambda: sample_v2(args.market, ip)  # noqa: E731
    else:
        print(f"source=V1  host={V1_HOST}:{V1_PORT}  market={args.market}  samples={args.samples}")
        sampler = lambda: sample_v1(args.market)  # noqa: E731

    results: list[int] = []
    errors = 0
    start = time.time()
    for i in range(args.samples):
        age = sampler()
        if age is None:
            errors += 1
        else:
            results.append(age)
        if i < args.samples - 1:
            time.sleep(args.interval)
    elapsed = time.time() - start
    print(f"completed in {elapsed:.1f}s  errors={errors}")
    print()
    histogram(results, buckets=[100, 200, 500, 1_000, 2_000, 5_000, 10_000])
    return 0 if errors == 0 else 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
