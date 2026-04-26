"""Bot Registry — manages multiple FundingArbEngine instances with shared resources.

Each bot shares:
- One authenticated WebSocket per exchange (position/balance/fill data)
- One OMS WebSocket for orderbook data
- Shared in-memory data cache

Bot configs are persisted to data/bots/{bot_id}/config.json.
"""

from __future__ import annotations

import asyncio
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
from app.state_machine import seed_holding_state
from app.symbol_resolver import resolve_variational_symbol

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

    async def _pre_correct_variational_symbol(
        self, bot_id: str, config: EngineConfig
    ) -> bool:
        """Phase F.4 / M9: rewrite stale Variational symbols on the config in
        place BEFORE the registry subscribes the bot to feeds.

        This must run before ``_subscribe_bot_symbols`` and ``_oms_data_layer.add_symbols``,
        otherwise the DataLayer / OMS subscribes to the stale symbol and never
        receives orderbook data.

        Returns True if any field was corrected, so the caller can decide whether
        to persist the updated config to disk.
        """
        for field, exch in (
            ("instrument_a", config.long_exchange),
            ("instrument_b", config.short_exchange),
        ):
            if exch != "variational":
                continue
            symbol = getattr(config, field)
            try:
                resolved, was_corrected, source = await resolve_variational_symbol(
                    requested_symbol=symbol,
                    oms_url=self._oms_url
                    or getattr(config, "fn_opt_shared_monitor_url", "")
                    or None,
                    variational_client=self._clients.get("variational"),
                )
            except Exception as exc:
                logger.warning(
                    "BotRegistry: Variational symbol resolver errored for %s/%s: %s",
                    bot_id, symbol, exc,
                )
                continue
            if was_corrected:
                setattr(config, field, resolved)
                logger.warning(
                    "BotRegistry: bot %s %s auto-corrected: %s → %s (source=%s)",
                    bot_id, field, symbol, resolved, source,
                )
                return True
        return False

    async def create_bot(self, bot_id: str, config: EngineConfig) -> FundingArbEngine:
        """Create a new bot, persist its config, start its engine."""
        if bot_id in self._bots:
            raise ValueError(f"Bot '{bot_id}' already exists")
        if not bot_id or not bot_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"Invalid bot_id: '{bot_id}' (use alphanumeric, -, _)")

        # Ensure shared resources are initialized
        if self._shared_data_cache is None:
            await self._init_shared_resources()

        # Phase F.4 / M9: rewrite stale Variational symbols before any
        # subscription happens, so DataLayer/OMS subscribe to the live one.
        await self._pre_correct_variational_symbol(bot_id, config)

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
            expected = {f"{e}:{s}" for e, s in oms_symbols.items()}
            missing = expected - set(results.keys())
            failed = [sym for sym, success in results.items() if not success]
            if missing or failed:
                logger.warning(
                    "BotRegistry: OMS subscription problems for %s — missing=%s failed=%s "
                    "(bot will fail pre-trade check until resolved)",
                    bot_id, sorted(missing), failed,
                )
            else:
                logger.info("BotRegistry: All OMS subscriptions successful for %s", bot_id)

        # Create engine with shared resources
        engine = FundingArbEngine(
            config=config,
            clients=self._clients,
            activity_forwarder=self._activity_forwarder,
            shared_data_cache=self._shared_data_cache,
            shared_ws_registry=self._shared_ws_registry,
            oms_data_layer=getattr(self, '_oms_data_layer', None),
            persist_config_callback=self._save_config,
        )

        await engine.start()
        self._bots[bot_id] = engine

        logger.info("Bot created: %s (long=%s:%s short=%s:%s)",
                     bot_id, config.long_exchange, config.instrument_a,
                     config.short_exchange, config.instrument_b)
        return engine

    # ── Adoption of existing hedge positions ──────────────────────────

    @staticmethod
    def _match_position(
        positions: list[dict], exchange: str, symbol: str,
    ) -> dict | None:
        """Find a position object matching the given exchange-side symbol.

        Primary: exact instrument/symbol match.
        Fallback (variational only): match by underlying token to handle
        the funding-interval drift documented in M9 (Variational position
        objects can carry stale funding_interval_s while bot configs track
        the live value).
        """
        if not positions:
            return None
        for p in positions:
            inst = p.get("instrument") or p.get("symbol") or ""
            if inst == symbol:
                return p
        if exchange == "variational" and symbol.startswith("P-"):
            parts = symbol.split("-")
            if len(parts) >= 2:
                token = parts[1].upper()
                for p in positions:
                    if str(p.get("underlying", "")).upper() == token:
                        return p
                    inst = p.get("instrument") or p.get("symbol") or ""
                    if inst.startswith("P-"):
                        ip = inst.split("-")
                        if len(ip) >= 2 and ip[1].upper() == token:
                            return p
        return None

    async def _validate_hedge_position(
        self,
        config: EngineConfig,
        expected_long_qty: float,
        expected_short_qty: float,
    ) -> tuple[float, float, float, float]:
        """Re-fetch positions from both exchanges and validate that they form
        a delta-neutral hedge that matches the user-claimed quantities.

        Returns (long_size, long_entry_price, short_size, short_entry_price)
        suitable for seed_holding_state(). Raises ValueError on any mismatch.
        """
        long_client = self._clients.get(config.long_exchange)
        short_client = self._clients.get(config.short_exchange)
        if not long_client:
            raise ValueError(
                f"Exchange client not loaded: {config.long_exchange} "
                f"(vault may be locked)"
            )
        if not short_client:
            raise ValueError(
                f"Exchange client not loaded: {config.short_exchange} "
                f"(vault may be locked)"
            )

        try:
            long_positions = await asyncio.to_thread(
                long_client.fetch_positions, [config.instrument_a],
            )
        except Exception as exc:
            raise ValueError(
                f"Failed to fetch {config.long_exchange} positions: {exc}",
            ) from exc
        try:
            short_positions = await asyncio.to_thread(
                short_client.fetch_positions, [config.instrument_b],
            )
        except Exception as exc:
            raise ValueError(
                f"Failed to fetch {config.short_exchange} positions: {exc}",
            ) from exc

        long_pos = self._match_position(
            long_positions, config.long_exchange, config.instrument_a,
        )
        short_pos = self._match_position(
            short_positions, config.short_exchange, config.instrument_b,
        )

        if not long_pos:
            raise ValueError(
                f"No open position on {config.long_exchange} for "
                f"{config.instrument_a}",
            )
        if not short_pos:
            raise ValueError(
                f"No open position on {config.short_exchange} for "
                f"{config.instrument_b}",
            )

        long_side = str(long_pos.get("side", "")).upper()
        short_side = str(short_pos.get("side", "")).upper()
        if long_side and long_side != "LONG":
            raise ValueError(
                f"{config.long_exchange}/{config.instrument_a} is {long_side}, "
                f"expected LONG",
            )
        if short_side and short_side != "SHORT":
            raise ValueError(
                f"{config.short_exchange}/{config.instrument_b} is {short_side}, "
                f"expected SHORT",
            )

        long_size = abs(float(long_pos.get("size", 0)))
        short_size = abs(float(short_pos.get("size", 0)))

        if long_size <= 0 or short_size <= 0:
            raise ValueError(
                f"Position size must be > 0 (long={long_size}, short={short_size})",
            )

        # Quantity match: both legs must agree within 1e-6 relative tolerance.
        if (
            abs(long_size - short_size) / max(long_size, short_size) > 1e-6
        ):
            raise ValueError(
                f"Hedge size mismatch: long={long_size}, short={short_size}. "
                f"Both legs must have identical size to be adopted.",
            )

        # User-claimed quantity must also match (defends against UI race
        # where the user confirmed a value that has since changed on the
        # exchange — e.g. someone partially closed a leg).
        for label, claimed, actual in (
            ("long", expected_long_qty, long_size),
            ("short", expected_short_qty, short_size),
        ):
            if claimed and actual:
                if abs(actual - abs(claimed)) / max(actual, abs(claimed)) > 1e-6:
                    raise ValueError(
                        f"{label} size changed since you confirmed: "
                        f"exchange={actual}, claimed={claimed}. Refresh "
                        f"and retry.",
                    )

        long_entry = float(long_pos.get("entry_price", 0))
        short_entry = float(short_pos.get("entry_price", 0))
        return long_size, long_entry, short_size, short_entry

    def _check_no_existing_bot_owns_position(
        self,
        long_exchange: str,
        long_symbol: str,
        short_exchange: str,
        short_symbol: str,
    ) -> None:
        """Refuse to adopt a position that another bot already manages.

        Looks at every (exchange, symbol) pair across both legs of every
        bot config — if either leg of the new adoption matches either leg
        of an existing bot, abort.
        """
        for bot_id, engine in self._bots.items():
            c = engine.config
            existing = {
                (c.long_exchange, c.instrument_a),
                (c.short_exchange, c.instrument_b),
            }
            attempted = {
                (long_exchange, long_symbol),
                (short_exchange, short_symbol),
            }
            if existing & attempted:
                raise ValueError(
                    f"Position already managed by bot '{bot_id}'",
                )

    async def adopt_bot(
        self,
        bot_id: str,
        config: EngineConfig,
        long_qty: float,
        short_qty: float,
    ) -> FundingArbEngine:
        """Create a bot that takes over an existing delta-neutral hedge.

        Mirrors create_bot() but seeds a HOLDING state.json before starting
        the engine, so the bot starts in HOLDING (not IDLE) and can be
        exited via the normal Stop button. The position itself was opened
        by the user — the bot just inherits its lifecycle from this point.

        Args:
            bot_id: alphanumeric bot identifier (no existing bot may share it).
            config: standard EngineConfig (long_exchange, short_exchange,
                instrument_a, instrument_b, quantity, twap settings, etc.).
            long_qty:  user-claimed long size (re-validated against exchange).
            short_qty: user-claimed short size (re-validated against exchange).

        Raises:
            ValueError: bot_id taken, position not found, sizes mismatched,
                position already managed by another bot, etc.
        """
        if bot_id in self._bots:
            raise ValueError(f"Bot '{bot_id}' already exists")
        if not bot_id or not bot_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError(
                f"Invalid bot_id: '{bot_id}' (use alphanumeric, -, _)",
            )

        # Ensure shared resources are initialized
        if self._shared_data_cache is None:
            await self._init_shared_resources()

        # Same Variational symbol auto-correction as create_bot — important
        # because the user-supplied instrument_b may carry a stale funding
        # interval that no longer matches the live tradable symbol.
        await self._pre_correct_variational_symbol(bot_id, config)

        # No other bot may already be managing either leg.
        self._check_no_existing_bot_owns_position(
            config.long_exchange, config.instrument_a,
            config.short_exchange, config.instrument_b,
        )

        # Re-fetch positions from the exchanges and confirm the hedge is
        # still delta-neutral and matches user expectation.
        (
            long_size, long_entry, short_size, short_entry,
        ) = await self._validate_hedge_position(config, long_qty, short_qty)

        # Persist the bot config so a recycle survives the adoption.
        config.job_id = bot_id
        self._save_config(bot_id, config)

        # Seed the HOLDING state BEFORE engine.start() so load_state() picks
        # it up. State machine convention: long is positive, short negative.
        seed_holding_state(
            job_id=bot_id,
            long_exchange=config.long_exchange,
            long_symbol=config.instrument_a,
            long_qty=long_size,
            long_entry_price=long_entry,
            short_exchange=config.short_exchange,
            short_symbol=config.instrument_b,
            short_qty=-short_size,
            short_entry_price=short_entry,
        )

        # Standard subscribe + OMS-add flow (same as create_bot).
        await self._subscribe_bot_symbols(bot_id, config)
        if self._oms_data_layer and hasattr(self._oms_data_layer, "add_symbols"):
            oms_symbols = {
                config.long_exchange: config.instrument_a,
                config.short_exchange: config.instrument_b,
            }
            results = await self._oms_data_layer.add_symbols(oms_symbols)
            failed = [sym for sym, ok in results.items() if not ok]
            if failed:
                logger.warning(
                    "BotRegistry.adopt_bot: OMS subscription problems for %s: %s",
                    bot_id, failed,
                )

        engine = FundingArbEngine(
            config=config,
            clients=self._clients,
            activity_forwarder=self._activity_forwarder,
            shared_data_cache=self._shared_data_cache,
            shared_ws_registry=self._shared_ws_registry,
            oms_data_layer=getattr(self, "_oms_data_layer", None),
            persist_config_callback=self._save_config,
        )

        # engine.start() → state_machine.load_state() → bot is HOLDING.
        # sync_position_from_exchange runs and confirms qty against exchange.
        await engine.start()
        self._bots[bot_id] = engine

        logger.info(
            "Bot adopted: %s (long=%s:%s qty=%.6f @ %.6f, short=%s:%s qty=%.6f @ %.6f)",
            bot_id,
            config.long_exchange, config.instrument_a, long_size, long_entry,
            config.short_exchange, config.instrument_b, short_size, short_entry,
        )
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

        # Phase F.4 / M9: rewrite stale Variational symbols on every restored
        # config before any auth-WS or OMS subscription happens. Without this,
        # subscriptions go out for the (stale) symbol and never receive data.
        # Persists the corrected config back to disk so the next restart loads
        # the live symbol directly.
        #
        # Wrapped in a hard 10s total budget per bot so a stuck OMS lookup
        # never blocks BotRegistry startup. If the budget fires, we just keep
        # the bot's existing (possibly stale) symbol — the existing feed-error
        # path will still surface the issue, just without auto-healing.
        for bot_id, config in configs:
            try:
                corrected = await asyncio.wait_for(
                    self._pre_correct_variational_symbol(bot_id, config),
                    timeout=10.0,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "BotRegistry: Variational symbol auto-correction timed out for %s — "
                    "skipping (bot will start with current config)",
                    bot_id,
                )
                continue
            except Exception as exc:
                logger.warning(
                    "BotRegistry: Variational symbol auto-correction errored for %s: %s",
                    bot_id, exc,
                )
                continue
            if corrected:
                try:
                    self._save_config(bot_id, config)
                except Exception as exc:
                    logger.warning(
                        "BotRegistry: failed to persist auto-corrected config for %s: %s",
                        bot_id, exc,
                    )

        # Subscribe all symbols to auth WS managers
        for bot_id, config in configs:
            await self._subscribe_bot_symbols(bot_id, config)

        # Add all symbols to OMS DataLayer.
        # NOTE: `add_symbols` takes a dict keyed by exchange, so stacking all
        # bots into a single dict collapses entries when multiple bots share
        # an exchange with different symbols. Call it per-bot to preserve
        # every (exchange, symbol) pair.
        if self._oms_data_layer and hasattr(self._oms_data_layer, 'add_symbols'):
            for bot_id, config in configs:
                oms_symbols = {
                    config.long_exchange: config.instrument_a,
                    config.short_exchange: config.instrument_b,
                }
                results = await self._oms_data_layer.add_symbols(oms_symbols)
                expected = {f"{e}:{s}" for e, s in oms_symbols.items()}
                missing = expected - set(results.keys())
                failed = [sym for sym, success in results.items() if not success]
                if missing or failed:
                    logger.warning(
                        "BotRegistry: OMS subscription problems during restore for %s — missing=%s failed=%s",
                        bot_id, sorted(missing), failed,
                    )

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
                    oms_data_layer=getattr(self, '_oms_data_layer', None),
                    persist_config_callback=self._save_config,
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
        # Phase F.4 M3.C.1: trigger an event-driven flush so the new
        # config lands in R2 immediately. Fire-and-forget; no-op on V1.
        try:
            from app.cloud_persistence import request_flush_soon
            request_flush_soon(reason=f"event:bot_config_save:{bot_id}")
        except Exception:
            pass

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
