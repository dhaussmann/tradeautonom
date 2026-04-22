"""
Nado WebSocket - Dynamic WebSocket handler that adapts to symbol state changes.
Manages single shared connection with subscriptions based on watchdog state.
"""

import asyncio
import json
import logging
import ssl
from typing import Dict, List, Optional, Set

import websockets
from websockets.extensions.permessage_deflate import ClientPerMessageDeflateFactory

from nado_watchdog import NadoWatchdog, SymbolState, get_watchdog

logger = logging.getLogger(__name__)

# SSL context for WebSocket connections
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

# Nado decimal format
_X18_FLOAT = 1e18


class NadoWebSocketManager:
    """Manages dynamic WebSocket connection for Nado."""
    
    def __init__(
        self,
        endpoint: str = "wss://gateway.prod.nado.xyz/v1/subscribe",
        subscription_delay_ms: float = 50.0,
        watchdog: Optional[NadoWatchdog] = None,
    ):
        self.endpoint = endpoint
        self.subscription_delay = subscription_delay_ms / 1000.0  # Convert to seconds
        self._watchdog = watchdog or get_watchdog()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._subscribed_symbols: Set[str] = set()
        self._running = False
        self._message_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        
        # Message handler callback
        self._on_message_callback: Optional[callable] = None
        
        logger.info(f"NadoWebSocketManager initialized with endpoint: {endpoint}")
    
    async def start(self) -> None:
        """Start the WebSocket manager."""
        if self._running:
            logger.warning("WebSocket manager already running")
            return
        
        self._running = True
        self._message_task = asyncio.create_task(
            self._connection_loop(),
            name="nado-ws-connection"
        )
        self._health_task = asyncio.create_task(
            self._health_check_loop(),
            name="nado-ws-health"
        )
        
        logger.info("Nado WebSocket manager started")
    
    async def stop(self) -> None:
        """Stop the WebSocket manager."""
        self._running = False
        
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        
        if self._message_task:
            self._message_task.cancel()
            try:
                await self._message_task
            except asyncio.CancelledError:
                pass
            self._message_task = None
        
        if self._health_task:
            self._health_task.cancel()
            try:
                await self._health_task
            except asyncio.CancelledError:
                pass
            self._health_task = None
        
        logger.info("Nado WebSocket manager stopped")
    
    async def _connection_loop(self) -> None:
        """Main connection loop with reconnection logic."""
        reconnect_delay = 1.0
        
        while self._running:
            try:
                await self._connect_and_run()
                # If we get here without exception, connection was closed cleanly
                reconnect_delay = 1.0  # Reset on clean close
                
            except websockets.ConnectionClosed as e:
                logger.warning(f"Nado WebSocket closed: {e}")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)
                
            except Exception as e:
                logger.error(f"Nado WebSocket error: {e}")
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30.0)
    
    async def _connect_and_run(self) -> None:
        """Connect and handle messages until connection closes."""
        extensions = [ClientPerMessageDeflateFactory()]
        
        logger.info(f"Connecting to {self.endpoint}")
        async with websockets.connect(
            self.endpoint,
            ssl=_SSL_CTX,
            extensions=extensions,
            close_timeout=5,
        ) as ws:
            self._ws = ws
            logger.info("Nado WebSocket connected")
            
            # Subscribe to all active symbols
            await self._subscribe_to_active_symbols()
            
            # Message loop
            async for raw in ws:
                if not self._running:
                    break
                await self._handle_message(raw)
    
    async def _subscribe_to_active_symbols(self) -> None:
        """Subscribe to all symbols that should be active."""
        symbols = self._watchdog.get_subscribable_symbols()
        
        logger.info(f"Subscribing to {len(symbols)} symbols")
        
        for symbol in symbols:
            info = self._watchdog.get_symbol(symbol)
            if not info:
                continue
            
            sub_msg = {
                "method": "subscribe",
                "stream": {"type": "book_depth", "product_id": info.product_id},
                "id": info.product_id,
            }
            
            try:
                await self._ws.send(json.dumps(sub_msg))
                self._subscribed_symbols.add(symbol)
                await asyncio.sleep(self.subscription_delay)
            except Exception as e:
                logger.error(f"Failed to subscribe to {symbol}: {e}")
        
        logger.info(f"Subscribed to {len(self._subscribed_symbols)} symbols")
    
    async def _handle_message(self, raw: str) -> None:
        """Handle incoming WebSocket message."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        
        # Skip non-book messages
        if "bids" not in msg and "asks" not in msg:
            return
        
        # Extract product ID
        product_id = msg.get("product_id")
        if not product_id:
            return
        
        # Get symbol from product ID
        symbol = self._watchdog.get_symbol_by_product_id(product_id)
        if not symbol:
            logger.debug(f"Unknown product_id: {product_id}")
            return
        
        # Record update in watchdog
        self._watchdog.record_update(symbol)
        
        # Call message handler if registered
        if self._on_message_callback:
            try:
                self._on_message_callback(symbol, msg)
            except Exception as e:
                logger.error(f"Message handler error: {e}")
    
    async def _health_check_loop(self) -> None:
        """Background loop for health checks and state transitions."""
        while self._running:
            try:
                await asyncio.sleep(10)  # Check every 10 seconds
                
                # Get transitions needed
                transitions = self._watchdog.check_health()
                
                for symbol, old_state, new_state in transitions:
                    self._watchdog.apply_transition(symbol, new_state)
                    
                    # Log significant transitions
                    if new_state == SymbolState.RETRYING:
                        info = self._watchdog.get_symbol(symbol)
                        if info:
                            delay = self._calculate_retry_delay(info.retry_attempt)
                            logger.info(f"Symbol {symbol} retry {info.retry_attempt}/10, next check in {delay}s")
                    
                    elif new_state == SymbolState.INACTIVE:
                        logger.warning(f"Symbol {symbol} marked INACTIVE after all retries")
                        # Unsubscribe from inactive symbols
                        await self._unsubscribe_symbol(symbol)
                
                # Check for new candidates to subscribe
                await self._subscribe_new_candidates()
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")
    
    def _calculate_retry_delay(self, attempt: int) -> int:
        """Calculate exponential backoff delay for retry."""
        delay = min(self._watchdog.retry_base_delay * (2 ** (attempt - 1)), self._watchdog.retry_max_delay)
        return int(delay)
    
    async def _unsubscribe_symbol(self, symbol: str) -> None:
        """Unsubscribe from a symbol."""
        if symbol in self._subscribed_symbols:
            self._subscribed_symbols.discard(symbol)
            # Note: Nado doesn't support explicit unsubscribe, 
            # we just stop tracking updates
            logger.info(f"Unsubscribed from {symbol}")
    
    async def _subscribe_new_candidates(self) -> None:
        """Subscribe to new candidate symbols."""
        candidates = self._watchdog.get_symbols_by_state(SymbolState.CANDIDATE)
        
        for symbol in candidates:
            if symbol in self._subscribed_symbols:
                continue
            
            info = self._watchdog.get_symbol(symbol)
            if not info:
                continue
            
            if not self._ws:
                continue
            
            sub_msg = {
                "method": "subscribe",
                "stream": {"type": "book_depth", "product_id": info.product_id},
                "id": info.product_id,
            }
            
            try:
                await self._ws.send(json.dumps(sub_msg))
                self._subscribed_symbols.add(symbol)
                logger.info(f"Subscribed to new candidate: {symbol}")
                await asyncio.sleep(self.subscription_delay)
            except Exception as e:
                logger.error(f"Failed to subscribe to candidate {symbol}: {e}")
    
    def register_message_handler(self, callback: callable) -> None:
        """Register a callback for book messages."""
        self._on_message_callback = callback
    
    def get_connection_stats(self) -> dict:
        """Get current connection statistics."""
        connected = False
        if self._ws is not None:
            try:
                # Check connection state (compatible with different websockets versions)
                if hasattr(self._ws, 'open'):
                    connected = self._ws.open
                elif hasattr(self._ws, 'state'):
                    from websockets.protocol import State
                    connected = self._ws.state == State.OPEN
                else:
                    # Fallback - try to check if close_code is None
                    connected = getattr(self._ws, 'close_code', None) is None
            except Exception:
                connected = False
        
        return {
            "connected": connected,
            "subscribed_symbols": len(self._subscribed_symbols),
            "endpoint": self.endpoint,
        }


def handle_nado_book_message(symbol: str, msg: dict, books_cache: dict, watchdog=None) -> None:
    """
    Process a Nado book message and update the order book cache.
    This is the message handler callback that integrates with OMS BookSnapshot.
    """
    import time
    
    key = ("nado", symbol)
    if key not in books_cache:
        return
    
    snap = books_cache[key]
    
    # Process bids (BookSnapshot has .bids list)
    bids_raw = msg.get("bids", [])
    if bids_raw:
        _apply_nado_deltas_to_snapshot(snap.bids, bids_raw, reverse=True)
    
    # Process asks (BookSnapshot has .asks list)
    asks_raw = msg.get("asks", [])
    if asks_raw:
        _apply_nado_deltas_to_snapshot(snap.asks, asks_raw, reverse=False)
    
    # Update metadata (BookSnapshot attributes)
    snap.timestamp_ms = time.time() * 1000
    snap.update_count += 1
    snap.has_data = True
    snap.connected = True
    
    # Record update in watchdog if provided
    if watchdog:
        watchdog.record_update(symbol)
    
    # Notify subscribers (if function available)
    try:
        from monitor_service import _notify_subscribers
        _notify_subscribers(key)
    except ImportError:
        pass


def _apply_nado_deltas(book_levels: List, updates: List, reverse: bool) -> None:
    """Apply Nado incremental deltas to order book levels."""
    price_map = {level[0]: level for level in book_levels}
    
    for entry in updates:
        if len(entry) < 2:
            continue
        
        price = float(entry[0]) / _X18_FLOAT
        size = float(entry[1]) / _X18_FLOAT
        
        if size <= 0:
            price_map.pop(price, None)
        else:
            price_map[price] = [price, size]
    
    book_levels.clear()
    book_levels.extend(sorted(price_map.values(), key=lambda x: -x[0] if reverse else x[0]))


def _apply_nado_deltas_to_snapshot(book_levels: list, updates: list, reverse: bool) -> None:
    """
    Apply Nado incremental deltas to BookSnapshot levels.
    Same logic as _apply_nado_deltas but works with list references.
    """
    # Build price map from current levels
    price_map = {}
    for level in book_levels:
        if len(level) >= 2:
            price_map[level[0]] = level
    
    # Apply updates
    for entry in updates:
        if len(entry) < 2:
            continue
        
        # Nado uses x18 format (divide by 1e18)
        price = float(entry[0]) / _X18_FLOAT
        size = float(entry[1]) / _X18_FLOAT
        
        if size <= 0:
            # Remove level
            price_map.pop(price, None)
        else:
            # Update or add level
            price_map[price] = [price, size]
    
    # Clear and rebuild sorted list
    book_levels.clear()
    sorted_levels = sorted(price_map.values(), key=lambda x: -x[0] if reverse else x[0])
    book_levels.extend(sorted_levels)


# Global WebSocket manager instance
_ws_manager: Optional[NadoWebSocketManager] = None


def get_websocket_manager(
    endpoint: Optional[str] = None,
    subscription_delay_ms: Optional[float] = None,
    watchdog: Optional[NadoWatchdog] = None,
) -> NadoWebSocketManager:
    """Get or create global WebSocket manager instance."""
    global _ws_manager
    if _ws_manager is None:
        kwargs = {}
        if endpoint:
            kwargs["endpoint"] = endpoint
        if subscription_delay_ms:
            kwargs["subscription_delay_ms"] = subscription_delay_ms
        if watchdog:
            kwargs["watchdog"] = watchdog
        _ws_manager = NadoWebSocketManager(**kwargs)
    return _ws_manager


def reset_websocket_manager() -> None:
    """Reset global WebSocket manager instance."""
    global _ws_manager
    _ws_manager = None
