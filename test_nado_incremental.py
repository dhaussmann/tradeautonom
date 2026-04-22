#!/usr/bin/env python3
"""
Step-by-step Nado WebSocket subscription test.
Starts with 1 subscription, adds one at a time, tests each.
"""

import asyncio
import json
import ssl
import time
from collections import defaultdict
from datetime import datetime

try:
    import websockets
    from websockets.extensions.permessage_deflate import ClientPerMessageDeflateFactory
except ImportError:
    print("ERROR: websockets package not installed")
    print("Run: pip install websockets")
    exit(1)

# SSL context
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

NADO_WS_URL = "wss://gateway.prod.nado.xyz/v1/subscribe"

# All 55 PERP symbols from Nado (fetched from API)
ALL_PERPS = [
    ("BTC-PERP", 2),
    ("ETH-PERP", 4),
    ("SOL-PERP", 8),
    ("XRP-PERP", 10),
    ("BNB-PERP", 14),
    ("HYPE-PERP", 16),
    ("ZEC-PERP", 18),
    ("FARTCOIN-PERP", 22),
    ("SUI-PERP", 24),
    ("XAUT-PERP", 28),
    ("PUMP-PERP", 30),
    ("TAO-PERP", 32),
    ("XMR-PERP", 34),
    ("LIT-PERP", 36),
    ("kPEPE-PERP", 38),
    ("PENGU-PERP", 40),
    ("USELESS-PERP", 42),
    ("SKR-PERP", 44),
    ("UNI-PERP", 46),
    ("ASTER-PERP", 48),
    ("XPL-PERP", 50),
    ("DOGE-PERP", 52),
    ("WLFI-PERP", 54),
    ("kBONK-PERP", 56),
    ("ZRO-PERP", 58),
    ("ADA-PERP", 60),
    ("ARB-PERP", 62),
    ("AVAX-PERP", 64),
    ("AXS-PERP", 66),
    ("BCH-PERP", 68),
    ("BERA-PERP", 70),
    ("ENA-PERP", 72),
    ("LINK-PERP", 74),
    ("LTC-PERP", 76),
    ("NEAR-PERP", 78),
    ("ONDO-PERP", 80),
    ("SKY-PERP", 82),
    ("VIRTUAL-PERP", 84),
    ("JUP-PERP", 86),
    ("XAG-PERP", 88),
    ("WTI-PERP", 90),
    ("EURUSD-PERP", 92),
    ("GBPUSD-PERP", 94),
    ("USDJPY-PERP", 96),
    ("QQQ-PERP", 98),
    ("SPY-PERP", 100),
    ("AAPL-PERP", 102),
    ("AMZN-PERP", 104),
    ("GOOGL-PERP", 106),
    ("META-PERP", 108),
    ("MSFT-PERP", 110),
    ("NVDA-PERP", 112),
    ("TSLA-PERP", 114),
    ("AAVE-PERP", 26),  # Note: ID 26 shared
]


