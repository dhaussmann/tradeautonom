"""
Nado Persistence - Handles saving and loading watchdog state to disk.
Only persists when state changes to minimize I/O.
"""

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from nado_watchdog import NadoWatchdog, get_watchdog

logger = logging.getLogger(__name__)


class NadoPersistence:
    """Manages persistence of Nado watchdog state."""
    
    def __init__(self, state_file_path: str = "/app/data/nado_watchdog_state.json"):
        self.state_file = Path(state_file_path)
        self._watchdog: Optional[NadoWatchdog] = None
        self._persist_task: Optional[asyncio.Task] = None
        self._running = False
        
        # Ensure directory exists
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"NadoPersistence initialized with state file: {self.state_file}")
    
    def initialize(self, watchdog: NadoWatchdog) -> None:
        """Initialize with watchdog instance and register callback."""
        self._watchdog = watchdog
        
        # Register for state change notifications
        watchdog.register_state_change_callback(self._on_state_change)
        
        logger.info("Persistence registered with watchdog")
    
    async def load_state(self) -> bool:
        """Load state from disk. Returns True if successful."""
        if not self.state_file.exists():
            logger.info("No persisted state file found")
            return False
        
        try:
            with open(self.state_file, 'r') as f:
                data = json.load(f)
            
            if self._watchdog:
                self._watchdog.from_dict(data)
            
            logger.info(f"Loaded state from {self.state_file}")
            return True
            
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse state file: {e}")
            return False
        except Exception as e:
            logger.error(f"Failed to load state: {e}")
            return False
    
    async def save_state(self, force: bool = False) -> bool:
        """Save state to disk if changed (or if forced)."""
        if not self._watchdog:
            logger.warning("Cannot save state: watchdog not initialized")
            return False
        
        # Check if we need to save
        if not force and not self._watchdog.should_persist():
            return True  # Nothing to save
        
        try:
            data = self._watchdog.to_dict()
            
            # Write to temp file first, then rename for atomicity
            temp_file = self.state_file.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            # Atomic rename
            temp_file.replace(self.state_file)
            
            self._watchdog.mark_persisted()
            logger.debug(f"State persisted to {self.state_file}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            return False
    
    async def start_auto_persist(self, interval_seconds: float = 60.0) -> None:
        """Start background task to auto-persist state periodically."""
        if self._running:
            logger.warning("Auto-persist already running")
            return
        
        self._running = True
        self._persist_task = asyncio.create_task(
            self._auto_persist_loop(interval_seconds),
            name="nado-persist"
        )
        logger.info(f"Auto-persist started (interval: {interval_seconds}s)")
    
    async def stop_auto_persist(self) -> None:
        """Stop auto-persist background task."""
        self._running = False
        if self._persist_task:
            self._persist_task.cancel()
            try:
                await self._persist_task
            except asyncio.CancelledError:
                pass
            self._persist_task = None
        
        # Final save
        await self.save_state(force=True)
        logger.info("Auto-persist stopped")
    
    async def _auto_persist_loop(self, interval_seconds: float) -> None:
        """Background loop for auto-persistence."""
        while self._running:
            try:
                await asyncio.sleep(interval_seconds)
                
                if self._watchdog and self._watchdog.should_persist():
                    await self.save_state()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Auto-persist error: {e}")
    
    def _on_state_change(self, symbol: str, old_state, new_state) -> None:
        """Callback for state changes - triggers immediate persist on significant changes."""
        # Persist immediately on significant state transitions
        significant_transitions = {
            ("suspect", "active"),      # Recovery
            ("retrying", "active"),     # Recovery
            ("retrying", "inactive"),   # Became inactive
            ("inactive", "active"),     # Reactivation
            ("candidate", "active"),    # New symbol activated
        }
        
        transition = (old_state.value, new_state.value)
        if transition in significant_transitions:
            # Schedule immediate save
            asyncio.create_task(self._immediate_save())
    
    async def _immediate_save(self) -> None:
        """Perform immediate save (debounced)."""
        # Wait a moment for any rapid successive changes
        await asyncio.sleep(1.0)
        await self.save_state()
    
    def get_state_file_info(self) -> dict:
        """Get information about the state file."""
        if not self.state_file.exists():
            return {"exists": False}
        
        stat = self.state_file.stat()
        return {
            "exists": True,
            "path": str(self.state_file),
            "size_bytes": stat.st_size,
            "modified": datetime.fromtimestamp(stat.st_mtime).isoformat(),
        }


# Global persistence instance
_persistence: Optional[NadoPersistence] = None


def get_persistence(state_file_path: Optional[str] = None) -> NadoPersistence:
    """Get or create global persistence instance."""
    global _persistence
    if _persistence is None:
        _persistence = NadoPersistence(state_file_path) if state_file_path else NadoPersistence()
    return _persistence


def reset_persistence() -> None:
    """Reset global persistence instance."""
    global _persistence
    _persistence = None
