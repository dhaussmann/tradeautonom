"""Bot Registry — manages multiple FundingArbEngine instances.

Each bot has its own EngineConfig, DataLayer, StateMachine, and RiskManager.
Exchange clients are shared across all bots (stateless, thread-safe).

Bot configs are persisted to data/bots/{bot_id}/config.json.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.engine import EngineConfig, FundingArbEngine

logger = logging.getLogger("tradeautonom.bot_registry")

_BOTS_DIR = Path("data/bots")


class BotRegistry:
    """CRUD + lifecycle manager for parallel FundingArbEngine bots."""

    def __init__(self, clients: dict[str, Any], settings: Any = None) -> None:
        self._clients = clients
        self._settings = settings
        self._bots: dict[str, FundingArbEngine] = {}

    # ── CRUD ───────────────────────────────────────────────────────────

    async def create_bot(self, bot_id: str, config: EngineConfig) -> FundingArbEngine:
        """Create a new bot, persist its config, start its engine."""
        if bot_id in self._bots:
            raise ValueError(f"Bot '{bot_id}' already exists")
        if not bot_id or not bot_id.replace("-", "").replace("_", "").isalnum():
            raise ValueError(f"Invalid bot_id: '{bot_id}' (use alphanumeric, -, _)")

        config.job_id = bot_id
        self._save_config(bot_id, config)

        engine = FundingArbEngine(config=config, clients=self._clients)
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
        restored = 0
        for config_path in sorted(_BOTS_DIR.glob("*/config.json")):
            bot_id = config_path.parent.name
            if bot_id in self._bots:
                continue
            try:
                config = self._load_config(bot_id)
                config.job_id = bot_id
                engine = FundingArbEngine(config=config, clients=self._clients)
                await engine.start()
                self._bots[bot_id] = engine
                restored += 1
                logger.info("Restored bot: %s", bot_id)
            except Exception as exc:
                logger.warning("Failed to restore bot '%s': %s", bot_id, exc)

        logger.info("BotRegistry started: %d bots (%d restored)", len(self._bots), restored)

    async def stop_all(self) -> None:
        """Stop all bot engines."""
        for bot_id, engine in self._bots.items():
            try:
                await engine.stop()
                logger.info("Bot stopped: %s", bot_id)
            except Exception as exc:
                logger.warning("Error stopping bot '%s': %s", bot_id, exc)
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