async def test_incremental_subscriptions():
    """Test subscriptions incrementally, one at a time."""
    print("="*70)
    print("NADO STEP-BY-STEP SUBSCRIPTION TEST")
    print("="*70)
    print(f"Testing {len(ALL_PERPS)} PERP symbols incrementally")
    print(f"Adding 1 subscription at a time, verifying updates\n")
    
    extensions = [ClientPerMessageDeflateFactory()]
    stats = defaultdict(lambda: {"count": 0, "first_update": None, "last_update": None})
    active_symbols = []
    inactive_symbols = []
    
    # Test parameters
    SUBSCRIPTION_DELAY = 0.15  # 150ms between subscriptions
    VERIFICATION_TIME = 8  # seconds to wait for updates after each subscription
    
    async with websockets.connect(NADO_WS_URL, ssl=ssl_ctx, extensions=extensions, close_timeout=5) as ws:
        print(f"Connected to {NADO_WS_URL}\n")
        
        for idx, (symbol, product_id) in enumerate(ALL_PERPS, 1):
            print(f"\n{'='*70}")
            print(f"STEP {idx}/{len(ALL_PERPS)}: Adding {symbol} (product_id={product_id})")
            print(f"{'='*70}")
            
            # Subscribe to this symbol
            sub_msg = {
                "method": "subscribe",
                "stream": {"type": "book_depth", "product_id": product_id},
                "id": product_id,
            }
            await ws.send(json.dumps(sub_msg))
            print(f"  ✓ Subscribed")
            
            # Wait for verification period
            print(f"  Waiting {VERIFICATION_TIME}s for updates...")
            start_time = time.time()
            new_messages = 0
            
            while time.time() - start_time < VERIFICATION_TIME:
                try:
                    data = await asyncio.wait_for(ws.recv(), timeout=1)
                    msg = json.loads(data)
                    
                    if "bids" in msg or "asks" in msg:
                        msg_product_id = msg.get("product_id")
                        if msg_product_id:
                            # Find symbol for this product_id
                            sym = next((s for s, pid in ALL_PERPS if pid == msg_product_id), f"id_{msg_product_id}")
                            
                            if stats[msg_product_id]["count"] == 0:
                                stats[msg_product_id]["first_update"] = time.time()
                            stats[msg_product_id]["count"] += 1
                            stats[msg_product_id]["last_update"] = time.time()
                            new_messages += 1
                            
                except asyncio.TimeoutError:
                    continue
            
            # Check if the newly added symbol received updates
            if stats[product_id]["count"] > 0:
                print(f"  ✓ ACTIVE: Received {stats[product_id]['count']} updates")
                active_symbols.append((symbol, product_id, stats[product_id]["count"]))
            else:
                print(f"  ✗ INACTIVE: No updates received")
                inactive_symbols.append((symbol, product_id))
            
            # Show current status
            print(f"\n  Current Status:")
            print(f"    Active: {len(active_symbols)}/{idx}")
            print(f"    Inactive: {len(inactive_symbols)}/{idx}")
            print(f"    Total messages this step: {new_messages}")
            
            # Small delay before next subscription
            if idx < len(ALL_PERPS):
                await asyncio.sleep(SUBSCRIPTION_DELAY)
    
    # Final Summary
    print("\n" + "="*70)
    print("TEST COMPLETE")
    print("="*70)
    
    print(f"\nACTIVE SYMBOLS ({len(active_symbols)}/{len(ALL_PERPS)}):")
    for symbol, pid, count in sorted(active_symbols, key=lambda x: -x[2]):
        print(f"  ✓ {symbol:20s} (id={pid:3d}): {count:4d} updates")
    
    print(f"\nINACTIVE SYMBOLS ({len(inactive_symbols)}/{len(ALL_PERPS)}):")
    for symbol, pid in sorted(inactive_symbols):
        print(f"  ✗ {symbol:20s} (id={pid:3d}): No updates")
    
    print(f"\nSUMMARY:")
    print(f"  Total tested: {len(ALL_PERPS)}")
    print(f"  Active: {len(active_symbols)}")
    print(f"  Inactive: {len(inactive_symbols)}")
    print(f"  Success rate: {len(active_symbols)/len(ALL_PERPS)*100:.1f}%")
    
    return len(inactive_symbols) == 0


async def test_connection_limit():
    """Test if there's a maximum subscription limit."""
    print("\n" + "="*70)
    print("CONNECTION LIMIT TEST")
    print("="*70)
    
    extensions = [ClientPerMessageDeflateFactory()]
    
    # Try subscribing to all at once with different delays
    for delay_ms in [0, 10, 50, 100, 200]:
        print(f"\nTesting with {delay_ms}ms delay between subscriptions...")
        
        async with websockets.connect(NADO_WS_URL, ssl=ssl_ctx, extensions=extensions, close_timeout=5) as ws:
            success_count = 0
            
            for symbol, product_id in ALL_PERPS:
                sub_msg = {
                    "method": "subscribe",
                    "stream": {"type": "book_depth", "product_id": product_id},
                    "id": product_id,
                }
                await ws.send(json.dumps(sub_msg))
                
                if delay_ms > 0:
                    await asyncio.sleep(delay_ms / 1000)
            
            # Wait a bit and count responses
            print(f"  Subscribed to {len(ALL_PERPS)} symbols, waiting 10s...")
            active_products = set()
            
            start_time = time.time()
            while time.time() - start_time < 10:
                try:
                    data = await asyncio.wait_for(ws.recv(), timeout=1)
                    msg = json.loads(data)
                    if "bids" in msg or "asks" in msg:
                        active_products.add(msg.get("product_id"))
                except asyncio.TimeoutError:
                    continue
            
            print(f"  Active products: {len(active_products)}/{len(ALL_PERPS)}")
            
            if len(active_products) >= 40:  # If we get at least 40, consider it successful
                print(f"  ✓ Delay of {delay_ms}ms works well")
                break
            else:
                print(f"  ✗ Delay of {delay_ms}ms may be too fast")


async def main():
    """Run all tests."""
    try:
        # Test 1: Incremental subscriptions
        success = await test_incremental_subscriptions()
        
        # Test 2: Connection limit (only if incremental test completed)
        await test_connection_limit()
        
        print("\n" + "="*70)
        print(f"OVERALL: {'ALL TESTS PASSED' if success else 'SOME TESTS FAILED'}")
        print("="*70)
        
    except Exception as e:
        print(f"\nERROR: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    print(f"Starting test at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"This will take approximately {len(ALL_PERPS) * 10 / 60:.1f} minutes\n")
    asyncio.run(main())
