"""Bot Registry — manages multiple FundingArbEngine instances with shared resources.

Each bot shares:
- One authenticated WebSocket per exchange (position/balance/fill data)
- One OMS WebSocket for orderbook data
- Shared in-memory data cache

Bot configs are persisted to data/bots/{bot_id}/config.json.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional

from app.engine import EngineConfig, FundingArbEngine
from app.shared_data_cache import SharedDataCache
from app.shared_auth_ws_manager import SharedWebSocketManagerRegistry

logger = logging.getLogger("tradeautonom.bot_registry")

_BOTS_DIR = Path("data/bots")


class BotRegistry:
    """CRUD + lifecycle manager for parallel FundingArbEngine bots with shared resources."""

    def __init__(
        self,
        clients: dict[str, Any],
        settings: Any = None,
        activity_forwarder: Any = None,
        oms_url: Optional[str] = None
    ) -> None:
        self._clients = clients
        self._settings = settings
        self._activity_forwarder = activity_forwarder
        self._oms_url = oms_url or os.environ.get("FN_OPT_SHARED_MONITOR_URL", "")
        self._bots: dict[str, FundingArbEngine] = {}

        # Shared resources (initialized in start_all)
        self._shared_data_cache: Optional[SharedDataCache] = None
        self._shared_ws_registry: Optional[SharedWebSocketManagerRegistry] = None
        self._shared_oms_task: Optional[Any] = None  # Will be OMS WS task
        self._oms_data_layer: Optional[Any] = None  # OMS DataLayer

    # ── Shared Resource Management ────────────────────────────────────

    async def _init_shared_resources(self) -> None:
        """Initialize shared resources (data cache, WS managers)."""
        logger.info("BotRegistry: Initializing shared resources")

        # Create shared data cache
        self._shared_data_cache = SharedDataCache()

        # Create shared WebSocket manager registry
        self._shared_ws_registry = SharedWebSocketManagerRegistry(
            data_cache=self._shared_data_cache
        )

        # Start shared authenticated WebSockets for each exchange
        for exchange, client in self._clients.items():
            if exchange == "variational":
                # Variational has no WebSocket - uses REST polling
                continue
            try:
                await self._shared_ws_registry.create_manager(exchange, client)
            except Exception as exc:
                logger.error("BotRegistry: Failed to create WS manager for %s: %s",
                           exchange, exc)

        # Start OMS WebSocket if URL configured
        if self._oms_url:
            await self._start_oms_websocket()

        logger.info("BotRegistry: Shared resources initialized (exchanges: %s)",
                   list(self._shared_ws_registry.list_managers()))

    async def _start_oms_websocket(self) -> None:
        """Start shared OMS WebSocket for orderbook data."""
        # Import here to avoid circular dependency
        try:
            from app.data_layer import DataLayer
            # Create a minimal DataLayer just for OMS
            self._oms_data_layer = DataLayer(
                stale_ms=5000,
                shared_monitor_url=self._oms_url
            )
            # Start with empty symbols - will add dynamically as bots start
            await self._oms_data_layer.start(self._clients, {})
            logger.info("BotRegistry: OMS WebSocket started: %s", self._oms_url)
        except Exception as exc:
            logger.error("BotRegistry: Failed to initialize OMS WebSocket: %s", exc)

    async def _stop_shared_resources(self) -> None:
        """Stop all shared resources."""
        logger.info("BotRegistry: Stopping shared resources")

        # Stop WebSocket managers
        if self._shared_ws_registry:
            await self._shared_ws_registry.stop_all()

        # Stop OMS
        if hasattr(self, '_oms_data_layer') and self._oms_data_layer:
            await self._oms_data_layer.stop()

        self._shared_data_cache = None
        self._shared_ws_registry = None

        logger.info("BotRegistry: Shared resources stopped")

    async def _subscribe_bot_symbols(self, bot_id: str, config: EngineConfig) -> None:
        """Subscribe bot's symbols to shared WebSocket managers."""
        if not self._shared_ws_registry:
            return

        # Subscribe long exchange
        manager = await self._shared_ws_registry.get_manager(config.long_exchange)
        if manager:
            await manager.subscribe_symbol(bot_id, config.instrument_a)

        # Subscribe short exchange
        manager = await self._shared_ws_registry.get_manager(config.short_exchange)
        if manager:
            await manager.subscribe_symbol(bot_id, config.instrument_b)

        logger.debug("BotRegistry: Subscribed %s to symbols (long=%s:%s, short=%s:%s)",
                    bot_id, config.long_exchange, config.instrument_a,
                    config.short_exchange, config.instrument_b)

    async def _unsubscribe_bot_symbols(self, bot_id: str, config: EngineConfig) -> None:
        """Unsubscribe bot's symbols from shared WebSocket managers."""
        if not self._shared_ws_registry:
            return

        # Unsubscribe long exchange
        manager = await self._shared_ws_registry.get_manager(config.long_exchange)
        if manager:
            await manager.unsubscribe_symbol(bot_id, config.instrument_a)

        # Unsubscribe short exchange
        manager = await self._shared_ws_registry.get_manager(config.short_exchange)
        if manager:
            await manager.unsubscribe_symbol(bot_id, config.instrument_b)

        logger.debug("BotRegistry: Unsubscribed %s from symbols", bot_id)

    # ── CRUD ───────────────────────────────────────────────────────────

    async def create_bot(self, bot_id: str, config: EngineConfig) -> FundingArbEngine:
        """Create a new bot, persist its config, start its engine."""
        if bot_id in self._bots:
            raise ValueError(f"Bot '{bot_id}' already exists")
        if not bot_id or not bot_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"Invalid bot_id: '{bot_id}' (use alphanumeric, -, _)")

        # Ensure shared resources are initialized
        if self._shared_data_cache is None:
            await self._init_shared_resources()

        config.job_id = bot_id
        self._save_config(bot_id, config)

        # Subscribe symbols before starting engine
        await self._subscribe_bot_symbols(bot_id, config)

        # Add symbols to OMS DataLayer for orderbook data
        if self._oms_data_layer and hasattr(self._oms_data_layer, 'add_symbols'):
            oms_symbols = {
                config.long_exchange: config.instrument_a,
                config.short_exchange: config.instrument_b,
            }
            results = await self._oms_data_layer.add_symbols(oms_symbols)
            failed = [sym for sym, success in results.items() if not success]
            if failed:
                logger.warning("BotRegistry: Some OMS subscriptions failed for %s: %s", bot_id, failed)
            else:
                logger.info("BotRegistry: All OMS subscriptions successful for %s", bot_id)

        # Create engine with shared resources
        engine = FundingArbEngine(
            config=config,
            clients=self._clients,
            activity_forwarder=self._activity_forwarder,
            shared_data_cache=self._shared_data_cache,
            shared_ws_registry=self._shared_ws_registry,
            oms_data_layer=getattr(self, '_oms_data_layer', None)
        )

        await engine.start()
        self._bots[bot_id] = engine

        logger.info("Bot created: %s (long=%s:%s short=%s:%s)",
                     bot_id, config.long_exchange, config.instrument_a,
                     config.short_exchange, config.instrument_b)
        return engine

    async def delete_bot(self, bot_id: str) -> None:
        """Delete a bot (must be IDLE). Stops engine, removes from registry."""
        engine = self.get_bot(bot_id)
        if engine._state_machine and engine._state_machine.state.value != "IDLE":
            raise RuntimeError(f"Cannot delete bot '{bot_id}': state is {engine._state_machine.state.value}, must be IDLE")

        # Unsubscribe symbols from auth WS managers
        await self._unsubscribe_bot_symbols(bot_id, engine.config)

        # Remove symbols from OMS DataLayer
        if self._oms_data_layer and hasattr(self._oms_data_layer, 'remove_symbols'):
            oms_symbols = {
                engine.config.long_exchange: engine.config.instrument_a,
                engine.config.short_exchange: engine.config.instrument_b,
            }
            await self._oms_data_layer.remove_symbols(oms_symbols)

        await engine.stop()
        del self._bots[bot_id]

        # Remove config file (keep position file for safety)
        config_path = _BOTS_DIR / bot_id / "config.json"
        if config_path.exists():
            config_path.unlink()
        logger.info("Bot deleted: %s", bot_id)

    def get_bot(self, bot_id: str) -> FundingArbEngine:
        """Get a bot by ID. Raises KeyError if not found."""
        if bot_id not in self._bots:
            raise KeyError(f"Bot '{bot_id}' not found")
        return self._bots[bot_id]

    def list_bots(self) -> list[dict]:
        """List all bots with summary info."""
        result = []
        for bot_id, engine in self._bots.items():
            sm = engine._state_machine
            result.append({
                "bot_id": bot_id,
                "state": sm.state.value if sm else "NOT_STARTED",
                "is_running": engine._is_running,
                "long_exchange": engine.config.long_exchange,
                "short_exchange": engine.config.short_exchange,
                "instrument_a": engine.config.instrument_a,
                "instrument_b": engine.config.instrument_b,
                "quantity": float(engine.config.quantity),
            })
        return result

    @property
    def bot_ids(self) -> list[str]:
        return list(self._bots.keys())

    def __contains__(self, bot_id: str) -> bool:
        return bot_id in self._bots

    # ── Lifecycle ──────────────────────────────────────────────────────

    async def start_all(self) -> None:
        """Restore all persisted bots from disk and start their engines."""
        _BOTS_DIR.mkdir(parents=True, exist_ok=True)

        # Load all configs first
        configs = []
        for config_path in sorted(_BOTS_DIR.glob("*/config.json")):
            bot_id = config_path.parent.name
            if bot_id in self._bots:
                continue
            try:
                config = self._load_config(bot_id)
                configs.append((bot_id, config))
            except Exception as exc:
                logger.warning("Failed to load config for bot '%s': %s", bot_id, exc)

        if not configs:
            logger.info("BotRegistry: No bots to restore")
            return

        # Initialize shared resources once (guard against double-init if create_bot ran first)
        if self._shared_data_cache is None:
            await self._init_shared_resources()

        # Subscribe all symbols to auth WS managers
        for bot_id, config in configs:
            await self._subscribe_bot_symbols(bot_id, config)

        # Add all symbols to OMS DataLayer
        if self._oms_data_layer and hasattr(self._oms_data_layer, 'add_symbols'):
            all_oms_symbols = {}
            for bot_id, config in configs:
                all_oms_symbols[config.long_exchange] = config.instrument_a
                all_oms_symbols[config.short_exchange] = config.instrument_b
            
            results = await self._oms_data_layer.add_symbols(all_oms_symbols)
            failed = [sym for sym, success in results.items() if not success]
            if failed:
                logger.warning("BotRegistry: Some OMS subscriptions failed during restore: %s", failed)

        # Start all bots with shared resources
        restored = 0
        for bot_id, config in configs:
            try:
                config.job_id = bot_id

                engine = FundingArbEngine(
                    config=config,
                    clients=self._clients,
                    activity_forwarder=self._activity_forwarder,
                    shared_data_cache=self._shared_data_cache,
                    shared_ws_registry=self._shared_ws_registry,
                    oms_data_layer=getattr(self, '_oms_data_layer', None)
                )

                await engine.start()
                self._bots[bot_id] = engine
                restored += 1
                logger.info("Restored bot: %s", bot_id)
            except Exception as exc:
                logger.warning("Failed to restore bot '%s': %s", bot_id, exc)
                # Unsubscribe this bot's symbols
                await self._unsubscribe_bot_symbols(bot_id, config)

        logger.info("BotRegistry started: %d bots (%d restored)", len(self._bots), restored)

    async def stop_all(self) -> None:
        """Stop all bot engines and shared resources."""
        # Stop all bots
        for bot_id, engine in self._bots.items():
            try:
                await engine.stop()
                logger.info("Bot stopped: %s", bot_id)
            except Exception as exc:
                logger.warning("Error stopping bot '%s': %s", bot_id, exc)

        # Stop shared resources
        await self._stop_shared_resources()

        self._bots.clear()
        logger.info("BotRegistry stopped: %d bots", len(self._bots))

    # ── Config persistence ─────────────────────────────────────────────

    def _save_config(self, bot_id: str, config: EngineConfig) -> None:
        """Persist bot config to disk."""
        bot_dir = _BOTS_DIR / bot_id
        bot_dir.mkdir(parents=True, exist_ok=True)
        config_path = bot_dir / "config.json"
        data = asdict(config)
        # Convert Decimal to str for JSON serialization
        for k, v in data.items():
            if isinstance(v, Decimal):
                data[k] = str(v)
        with open(config_path, "w") as fh:
            json.dump(data, fh, indent=2)
        logger.debug("Saved config for bot '%s'", bot_id)

    def _load_config(self, bot_id: str) -> EngineConfig:
        """Load bot config from disk."""
        config_path = _BOTS_DIR / bot_id / "config.json"
        with open(config_path) as fh:
            data = json.load(fh)
        # Convert quantity back to Decimal
        if "quantity" in data:
            data["quantity"] = Decimal(str(data["quantity"]))
        return EngineConfig(**data)

    def update_bot_config(self, bot_id: str, config: EngineConfig) -> None:
        """Update persisted config for an existing bot."""
        if bot_id not in self._bots:
            raise KeyError(f"Bot '{bot_id}' not found")
        self._save_config(bot_id, config)
