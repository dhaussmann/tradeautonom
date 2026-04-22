"""Shared authenticated WebSocket manager for exchange connections.

Manages ONE WebSocket connection per exchange that is shared by all bots.
Supports dynamic symbol subscription (subscribe/unsubscribe as bots start/stop).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Dict, Optional, Set
from decimal import Decimal

import websockets

from app.shared_data_cache import SharedDataCache, PositionSnapshot

logger = logging.getLogger("tradeautonom.shared_auth_ws")


class SymbolSubscriptionTracker:
    """Tracks which bots are subscribed to which symbols."""
    
    def __init__(self):
        # symbol -> set of bot_ids
        self._subscribers: Dict[str, Set[str]] = {}
    
    def subscribe(self, bot_id: str, symbol: str) -> bool:
        """Subscribe bot to symbol. Returns True if this is the first subscriber."""
        is_first = symbol not in self._subscribers or not self._subscribers[symbol]
        if symbol not in self._subscribers:
            self._subscribers[symbol] = set()
        self._subscribers[symbol].add(bot_id)
        return is_first
    
    def unsubscribe(self, bot_id: str, symbol: str) -> bool:
        """Unsubscribe bot from symbol. Returns True if no more subscribers."""
        if symbol in self._subscribers:
            self._subscribers[symbol].discard(bot_id)
            if not self._subscribers[symbol]:
                del self._subscribers[symbol]
                return True
        return False
    
    def get_subscribed_symbols(self) -> Set[str]:
        """Get all currently subscribed symbols."""
        return set(self._subscribers.keys())
    
    def is_subscribed(self, symbol: str) -> bool:
        """Check if symbol has any subscribers."""
        return symbol in self._subscribers and len(self._subscribers[symbol]) > 0


class SharedAuthWebSocketManager:
    """Manages shared authenticated WebSocket connection for one exchange.
    
    - Maintains single WS connection
    - Dynamic symbol subscription (bots subscribe/unsubscribe)
    - Automatic REST fallback when WS disconnected
    - Stores data in SharedDataCache
    """
    
    def __init__(
        self,
        exchange: str,
        client: Any,
        data_cache: SharedDataCache,
        rest_fallback: bool = True
    ):
        self.exchange = exchange
        self.client = client
        self.data_cache = data_cache
        self.rest_fallback = rest_fallback
        
        # Symbol subscription tracking
        self._subscription_tracker = SymbolSubscriptionTracker()
        
        # WebSocket state
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._ws_task: Optional[asyncio.Task] = None
        self._running = False
        self._connected = False
        self._last_ws_message_time = 0.0
        
        # REST fallback state
        self._rest_poll_task: Optional[asyncio.Task] = None
        self._rest_poll_interval = 2.0  # seconds
        
        # Message handlers by type
        self._message_handlers: Dict[str, Callable] = {
            "POSITION": self._handle_position_message,
            "TRADE": self._handle_trade_message,
            "BALANCE": self._handle_balance_message,
        }
    
    # ── Public API ─────────────────────────────────────────────────
    
    async def start(self) -> None:
        """Start the shared WebSocket connection."""
        if self._running:
            return
        
        self._running = True
        logger.info("SharedAuthWS[%s]: Starting", self.exchange)
        
        # Start WebSocket connection
        self._ws_task = asyncio.create_task(
            self._run_websocket(),
            name=f"shared-ws-{self.exchange}"
        )
    
    async def stop(self) -> None:
        """Stop the shared WebSocket connection."""
        if not self._running:
            return
        
        self._running = False
        logger.info("SharedAuthWS[%s]: Stopping", self.exchange)
        
        # Cancel WebSocket task
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except asyncio.CancelledError:
                pass
        
        # Cancel REST poll task
        if self._rest_poll_task:
            self._rest_poll_task.cancel()
            try:
                await self._rest_poll_task
            except asyncio.CancelledError:
                pass
        
        # Close WebSocket
        if self._ws:
            await self._ws.close()
        
        self._connected = False
        logger.info("SharedAuthWS[%s]: Stopped", self.exchange)
    
    async def subscribe_symbol(self, bot_id: str, symbol: str) -> None:
        """Subscribe bot to symbol updates."""
        is_first = self._subscription_tracker.subscribe(bot_id, symbol)
        
        if is_first and self._connected and self._ws:
            # First subscriber - send subscribe message
            try:
                await self._send_subscribe_message(symbol)
                logger.info("SharedAuthWS[%s]: Subscribed to %s (bot=%s)", 
                          self.exchange, symbol, bot_id)
            except Exception as exc:
                logger.warning("SharedAuthWS[%s]: Failed to subscribe %s: %s",
                             self.exchange, symbol, exc)
        else:
            logger.debug("SharedAuthWS[%s]: Added subscriber for %s (bot=%s, existing=%d)",
                        self.exchange, symbol, bot_id,
                        len(self._subscription_tracker._subscribers.get(symbol, set())))
    
    async def unsubscribe_symbol(self, bot_id: str, symbol: str) -> None:
        """Unsubscribe bot from symbol updates."""
        is_last = self._subscription_tracker.unsubscribe(bot_id, symbol)
        
        if is_last and self._connected and self._ws:
            # Last subscriber - send unsubscribe message
            try:
                await self._send_unsubscribe_message(symbol)
                logger.info("SharedAuthWS[%s]: Unsubscribed from %s", 
                          self.exchange, symbol)
            except Exception as exc:
                logger.warning("SharedAuthWS[%s]: Failed to unsubscribe %s: %s",
                             self.exchange, symbol, exc)
    
    def get_subscribed_symbols(self) -> Set[str]:
        """Get all currently subscribed symbols."""
        return self._subscription_tracker.get_subscribed_symbols()
    
    def is_connected(self) -> bool:
        """Check if WebSocket is connected."""
        return self._connected
    
    # ── WebSocket Implementation ───────────────────────────────────
    
    async def _run_websocket(self) -> None:
        """Main WebSocket connection loop with auto-reconnect."""
        reconnect_delay = 1.0
        max_reconnect_delay = 30.0
        
        while self._running:
            try:
                await self._connect_and_run()
                # Successful run ended (likely _running=False)
                break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._connected = False
                logger.error("SharedAuthWS[%s]: Connection error: %s — retry in %.1fs",
                           self.exchange, exc, reconnect_delay)
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 1.5, max_reconnect_delay)
    
    async def _connect_and_run(self) -> None:
        """Connect to WebSocket and handle messages."""
        ws_url = self._get_websocket_url()
        headers = self._get_auth_headers()
        
        logger.info("SharedAuthWS[%s]: Connecting to %s", self.exchange, ws_url)
        
        import ssl
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE
        
        async for ws in websockets.connect(
            ws_url,
            ssl=ssl_ctx,
            extra_headers=headers,
            close_timeout=5
        ):
            self._ws = ws
            self._connected = True
            logger.info("SharedAuthWS[%s]: Connected", self.exchange)
            
            # Stop REST fallback if running
            if self._rest_poll_task:
                self._rest_poll_task.cancel()
                self._rest_poll_task = None
            
            # Re-subscribe to all symbols
            await self._resubscribe_all()
            
            try:
                async for raw in ws:
                    self._last_ws_message_time = time.time()
                    await self._handle_message(raw)
            except websockets.ConnectionClosed:
                logger.warning("SharedAuthWS[%s]: Connection closed", self.exchange)
                self._connected = False
            except Exception as exc:
                logger.error("SharedAuthWS[%s]: Message handling error: %s",
                           self.exchange, exc)
                self._connected = False
    
    def _get_websocket_url(self) -> str:
        """Get WebSocket URL for this exchange."""
        urls = {
            "extended": "wss://api.starknet.extended.exchange/stream.extended.exchange/v1/account",
            "nado": self._get_nado_ws_url(),
            "grvt": self._get_grvt_ws_url(),
        }
        return urls.get(self.exchange, "")
    
    def _get_nado_ws_url(self) -> str:
        """Get Nado WebSocket URL."""
        gateway = getattr(self.client, "_gateway_rest", "https://gateway.prod.nado.xyz")
        return gateway.replace("https://", "wss://") + "/subscribe"
    
    def _get_grvt_ws_url(self) -> str:
        """Get GRVT WebSocket URL."""
        # GRVT uses different endpoints per environment
        env = getattr(self.client, "_env", "prod")
        urls = {
            "dev": "wss://market-data.dev.gravitymarkets.io/ws/full",
            "staging": "wss://market-data.stg.gravitymarkets.io/ws/full",
            "testnet": "wss://market-data.testnet.grvt.io/ws/full",
            "prod": "wss://market-data.grvt.io/ws/full",
        }
        return urls.get(env, urls["prod"])
    
    def _get_auth_headers(self) -> dict:
        """Get authentication headers for this exchange."""
        headers = {"User-Agent": "tradeautonom/1.0"}
        
        if self.exchange == "extended":
            api_key = getattr(self.client, "_api_key", None)
            if api_key:
                headers["X-Api-Key"] = api_key
        elif self.exchange == "grvt":
            # GRVT uses cookie-based auth
            pass
        
        return headers
    
    async def _send_subscribe_message(self, symbol: str) -> None:
        """Send subscribe message to WebSocket."""
        if not self._ws:
            return
        
        # Exchange-specific subscription format
        if self.exchange == "extended":
            # Extended doesn't need explicit subscription - account stream sends all
            pass
        elif self.exchange == "nado":
            # Nado subscription format
            msg = json.dumps({"type": "subscribe", "product_id": symbol})
            await self._ws.send(msg)
        elif self.exchange == "grvt":
            # GRVT subscription format
            msg = json.dumps({"type": "subscribe", "instrument": symbol})
            await self._ws.send(msg)
    
    async def _send_unsubscribe_message(self, symbol: str) -> None:
        """Send unsubscribe message to WebSocket."""
        if not self._ws:
            return
        
        if self.exchange == "nado":
            msg = json.dumps({"type": "unsubscribe", "product_id": symbol})
            await self._ws.send(msg)
        elif self.exchange == "grvt":
            msg = json.dumps({"type": "unsubscribe", "instrument": symbol})
            await self._ws.send(msg)
    
    async def _resubscribe_all(self) -> None:
        """Re-subscribe to all symbols after reconnect."""
        symbols = self._subscription_tracker.get_subscribed_symbols()
        if symbols:
            logger.info("SharedAuthWS[%s]: Re-subscribing to %d symbols",
                       self.exchange, len(symbols))
            for symbol in symbols:
                await self._send_subscribe_message(symbol)
                await asyncio.sleep(0.1)  # Rate limit
    
    async def _handle_message(self, raw: str) -> None:
        """Handle incoming WebSocket message."""
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        
        msg_type = msg.get("type", "")
        handler = self._message_handlers.get(msg_type)
        
        if handler:
            await handler(msg)
        else:
            # Try exchange-specific handlers
            await self._handle_exchange_specific(msg)
    
    async def _handle_position_message(self, msg: dict) -> None:
        """Handle position update message."""
        # Exchange-specific parsing
        if self.exchange == "extended":
            await self._handle_extended_position(msg)
        elif self.exchange == "nado":
            await self._handle_nado_position(msg)
        elif self.exchange == "grvt":
            await self._handle_grvt_position(msg)
    
    async def _handle_trade_message(self, msg: dict) -> None:
        """Handle trade/fill message."""
        # Parse and update fill cache
        pass  # Implementation per exchange
    
    async def _handle_balance_message(self, msg: dict) -> None:
        """Handle balance update message."""
        # Parse and update balance cache
        pass  # Implementation per exchange
    
    async def _handle_exchange_specific(self, msg: dict) -> None:
        """Handle exchange-specific message formats."""
        if self.exchange == "extended":
            # Extended sends POSITION, TRADE, ORDER, BALANCE in same stream
            msg_type = msg.get("type", "")
            if msg_type == "POSITION":
                await self._handle_extended_position(msg)
            elif msg_type == "TRADE":
                await self._handle_extended_trade(msg)
        # Add other exchanges as needed
    
    # ── Exchange-Specific Handlers ─────────────────────────────────
    
    async def _handle_extended_position(self, msg: dict) -> None:
        """Parse Extended position message."""
        data = msg.get("data", {})
        positions = data.get("positions", [data]) if "positions" in data else [data]
        
        for p in positions:
            symbol = p.get("market", "")
            if not symbol:
                continue
            
            size = abs(float(p.get("qty", p.get("size", 0))))
            side_raw = p.get("side", "").lower()
            side = "long" if side_raw in ("buy", "long") else ("short" if side_raw in ("sell", "short") else "")
            entry_price = float(p.get("entryPrice", p.get("entry_price", 0)))
            upnl = float(p.get("unrealisedPnl", p.get("unrealized_pnl", 0)))
            
            await self.data_cache.update_position(
                exchange=self.exchange,
                symbol=symbol,
                size=size,
                side=side,
                entry_price=entry_price,
                unrealized_pnl=upnl,
                connected=True
            )
            
            logger.debug("SharedAuthWS[%s]: Updated position %s=%.4f",
                        self.exchange, symbol, size)
    
    async def _handle_extended_trade(self, msg: dict) -> None:
        """Parse Extended trade message."""
        data = msg.get("data", {})
        trades = data.get("trades", [])
        
        for trade in trades:
            order_id = str(trade.get("orderId", ""))
            if not order_id:
                continue
            
            await self.data_cache.update_fill(
                order_id=order_id,
                filled_qty=float(trade.get("qty", 0)),
                price=float(trade.get("price", 0)),
                is_taker=trade.get("isTaker", True),
                fee=float(trade.get("fee", 0)),
                symbol=trade.get("market", "")
            )
    
    async def _handle_nado_position(self, msg: dict) -> None:
        """Parse Nado position message."""
        # Nado position format
        pass  # TODO: Implement based on Nado message format
    
    async def _handle_grvt_position(self, msg: dict) -> None:
        """Parse GRVT position message."""
        # GRVT position format
        pass  # TODO: Implement based on GRVT message format
    
    # ── REST Fallback ──────────────────────────────────────────────
    
    async def _start_rest_fallback(self) -> None:
        """Start REST polling when WebSocket is disconnected."""
        if not self.rest_fallback or self._rest_poll_task:
            return
        
        logger.info("SharedAuthWS[%s]: Starting REST fallback polling", self.exchange)
        self._rest_poll_task = asyncio.create_task(
            self._rest_poll_loop(),
            name=f"rest-poll-{self.exchange}"
        )
    
    async def _rest_poll_loop(self) -> None:
        """REST polling loop for position updates."""
        while self._running and not self._connected:
            try:
                await self._poll_positions_rest()
                await asyncio.sleep(self._rest_poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("SharedAuthWS[%s]: REST poll error: %s", 
                           self.exchange, exc)
                await asyncio.sleep(self._rest_poll_interval * 2)
    
    async def _poll_positions_rest(self) -> None:
        """Poll positions via REST API."""
        symbols = self._subscription_tracker.get_subscribed_symbols()
        if not symbols:
            return
        
        try:
            # Use client's position fetching method
            if hasattr(self.client, 'async_get_positions'):
                positions = await self.client.async_get_positions()
                for pos in positions:
                    symbol = pos.get("symbol", "")
                    if symbol in symbols:
                        await self.data_cache.update_position(
                            exchange=self.exchange,
                            symbol=symbol,
                            size=float(pos.get("size", 0)),
                            side=pos.get("side", ""),
                            entry_price=float(pos.get("entry_price", 0)),
                            unrealized_pnl=float(pos.get("unrealized_pnl", 0)),
                            connected=False  # Via REST, not WS
                        )
        except Exception as exc:
            logger.warning("SharedAuthWS[%s]: Failed to poll positions: %s",
                         self.exchange, exc)


class SharedWebSocketManagerRegistry:
    """Registry of all shared WebSocket managers (one per exchange)."""
    
    def __init__(self, data_cache: SharedDataCache):
        self.data_cache = data_cache
        self._managers: Dict[str, SharedAuthWebSocketManager] = {}
    
    async def create_manager(self, exchange: str, client: Any) -> SharedAuthWebSocketManager:
        """Create manager for an exchange if not exists."""
        if exchange not in self._managers:
            manager = SharedAuthWebSocketManager(
                exchange=exchange,
                client=client,
                data_cache=self.data_cache
            )
            await manager.start()
            self._managers[exchange] = manager
            logger.info("WSManagerRegistry: Created manager for %s", exchange)
        
        return self._managers[exchange]
    
    async def get_manager(self, exchange: str) -> Optional[SharedAuthWebSocketManager]:
        """Get existing manager for exchange."""
        return self._managers.get(exchange)
    
    async def stop_all(self) -> None:
        """Stop all managers."""
        for exchange, manager in self._managers.items():
            logger.info("WSManagerRegistry: Stopping manager for %s", exchange)
            await manager.stop()
        self._managers.clear()
    
    def list_managers(self) -> list[str]:
        """List all managed exchanges."""
        return list(self._managers.keys())
