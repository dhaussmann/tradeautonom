#!/usr/bin/env python3
"""
Local test for Nado WebSocket shared connection.
Tests subscribing to multiple products on a single WebSocket connection.
"""

import asyncio
import json
import ssl
import time
from collections import defaultdict
from websockets.extensions.permessage_deflate import ClientPerMessageDeflateFactory

try:
    import websockets
except ImportError:
    print("ERROR: websockets package not installed")
    print("Run: pip install websockets")
    exit(1)

# Nado configuration
NADO_WS_URL = "wss://gateway.prod.nado.xyz/v1/subscribe"
NADO_REST_URL = "https://gateway.prod.nado.xyz"

# SSL context (disable verification for testing)
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

# Test products (subset for initial testing)
TEST_PRODUCTS = {
    "XRP-PERP": 10,
    "BTC-PERP": 1,
    "ETH-PERP": 2,
    "SOL-PERP": 3,
    "ADA-PERP": 11,
}

# Full product list (will use after initial test)
ALL_PRODUCTS = {
    "BTC-PERP": 1,
    "ETH-PERP": 2,
    "SOL-PERP": 3,
    "DOGE-PERP": 4,
    "XRP-PERP": 10,
    "ADA-PERP": 11,
    "BNB-PERP": 14,
    "LTC-PERP": 15,
    "LINK-PERP": 16,
    "BCH-PERP": 18,
    "XMR-PERP": 22,
    "AVAX-PERP": 26,
    "AAVE-PERP": 26,  # Note: may have duplicate
    "ATOM-PERP": 28,
    "DOT-PERP": 30,
    "UNI-PERP": 32,
    "FIL-PERP": 36,
    "LIT-PERP": 36,   # Note: may have duplicate
    "NEAR-PERP": 48,
    "SUI-PERP": 52,
    "ARB-PERP": 56,
    "OP-PERP": 62,
    "SEI-PERP": 66,
    "TIA-PERP": 68,
    "DYM-PERP": 70,
    "STRK-PERP": 72,
    "AEVO-PERP": 74,
    "ENA-PERP": 86,
    "WIF-PERP": 90,
    "WLD-PERP": 92,
    "ARKM-PERP": 94,
    "TNSR-PERP": 96,
    "ZEUS-PERP": 98,
    "TAO-PERP": 100,
    "AAPL-PERP": 102,
    "GOOGL-PERP": 104,
    "TSLA-PERP": 106,
    "AMZN-PERP": 108,
    "NVDA-PERP": 110,
    "META-PERP": 112,
    "MSFT-PERP": 114,
    "NFLX-PERP": 116,
    "AMD-PERP": 118,
    "INTC-PERP": 120,
    "SPY-PERP": 122,
    "QQQ-PERP": 124,
    "IWM-PERP": 126,
    "VIX-PERP": 128,
    "USO-PERP": 130,
    "GLD-PERP": 132,
    "TLT-PERP": 134,
    "HYG-PERP": 136,
    "LQD-PERP": 138,
}


async def test_single_subscription():
    """Test subscribing to a single product."""
    print("\n" + "="*60)
    print("TEST 1: Single Product Subscription")
    print("="*60)
    
    extensions = [ClientPerMessageDeflateFactory()]
    
    async with websockets.connect(NADO_WS_URL, ssl=ssl_ctx, extensions=extensions, close_timeout=5) as ws:
        print(f"Connected to {NADO_WS_URL}")
        
        # Subscribe to XRP
        product_id = 10
        sub_msg = {
            "method": "subscribe",
            "stream": {"type": "book_depth", "product_id": product_id},
            "id": product_id,
        }
        await ws.send(json.dumps(sub_msg))
        print(f"Sent subscription for product_id={product_id}")
        
        # Receive response
        resp = await asyncio.wait_for(ws.recv(), timeout=5)
        resp_data = json.loads(resp)
        print(f"Response: {resp_data}")
        
        # Receive book updates
        msg_count = 0
        for i in range(5):
            try:
                data = await asyncio.wait_for(ws.recv(), timeout=3)
                msg = json.loads(data)
                if "bids" in msg or "asks" in msg:
                    msg_count += 1
                    print(f"Message {msg_count}: product_id={msg.get('product_id')}, "
                          f"bids={len(msg.get('bids', []))}, asks={len(msg.get('asks', []))}, "
                          f"max_ts={msg.get('max_timestamp', 'N/A')[:20]}...")
                else:
                    print(f"Non-book message: {list(msg.keys())}")
            except asyncio.TimeoutError:
                print(f"Timeout after {i} messages")
                break
        
        print(f"\nReceived {msg_count} book updates")
        return msg_count > 0


