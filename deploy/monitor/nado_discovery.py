"""
Nado Discovery - Handles daily discovery of new PERP symbols and testing.
"""

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

import httpx

from nado_watchdog import NadoWatchdog, SymbolState, get_watchdog

logger = logging.getLogger(__name__)


class NadoDiscovery:
    """Discovers and tests new Nado PERP symbols."""
    
    def __init__(
        self,
        api_url: str = "https://gateway.prod.nado.xyz",
        test_duration_seconds: int = 30,
        watchdog: Optional[NadoWatchdog] = None,
    ):
        self.api_url = api_url
        self.test_duration = test_duration_seconds
        self._watchdog = watchdog or get_watchdog()
        self._discovered_product_ids: Dict[str, int] = {}
        
        logger.info(f"NadoDiscovery initialized with API: {api_url}")
    
    async def fetch_all_perp_symbols(self) -> Dict[str, int]:
        """Fetch all PERP symbols from Nado API."""
        url = f"{self.api_url}/symbols"
        
        try:
            async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
                response = await client.get(url, headers={"Accept-Encoding": "gzip"})
                response.raise_for_status()
                symbols_data = response.json()
            
            # Filter for PERP symbols
            perps = {}
            for item in symbols_data:
                symbol = item.get("symbol", "")
                product_id = item.get("product_id")
                
                if symbol.endswith("-PERP") and product_id is not None:
                    perps[symbol] = product_id
            
            self._discovered_product_ids = perps
            logger.info(f"Discovery: Found {len(perps)} PERP symbols from API")
            return perps
            
        except Exception as e:
            logger.error(f"Discovery: Failed to fetch symbols: {e}")
            return {}
    
    async def find_new_symbols(self) -> Dict[str, int]:
        """Find symbols not currently tracked."""
        all_perps = await self.fetch_all_perp_symbols()
        current_symbols = set(self._watchdog.get_all_symbols())
        
        new_symbols = {}
        for symbol, product_id in all_perps.items():
            if symbol not in current_symbols:
                new_symbols[symbol] = product_id
        
        if new_symbols:
            logger.info(f"Discovery: Found {len(new_symbols)} new symbols: {list(new_symbols.keys())}")
        else:
            logger.debug("Discovery: No new symbols found")
        
        return new_symbols
    
    async def run_discovery(self) -> List[str]:
        """
        Run full discovery cycle:
        1. Fetch all PERPs from API
        2. Find untracked symbols
        3. Add as CANDIDATEs
        4. Return list of new symbols
        """
        logger.info("Discovery: Starting discovery cycle")
        
        new_symbols = await self.find_new_symbols()
        
        if not new_symbols:
            logger.info("Discovery: No new symbols to add")
            return []
        
        # Add new symbols as CANDIDATEs
        for symbol, product_id in new_symbols.items():
            self._watchdog.add_symbol(symbol, product_id, SymbolState.CANDIDATE)
            logger.info(f"Discovery: Added {symbol} (id={product_id}) as CANDIDATE")
        
        return list(new_symbols.keys())
    
    async def test_candidates(self, candidates: Optional[List[str]] = None) -> Dict[str, bool]:
        """
        Test candidate symbols by subscribing and monitoring updates.
        Returns dict of symbol -> is_active.
        
        Note: This requires an active WebSocket connection to be testing.
        The actual testing is done by the WebSocket handler marking symbols as TESTING.
        """
        if candidates is None:
            candidates = self._watchdog.get_symbols_by_state(SymbolState.CANDIDATE)
        
        if not candidates:
            return {}
        
        logger.info(f"Discovery: Testing {len(candidates)} candidate symbols for {self.test_duration}s")
        
        # Mark candidates as TESTING
        for symbol in candidates:
            self._watchdog.apply_transition(symbol, SymbolState.TESTING)
        
        # Wait for test duration
        await asyncio.sleep(self.test_duration)
        
        # Check results
        results = {}
        for symbol in candidates:
            info = self._watchdog.get_symbol(symbol)
            if info:
                # Symbol received updates during testing
                if info.update_count > 0 or info.last_update:
                    results[symbol] = True
                    logger.info(f"Discovery: {symbol} is ACTIVE (received updates)")
                else:
                    results[symbol] = False
                    logger.info(f"Discovery: {symbol} is INACTIVE (no updates)")
            else:
                results[symbol] = False
        
        return results
    
    def promote_active_candidates(self, test_results: Dict[str, bool]) -> List[str]:
        """Promote active candidates to ACTIVE, inactive ones to INACTIVE."""
        promoted = []
        
        for symbol, is_active in test_results.items():
            info = self._watchdog.get_symbol(symbol)
            if not info or info.state != SymbolState.TESTING:
                continue
            
            if is_active:
                self._watchdog.apply_transition(symbol, SymbolState.ACTIVE)
                promoted.append(symbol)
                logger.info(f"Discovery: Promoted {symbol} to ACTIVE")
            else:
                self._watchdog.apply_transition(symbol, SymbolState.INACTIVE)
                logger.info(f"Discovery: Marked {symbol} as INACTIVE")
        
        return promoted
    
    async def run_full_cycle(self) -> dict:
        """Run complete discovery and testing cycle."""
        logger.info("Discovery: Running full discovery cycle")
        
        # Step 1: Discover new symbols
        new_symbols = await self.run_discovery()
        
        if not new_symbols:
            return {
                "discovered": 0,
                "tested": 0,
                "promoted": 0,
                "new_symbols": [],
            }
        
        # Step 2: Wait a moment for WebSocket subscriptions to be sent
        logger.info("Discovery: Waiting for WebSocket subscriptions...")
        await asyncio.sleep(5)
        
        # Step 3: Test candidates
        test_results = await self.test_candidates(new_symbols)
        
        # Step 4: Promote active ones
        promoted = self.promote_active_candidates(test_results)
        
        # Summary
        active_count = sum(1 for v in test_results.values() if v)
        inactive_count = sum(1 for v in test_results.values() if not v)
        
        summary = {
            "discovered": len(new_symbols),
            "tested": len(test_results),
            "promoted": len(promoted),
            "inactive": inactive_count,
            "new_symbols": new_symbols,
            "promoted_symbols": promoted,
        }
        
        logger.info(f"Discovery cycle complete: {summary}")
        return summary
    
    async def start_scheduled_discovery(self, interval_hours: int = 24) -> None:
        """Start background task for scheduled discovery."""
        logger.info(f"Discovery: Starting scheduled discovery (interval: {interval_hours}h)")
        
        while True:
            try:
                await self.run_full_cycle()
            except Exception as e:
                logger.error(f"Discovery cycle error: {e}")
            
            # Wait for next cycle
            await asyncio.sleep(interval_hours * 3600)
    
    def get_product_id(self, symbol: str) -> Optional[int]:
        """Get product ID for a symbol (from cache or API)."""
        # Check cache first
        if symbol in self._discovered_product_ids:
            return self._discovered_product_ids[symbol]
        
        # Check watchdog
        info = self._watchdog.get_symbol(symbol)
        if info:
            return info.product_id
        
        return None


# Global discovery instance
_discovery: Optional[NadoDiscovery] = None


def get_discovery(
    api_url: Optional[str] = None,
    test_duration_seconds: Optional[int] = None,
    watchdog: Optional[NadoWatchdog] = None,
) -> NadoDiscovery:
    """Get or create global discovery instance."""
    global _discovery
    if _discovery is None:
        kwargs = {}
        if api_url:
            kwargs["api_url"] = api_url
        if test_duration_seconds:
            kwargs["test_duration_seconds"] = test_duration_seconds
        if watchdog:
            kwargs["watchdog"] = watchdog
        _discovery = NadoDiscovery(**kwargs)
    return _discovery


def reset_discovery() -> None:
    """Reset global discovery instance."""
    global _discovery
    _discovery = None
