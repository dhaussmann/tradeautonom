"""Shared in-memory data cache for all bots in a container.

Provides thread-safe access to position, balance, fill, and orderbook data.
All bots read from this shared cache instead of creating their own connections.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
from decimal import Decimal


@dataclass
class PositionSnapshot:
    """Cached position state for one exchange + symbol."""
    size: float = 0.0          # Absolute position size
    side: str = ""             # "long", "short", or ""
    entry_price: float = 0.0
    unrealized_pnl: float = 0.0
    timestamp_ms: float = 0.0
    connected: bool = False
    update_count: int = 0


@dataclass
class BalanceSnapshot:
    """Cached balance state for one exchange."""
    asset: str = ""
    total: float = 0.0
    available: float = 0.0
    timestamp_ms: float = 0.0


@dataclass
class FillSnapshot:
    """Cached fill information."""
    order_id: str = ""
    filled_qty: float = 0.0
    remaining_qty: float = 0.0
    price: float = 0.0
    is_taker: bool = True
    fee: float = 0.0
    symbol: str = ""
    timestamp_ms: float = 0.0


@dataclass
class OrderbookSnapshot:
    """Cached orderbook state."""
    bids: list = field(default_factory=list)
    asks: list = field(default_factory=list)
    timestamp_ms: float = 0.0
    is_synced: bool = False
    connected: bool = False
    update_count: int = 0


class SharedDataCache:
    """Thread-safe in-memory cache shared by all bots in a container.
    
    Key design:
    - All data stored in memory (no persistence)
    - Lock-free reads using atomic reference updates
    - Async locks for writes
    """
    
    def __init__(self) -> None:
        # Data caches keyed by (exchange, symbol) or exchange
        self._positions: Dict[Tuple[str, str], PositionSnapshot] = {}
        self._balances: Dict[str, BalanceSnapshot] = {}
        self._fills: Dict[str, FillSnapshot] = {}  # keyed by order_id
        self._orderbooks: Dict[Tuple[str, str], OrderbookSnapshot] = {}
        
        # Locks for thread-safe writes
        self._pos_locks: Dict[Tuple[str, str], asyncio.Lock] = {}
        self._bal_locks: Dict[str, asyncio.Lock] = {}
        self._fill_locks: Dict[str, asyncio.Lock] = {}
        self._ob_locks: Dict[Tuple[str, str], asyncio.Lock] = {}
        
        # Global lock for creating new locks
        self._lock_creation_lock = asyncio.Lock()
    
    def _get_or_create_lock(self, lock_dict: dict, key: tuple | str) -> asyncio.Lock:
        """Get existing lock or create new one atomically."""
        if key not in lock_dict:
            lock_dict[key] = asyncio.Lock()
        return lock_dict[key]
    
    # ── Position Cache ─────────────────────────────────────────────
    
    async def update_position(
        self,
        exchange: str,
        symbol: str,
        size: float,
        side: str = "",
        entry_price: float = 0.0,
        unrealized_pnl: float = 0.0,
        connected: bool = True
    ) -> None:
        """Update position in cache (thread-safe write)."""
        key = (exchange, symbol)
        async with self._get_or_create_lock(self._pos_locks, key):
            existing = self._positions.get(key, PositionSnapshot())
            self._positions[key] = PositionSnapshot(
                size=size,
                side=side or existing.side,
                entry_price=entry_price or existing.entry_price,
                unrealized_pnl=unrealized_pnl,
                timestamp_ms=time.time() * 1000,
                connected=connected,
                update_count=existing.update_count + 1
            )
    
    def get_position(self, exchange: str, symbol: str) -> PositionSnapshot:
        """Get position from cache (lock-free read)."""
        return self._positions.get((exchange, symbol), PositionSnapshot())
    
    def get_all_positions(self) -> Dict[Tuple[str, str], PositionSnapshot]:
        """Get all cached positions."""
        return dict(self._positions)
    
    def is_position_fresh(self, exchange: str, symbol: str, max_age_ms: float = 5000) -> bool:
        """Check if position data is fresh."""
        pos = self._positions.get((exchange, symbol))
        if not pos:
            return False
        age_ms = (time.time() * 1000) - pos.timestamp_ms
        return age_ms < max_age_ms and pos.connected
    
    # ── Balance Cache ──────────────────────────────────────────────
    
    async def update_balance(
        self,
        exchange: str,
        asset: str,
        total: float,
        available: float
    ) -> None:
        """Update balance in cache."""
        async with self._get_or_create_lock(self._bal_locks, exchange):
            self._balances[exchange] = BalanceSnapshot(
                asset=asset,
                total=total,
                available=available,
                timestamp_ms=time.time() * 1000
            )
    
    def get_balance(self, exchange: str) -> BalanceSnapshot:
        """Get balance from cache."""
        return self._balances.get(exchange, BalanceSnapshot())
    
    # ── Fill Cache ─────────────────────────────────────────────────
    
    async def update_fill(
        self,
        order_id: str,
        filled_qty: float,
        remaining_qty: float = 0.0,
        price: float = 0.0,
        is_taker: bool = True,
        fee: float = 0.0,
        symbol: str = ""
    ) -> None:
        """Update fill information in cache."""
        async with self._get_or_create_lock(self._fill_locks, order_id):
            self._fills[order_id] = FillSnapshot(
                order_id=order_id,
                filled_qty=filled_qty,
                remaining_qty=remaining_qty,
                price=price,
                is_taker=is_taker,
                fee=fee,
                symbol=symbol,
                timestamp_ms=time.time() * 1000
            )
    
    def get_fill(self, order_id: str) -> FillSnapshot:
        """Get fill information from cache."""
        return self._fills.get(order_id, FillSnapshot())
    
    # ── Orderbook Cache ────────────────────────────────────────────
    
    async def update_orderbook(
        self,
        exchange: str,
        symbol: str,
        bids: list,
        asks: list,
        timestamp_ms: Optional[float] = None
    ) -> None:
        """Update orderbook in cache."""
        key = (exchange, symbol)
        async with self._get_or_create_lock(self._ob_locks, key):
            existing = self._orderbooks.get(key, OrderbookSnapshot())
            self._orderbooks[key] = OrderbookSnapshot(
                bids=bids,
                asks=asks,
                timestamp_ms=timestamp_ms or time.time() * 1000,
                is_synced=True,
                connected=True,
                update_count=existing.update_count + 1
            )
    
    def get_orderbook(self, exchange: str, symbol: str) -> OrderbookSnapshot:
        """Get orderbook from cache."""
        return self._orderbooks.get((exchange, symbol), OrderbookSnapshot())
    
    def is_orderbook_fresh(self, exchange: str, symbol: str, max_age_ms: float = 5000) -> bool:
        """Check if orderbook is fresh."""
        ob = self._orderbooks.get((exchange, symbol))
        if not ob:
            return False
        age_ms = (time.time() * 1000) - ob.timestamp_ms
        return age_ms < max_age_ms and ob.is_synced and ob.connected
    
    # ── Utility Methods ────────────────────────────────────────────
    
    def get_cache_stats(self) -> dict:
        """Get cache statistics."""
        return {
            "positions_cached": len(self._positions),
            "balances_cached": len(self._balances),
            "fills_cached": len(self._fills),
            "orderbooks_cached": len(self._orderbooks),
        }
    
    def clear(self) -> None:
        """Clear all cached data."""
        self._positions.clear()
        self._balances.clear()
        self._fills.clear()
        self._orderbooks.clear()