async def test_multiple_subscriptions():
    """Test subscribing to multiple products on one connection."""
    print("\n" + "="*60)
    print("TEST 2: Multiple Product Subscriptions (5 products)")
    print("="*60)
    
    extensions = [ClientPerMessageDeflateFactory()]
    stats = defaultdict(lambda: {"count": 0, "last_ts": None})
    
    async with websockets.connect(NADO_WS_URL, ssl=ssl_ctx, extensions=extensions, close_timeout=5) as ws:
        print(f"Connected to {NADO_WS_URL}")
        
        # Subscribe to all test products
        for symbol, product_id in TEST_PRODUCTS.items():
            sub_msg = {
                "method": "subscribe",
                "stream": {"type": "book_depth", "product_id": product_id},
                "id": product_id,
            }
            await ws.send(json.dumps(sub_msg))
            print(f"Subscribed to {symbol} (product_id={product_id})")
            await asyncio.sleep(0.1)  # Brief delay between subscriptions
        
        print(f"\nListening for messages from {len(TEST_PRODUCTS)} products...")
        
        # Collect messages for 10 seconds
        start_time = time.time()
        while time.time() - start_time < 10:
            try:
                data = await asyncio.wait_for(ws.recv(), timeout=2)
                msg = json.loads(data)
                
                if "bids" in msg or "asks" in msg:
                    product_id = msg.get("product_id")
                    stats[product_id]["count"] += 1
                    stats[product_id]["last_ts"] = msg.get("max_timestamp")
                    
                    if stats[product_id]["count"] <= 2:
                        symbol = [s for s, pid in TEST_PRODUCTS.items() if pid == product_id]
                        symbol = symbol[0] if symbol else f"product_{product_id}"
                        print(f"  {symbol}: update #{stats[product_id]['count']}, "
                              f"bids={len(msg.get('bids', []))}, asks={len(msg.get('asks', []))}")
                
            except asyncio.TimeoutError:
                continue
        
        print(f"\n--- Results ---")
        print(f"Products with updates:")
        for symbol, product_id in TEST_PRODUCTS.items():
            count = stats[product_id]["count"]
            print(f"  {symbol} (id={product_id}): {count} updates")
        
        total_updates = sum(s["count"] for s in stats.values())
        print(f"\nTotal updates received: {total_updates}")
        return len([s for s in stats.values() if s["count"] > 0]) >= 3


async def test_all_products():
    """Test subscribing to all 52 products."""
    print("\n" + "="*60)
    print("TEST 3: All 52 Products")
    print("="*60)
    
    extensions = [ClientPerMessageDeflateFactory()]
    stats = defaultdict(lambda: {"count": 0, "first_ts": None, "last_ts": None})
    
    async with websockets.connect(NADO_WS_URL, ssl=ssl_ctx, extensions=extensions, close_timeout=5) as ws:
        print(f"Connected to {NADO_WS_URL}")
        print(f"Subscribing to {len(ALL_PRODUCTS)} products...")
        
        # Subscribe to all products
        for symbol, product_id in ALL_PRODUCTS.items():
            sub_msg = {
                "method": "subscribe",
                "stream": {"type": "book_depth", "product_id": product_id},
                "id": product_id,
            }
            await ws.send(json.dumps(sub_msg))
        
        print("All subscriptions sent. Listening for 30 seconds...")
        
        # Collect messages for 30 seconds
        start_time = time.time()
        last_status_time = start_time
        
        while time.time() - start_time < 30:
            try:
                data = await asyncio.wait_for(ws.recv(), timeout=1)
                msg = json.loads(data)
                
                if "bids" in msg or "asks" in msg:
                    product_id = msg.get("product_id")
                    if product_id:
                        if stats[product_id]["count"] == 0:
                            stats[product_id]["first_ts"] = time.time()
                        stats[product_id]["count"] += 1
                        stats[product_id]["last_ts"] = time.time()
                
            except asyncio.TimeoutError:
                pass
            
            # Print status every 5 seconds
            if time.time() - last_status_time >= 5:
                active_products = len([s for s in stats.values() if s["count"] > 0])
                total_messages = sum(s["count"] for s in stats.values())
                elapsed = int(time.time() - start_time)
                print(f"  [{elapsed}s] Active: {active_products}/{len(ALL_PRODUCTS)}, "
                      f"Total messages: {total_messages}")
                last_status_time = time.time()
        
        print(f"\n--- Final Results ---")
        active_count = len([s for s in stats.values() if s["count"] > 0])
        print(f"Products receiving updates: {active_count}/{len(ALL_PRODUCTS)}")
        
        # Show per-product breakdown
        print(f"\nTop 10 most active products:")
        sorted_products = sorted(stats.items(), key=lambda x: x[1]["count"], reverse=True)[:10]
        for product_id, data in sorted_products:
            symbol = [s for s, pid in ALL_PRODUCTS.items() if pid == product_id]
            symbol = symbol[0] if symbol else f"id_{product_id}"
            print(f"  {symbol:20s}: {data['count']:4d} updates")
        
        return active_count >= 40  # Expect at least 40 of 52 to be active


