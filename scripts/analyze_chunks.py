#!/usr/bin/env python3
"""Analyze chunk log to find imbalance source."""
import json, re, urllib.request

resp = urllib.request.urlopen("http://192.168.133.253:8002/fn/log?limit=300")
data = json.loads(resp.read())

maker_total = 0.0
taker_total = 0.0
phase = "?"

for e in data["entries"]:
    msg = e["msg"]
    cat = e["cat"]

    if "Exit COMPLETE" in msg:
        print(f"--- EXIT COMPLETE (maker={maker_total:.4f} taker={taker_total:.4f}) ---")
        maker_total = 0.0
        taker_total = 0.0
        phase = "ENTRY"
        continue

    if "Entry COMPLETE" in msg or "Entry FAILED" in msg:
        print(f"--- ENTRY done (maker={maker_total:.4f} taker={taker_total:.4f}) ---")
        print(f"    GAP = {maker_total - taker_total:.4f}")
        continue

    if cat == "CHUNK" and "DONE" in msg:
        m_maker = re.search(r"maker=([\d.]+)", msg)
        m_taker = re.search(r"taker=([\d.]+)", msg)
        mq = float(m_maker.group(1)) if m_maker else 0
        tq = float(m_taker.group(1)) if m_taker else 0
        maker_total += mq
        taker_total += tq
        gap = maker_total - taker_total
        print(f"[{e['seq']:>4d}] maker={mq:>8.4f} taker={tq:>8.4f}  | cum M={maker_total:>8.4f} T={taker_total:>8.4f} gap={gap:>+8.4f}")

    if "repair FILLED" in msg:
        m = re.search(r"qty=([\d.]+)", msg)
        if m:
            rq = float(m.group(1))
            taker_total += rq
            gap = maker_total - taker_total
            print(f"[{e['seq']:>4d}] REPAIR taker +{rq:.4f}           | cum M={maker_total:>8.4f} T={taker_total:>8.4f} gap={gap:>+8.4f}")

    if "repair" in msg and "FAILED" in msg:
        print(f"[{e['seq']:>4d}] {msg}")

print(f"\nFINAL TOTALS: maker={maker_total:.4f}  taker={taker_total:.4f}  gap={maker_total - taker_total:.4f}")
