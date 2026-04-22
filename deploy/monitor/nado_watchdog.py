"""
Nado Watchdog - Core state management for dynamic symbol monitoring.
Tracks symbol health, manages state transitions, and coordinates retries.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Set
from collections import defaultdict

logger = logging.getLogger(__name__)


class SymbolState(Enum):
    """Possible states for a Nado symbol."""
    ACTIVE = "active"           # Receiving regular updates
    SUSPECT = "suspect"         # No updates for threshold period
    RETRYING = "retrying"       # In retry loop
    INACTIVE = "inactive"       # Failed all retries
    CANDIDATE = "candidate"     # New symbol from discovery
    TESTING = "testing"         # Currently being tested


@dataclass
class SymbolInfo:
    """Information about a tracked Nado symbol."""
    symbol: str
    product_id: int
    state: SymbolState = SymbolState.ACTIVE
    last_update: Optional[datetime] = None
    update_count: int = 0
    retry_attempt: int = 0
    retry_next_at: Optional[datetime] = None
    inactive_since: Optional[datetime] = None
    last_reactivation_check: Optional[datetime] = None
    added_at: datetime = field(default_factory=datetime.utcnow)
    
    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "symbol": self.symbol,
            "product_id": self.product_id,
            "state": self.state.value,
            "last_update": self.last_update.isoformat() if self.last_update else None,
            "update_count": self.update_count,
            "retry_attempt": self.retry_attempt,
            "retry_next_at": self.retry_next_at.isoformat() if self.retry_next_at else None,
            "inactive_since": self.inactive_since.isoformat() if self.inactive_since else None,
            "last_reactivation_check": self.last_reactivation_check.isoformat() if self.last_reactivation_check else None,
            "added_at": self.added_at.isoformat(),
        }
    
    @classmethod
    def from_dict(cls, data: dict) -> "SymbolInfo":
        """Create from dictionary."""
        info = cls(
            symbol=data["symbol"],
            product_id=data["product_id"],
            state=SymbolState(data["state"]),
            update_count=data.get("update_count", 0),
            retry_attempt=data.get("retry_attempt", 0),
        )
        if data.get("last_update"):
            info.last_update = datetime.fromisoformat(data["last_update"])
        if data.get("retry_next_at"):
            info.retry_next_at = datetime.fromisoformat(data["retry_next_at"])
        if data.get("inactive_since"):
            info.inactive_since = datetime.fromisoformat(data["inactive_since"])
        if data.get("last_reactivation_check"):
            info.last_reactivation_check = datetime.fromisoformat(data["last_reactivation_check"])
        if data.get("added_at"):
            info.added_at = datetime.fromisoformat(data["added_at"])
        return info


class NadoWatchdog:
    """Manages dynamic symbol monitoring for Nado exchange."""
    
    def __init__(
        self,
        suspect_threshold_seconds: int = 120,
        retry_max_attempts: int = 10,
        retry_base_delay_seconds: int = 10,
        retry_max_delay_seconds: int = 7200,
        reactivation_check_interval_hours: int = 24,
    ):
        self._symbols: Dict[str, SymbolInfo] = {}
        self._product_to_symbol: Dict[int, str] = {}
        
        # Configuration
        self.suspect_threshold = timedelta(seconds=suspect_threshold_seconds)
        self.retry_max_attempts = retry_max_attempts
        self.retry_base_delay = retry_base_delay_seconds
        self.retry_max_delay = retry_max_delay_seconds
        self.reactivation_interval = timedelta(hours=reactivation_check_interval_hours)
        
        # Statistics
        self._state_changes: List[dict] = []
        self._last_state_save: Optional[datetime] = None
        
        # Callbacks for state changes
        self._on_state_change_callbacks: List[callable] = []
        
        logger.info("NadoWatchdog initialized")
    
    def add_symbol(self, symbol: str, product_id: int, state: SymbolState = SymbolState.ACTIVE) -> SymbolInfo:
        """Add a new symbol to tracking."""
        if symbol in self._symbols:
            logger.warning(f"Symbol {symbol} already tracked, skipping")
            return self._symbols[symbol]
        
        info = SymbolInfo(
            symbol=symbol,
            product_id=product_id,
            state=state,
            last_update=datetime.utcnow() if state == SymbolState.ACTIVE else None,
        )
        self._symbols[symbol] = info
        self._product_to_symbol[product_id] = symbol
        
        logger.info(f"Added symbol {symbol} (id={product_id}) in state {state.value}")
        return info
    
    def get_symbol(self, symbol: str) -> Optional[SymbolInfo]:
        """Get symbol info by name."""
        return self._symbols.get(symbol)
    
    def get_symbol_by_product_id(self, product_id: int) -> Optional[str]:
        """Get symbol name by product ID."""
        return self._product_to_symbol.get(product_id)
    
    def record_update(self, symbol: str) -> None:
        """Record an update received for a symbol."""
        info = self._symbols.get(symbol)
        if not info:
            logger.warning(f"Update for untracked symbol: {symbol}")
            return
        
        now = datetime.utcnow()
        old_state = info.state
        info.last_update = now
        info.update_count += 1
        
        # If was in retry/inactive state, promote back to active
        if info.state in (SymbolState.SUSPECT, SymbolState.RETRYING):
            self._transition_state(symbol, SymbolState.ACTIVE)
            logger.info(f"Symbol {symbol} recovered on retry attempt {info.retry_attempt}, marking ACTIVE")
            info.retry_attempt = 0
            info.retry_next_at = None
        elif info.state == SymbolState.INACTIVE:
            self._transition_state(symbol, SymbolState.ACTIVE)
            logger.info(f"Symbol {symbol} reactivated after being inactive, marking ACTIVE")
            info.inactive_since = None
            info.retry_attempt = 0
        elif info.state == SymbolState.TESTING:
            # Will be promoted by discovery process
            pass
        
        # Check if we need to save state (only on significant state changes)
        if old_state != info.state:
            self._notify_state_change(symbol, old_state, info.state)
    
    def check_health(self) -> List[tuple[str, SymbolState, Optional[SymbolState]]]:
        """
        Check health of all symbols and return state transitions needed.
        Returns list of (symbol, current_state, new_state) tuples.
        """
        now = datetime.utcnow()
        transitions = []
        
        for symbol, info in self._symbols.items():
            # Check ACTIVE symbols that haven't updated recently
            if info.state == SymbolState.ACTIVE:
                if info.last_update and (now - info.last_update) > self.suspect_threshold:
                    transitions.append((symbol, SymbolState.ACTIVE, SymbolState.SUSPECT))
            
            # Check SUSPECT symbols - start retry loop
            elif info.state == SymbolState.SUSPECT:
                transitions.append((symbol, SymbolState.SUSPECT, SymbolState.RETRYING))
            
            # Check RETRYING symbols
            elif info.state == SymbolState.RETRYING:
                if info.retry_next_at and now >= info.retry_next_at:
                    if info.retry_attempt >= self.retry_max_attempts:
                        transitions.append((symbol, SymbolState.RETRYING, SymbolState.INACTIVE))
                    # Otherwise, keep retrying (retry_next_at will be updated)
            
            # Check INACTIVE symbols for reactivation
            elif info.state == SymbolState.INACTIVE:
                if info.inactive_since and (now - info.inactive_since) > self.reactivation_interval:
                    if not info.last_reactivation_check or (now - info.last_reactivation_check) > self.reactivation_interval:
                        transitions.append((symbol, SymbolState.INACTIVE, SymbolState.CANDIDATE))
            
            # Check CANDIDATE symbols that haven't been tested
            elif info.state == SymbolState.CANDIDATE:
                # Discovery process will handle testing
                pass
        
        return transitions
    
    def apply_transition(self, symbol: str, new_state: SymbolState) -> None:
        """Apply a state transition."""
        info = self._symbols.get(symbol)
        if not info:
            logger.warning(f"Cannot transition untracked symbol: {symbol}")
            return
        
        old_state = info.state
        now = datetime.utcnow()
        
        if old_state == new_state:
            return
        
        # Handle specific transition logic
        if new_state == SymbolState.RETRYING:
            info.retry_attempt += 1
            delay = min(
                self.retry_base_delay * (2 ** (info.retry_attempt - 1)),
                self.retry_max_delay
            )
            info.retry_next_at = now + timedelta(seconds=delay)
            logger.info(f"Symbol {symbol} entering retry loop (attempt {info.retry_attempt}/{self.retry_max_attempts}, next retry in {delay}s)")
        
        elif new_state == SymbolState.INACTIVE:
            info.inactive_since = now
            info.retry_attempt = 0
            info.retry_next_at = None
            logger.warning(f"Symbol {symbol} marked INACTIVE after {self.retry_max_attempts} failed retries")
        
        elif new_state == SymbolState.CANDIDATE:
            info.last_reactivation_check = now
            logger.info(f"Symbol {symbol} promoted to CANDIDATE for reactivation testing")
        
        elif new_state == SymbolState.TESTING:
            logger.info(f"Symbol {symbol} now TESTING")
        
        self._transition_state(symbol, new_state)
    
    def _transition_state(self, symbol: str, new_state: SymbolState) -> None:
        """Internal state transition with logging and callbacks."""
        info = self._symbols.get(symbol)
        if not info:
            return
        
        old_state = info.state
        info.state = new_state
        
        # Record state change
        self._state_changes.append({
            "symbol": symbol,
            "from": old_state.value,
            "to": new_state.value,
            "at": datetime.utcnow().isoformat(),
        })
        
        logger.info(f"Symbol {symbol} state transition: {old_state.value} -> {new_state.value}")
        self._notify_state_change(symbol, old_state, new_state)
    
    def get_symbols_by_state(self, state: SymbolState) -> List[str]:
        """Get all symbols in a given state."""
        return [s for s, info in self._symbols.items() if info.state == state]
    
    def get_active_symbols(self) -> List[str]:
        """Get symbols that should be subscribed (ACTIVE + SUSPECT + RETRYING + TESTING)."""
        active_states = {
            SymbolState.ACTIVE,
            SymbolState.SUSPECT,
            SymbolState.RETRYING,
            SymbolState.TESTING,
        }
        return [s for s, info in self._symbols.items() if info.state in active_states]
    
    def get_subscribable_symbols(self) -> List[str]:
        """Get symbols that should have active WebSocket subscriptions."""
        # Include candidates for testing
        return self.get_active_symbols() + self.get_symbols_by_state(SymbolState.CANDIDATE)
    
    def get_all_symbols(self) -> List[str]:
        """Get all tracked symbols."""
        return list(self._symbols.keys())
    
    def get_statistics(self) -> dict:
        """Get current statistics."""
        stats = defaultdict(int)
        for info in self._symbols.values():
            stats[info.state.value] += 1
        
        return {
            "total": len(self._symbols),
            "by_state": dict(stats),
            "active": len(self.get_active_symbols()),
            "subscriptions_needed": len(self.get_subscribable_symbols()),
        }
    
    def to_dict(self) -> dict:
        """Serialize watchdog state to dictionary."""
        return {
            "version": "1.0",
            "last_saved": datetime.utcnow().isoformat(),
            "symbols": {s: info.to_dict() for s, info in self._symbols.items()},
            "statistics": self.get_statistics(),
            "recent_state_changes": self._state_changes[-100:],  # Keep last 100
        }
    
    def from_dict(self, data: dict) -> None:
        """Load watchdog state from dictionary."""
        self._symbols.clear()
        self._product_to_symbol.clear()
        
        for symbol, info_data in data.get("symbols", {}).items():
            try:
                info = SymbolInfo.from_dict(info_data)
                self._symbols[symbol] = info
                self._product_to_symbol[info.product_id] = symbol
            except Exception as e:
                logger.error(f"Failed to load symbol {symbol}: {e}")
        
        self._state_changes = data.get("recent_state_changes", [])
        logger.info(f"Loaded {len(self._symbols)} symbols from persisted state")
    
    def register_state_change_callback(self, callback: callable) -> None:
        """Register a callback for state changes."""
        self._on_state_change_callbacks.append(callback)
    
    def _notify_state_change(self, symbol: str, old_state: SymbolState, new_state: SymbolState) -> None:
        """Notify all registered callbacks of state change."""
        for callback in self._on_state_change_callbacks:
            try:
                callback(symbol, old_state, new_state)
            except Exception as e:
                logger.error(f"State change callback error: {e}")
    
    def should_persist(self) -> bool:
        """Check if state should be persisted (has changes since last save)."""
        if not self._last_state_save:
            return True
        
        # Check if any state changes since last save
        for change in self._state_changes:
            change_time = datetime.fromisoformat(change["at"])
            if change_time > self._last_state_save:
                return True
        
        return False
    
    def mark_persisted(self) -> None:
        """Mark state as persisted."""
        self._last_state_save = datetime.utcnow()


# Global watchdog instance
_watchdog: Optional[NadoWatchdog] = None


def get_watchdog() -> NadoWatchdog:
    """Get or create global watchdog instance."""
    global _watchdog
    if _watchdog is None:
        _watchdog = NadoWatchdog()
    return _watchdog


def reset_watchdog() -> None:
    """Reset global watchdog instance."""
    global _watchdog
    _watchdog = None