async def test_sequence_numbers():
    """Test sequence number handling per product."""
    print("\n" + "="*60)
    print("TEST 4: Sequence Number Handling")
    print("="*60)
    
    extensions = [ClientPerMessageDeflateFactory()]
    last_max_ts = {}
    gap_count = 0
    
    async with websockets.connect(NADO_WS_URL, ssl=ssl_ctx, extensions=extensions, close_timeout=5) as ws:
        print(f"Connected to {NADO_WS_URL}")
        
        # Subscribe to a few products
        test_pids = [10, 1, 2]  # XRP, BTC, ETH
        for product_id in test_pids:
            sub_msg = {
                "method": "subscribe",
                "stream": {"type": "book_depth", "product_id": product_id},
                "id": product_id,
            }
            await ws.send(json.dumps(sub_msg))
        
        print(f"Subscribed to {len(test_pids)} products")
        print("Checking sequence numbers for 10 seconds...")
        
        start_time = time.time()
        while time.time() - start_time < 10:
            try:
                data = await asyncio.wait_for(ws.recv(), timeout=1)
                msg = json.loads(data)
                
                if "bids" in msg or "asks" in msg:
                    product_id = msg.get("product_id")
                    msg_max_ts = msg.get("max_timestamp", "0")
                    msg_last_max_ts = msg.get("last_max_timestamp", "0")
                    
                    # Check for gaps
                    if product_id in last_max_ts:
                        expected_last = last_max_ts[product_id]
                        if msg_last_max_ts != expected_last:
                            gap_count += 1
                            print(f"  GAP detected for product {product_id}: "
                                  f"expected {expected_last[:20]}..., got {msg_last_max_ts[:20]}...")
                    
                    last_max_ts[product_id] = msg_max_ts
                    
            except asyncio.TimeoutError:
                pass
        
        print(f"\nSequence gaps detected: {gap_count}")
        print(f"Products tracked: {len(last_max_ts)}")
        return gap_count == 0


async def main():
    """Run all tests."""
    print("="*60)
    print("NADO WEBSOCKET MULTIPLEXING TEST")
    print("="*60)
    print(f"Target: {NADO_WS_URL}")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    results = []
    
    try:
        # Test 1: Single subscription
        results.append(("Single Subscription", await test_single_subscription()))
        
        # Test 2: Multiple subscriptions (5 products)
        results.append(("Multiple Subscriptions (5)", await test_multiple_subscriptions()))
        
        # Test 3: All 52 products
        results.append(("All 52 Products", await test_all_products()))
        
        # Test 4: Sequence numbers
        results.append(("Sequence Numbers", await test_sequence_numbers()))
        
    except Exception as e:
        print(f"\nERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    
    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    for name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status}: {name}")
    
    all_passed = all(r[1] for r in results)
    print(f"\nOverall: {'ALL TESTS PASSED' if all_passed else 'SOME TESTS FAILED'}")
    return all_passed


if __name__ == "__main__":
    success = asyncio.run(main())
    exit(0 if success else 1)
