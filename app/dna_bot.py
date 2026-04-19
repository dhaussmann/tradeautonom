"""Delta-Neutral Arbitrage (DNA) Bot.

Polls OMS arb opportunities via HTTP and opens delta-neutral
positions across two DEXes when profitable spreads appear.

Flow:
  1. Poll OMS /arb/opportunities for profitable spreads.
  2. When DEX_A bid > DEX_B ask (profitable arb detected):
     - BUY on DEX_B (cheaper ask), SHORT on DEX_A (higher bid)
     - Both legs executed quasi-simultaneously via asyncio.gather
     - IOC market orders for immediate execution
  3. Track up to max_positions, each with a fixed notional size.
  4. Position closing is handled separately (future implementation).

Each position is delta-neutral: same token quantity on both sides.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from pathlib import Path
from typing import Any
import urllib.error
import urllib.request

logger = logging.getLogger("tradeautonom.dna_bot")

# Persist state to data/dna_bot/{bot_id}/
_DNA_DIR = Path("data/dna_bot")


# ── Data classes ──────────────────────────────────────────────────

@dataclass
class DNAPosition:
    """A single delta-neutral arb position."""
    position_id: str
    token: str
    buy_exchange: str
    buy_symbol: str
    sell_exchange: str
    sell_symbol: str
    quantity: float               # token qty (min of both sides, for notional calc)
    buy_fill_price: float         # actual fill price on buy side
    sell_fill_price: float        # actual fill price on sell side
    entry_spread_bps: float       # spread at entry time
    notional_usd: float           # approximate USD value
    opened_at: float              # epoch seconds
    status: str = "open"          # open / closing / closed
    buy_fill_qty: float = 0.0     # actual filled qty on buy side
    sell_fill_qty: float = 0.0    # actual filled qty on sell side
    exit_mode: str = "direct"     # mode at time of open
    exit_min_hold_s: float = 300.0  # seconds before auto-close eligible
    exit_threshold_bps: float = 0.01  # spread threshold for close
    closed_at: float | None = None
    close_spread_bps: float | None = None
    close_reason: str = ""        # "arb_closed" | "manual" | ""
    close_buy_fill_price: float = 0.0   # exit fill price on buy side (reverse sell)
    close_sell_fill_price: float = 0.0  # exit fill price on sell side (reverse buy)
    simulation: bool = False         # snapshot of config.simulation at open time


@dataclass
class DNAConfig:
    """Configuration for a DNA bot instance."""
    bot_id: str = ""
    oms_url: str = "http://192.168.133.100:8099"
    position_size_usd: float = 1000.0     # notional USD per position
    max_positions: int = 3                 # max concurrent positions
    min_profit_bps: float = 0.0           # 0 = use OMS fee thresholds (already filtered)
    spread_mode: str = "delta_neutral"    # "delta_neutral" | "half_neutral" | "custom"
    custom_min_spread_bps: float = 5.0    # only used when spread_mode == "custom"
    exchanges: list[str] = field(default_factory=lambda: ["extended", "grvt", "nado"])
    slippage_tolerance_pct: float = 0.5   # max slippage for IOC orders
    size_tolerance_pct: float = 5.0       # accept position if within X% of target
    simulation: bool = False              # paper-trade mode
    tick_interval_s: float = 0.5          # how often to process signals
    exit_mode: str = "direct"             # "direct" | "hours" | "days" | "manual"
    exit_min_hold_minutes: float = 5.0    # min hold for "direct" mode
    exit_min_hold_hours: float = 8.0      # min hold for "hours" mode
    exit_min_hold_days: float = 7.0       # min hold for "days" mode
    exit_threshold_bps: float = 0.01      # spread considered "closed"
    excluded_tokens: list[str] = field(default_factory=list)  # tokens to skip
    auto_exclude_open_positions: bool = True  # auto-add tokens with open exchange positions


@dataclass
class DNALegResult:
    """Result of executing one leg."""
    success: bool
    exchange: str
    symbol: str
    side: str
    quantity: float
    fill_price: float | None = None
    order_id: str | None = None
    error: str | None = None


# ── DNA Bot ───────────────────────────────────────────────────────

class DNABot:
    """Delta-Neutral Arbitrage bot.

    Connects to OMS arb feed, opens positions when profitable
    spreads appear, up to max_positions.
    """

    def __init__(
        self,
        config: DNAConfig,
        clients: dict[str, Any],
        activity_forwarder: Any | None = None,
    ) -> None:
        self.config = config
        self._clients = clients  # exchange name → AsyncExchangeClient
        self._positions: list[DNAPosition] = []
        self._ws_task: asyncio.Task | None = None
        self._running = False
        self._activity_log: list[dict] = []
        self._activity_forwarder = activity_forwarder
        self._oms_fee_config: dict | None = None  # cached /arb/config response
        self._leverage_applied: set[tuple[str, str]] = set()  # (exchange, symbol) where max leverage was set

        # Restore positions and config from disk
        self._load_state()
        self._load_config()

    # ── Lifecycle ──────────────────────────────────────────────────

    @staticmethod
    def _token_from_instrument(instrument: str) -> str:
        """Extract base token from instrument name.

        Examples: HYPE-USD -> HYPE, HYPE_USDT_Perp -> HYPE, HYPE-PERP -> HYPE
        """
        # Remove common suffixes
        for sep in ("_USDT_Perp", "-USD", "-PERP", "_USDT", "-USDT", "_USD"):
            if instrument.upper().endswith(sep.upper()):
                return instrument[: len(instrument) - len(sep)].upper()
        # Fallback: split on first separator
        for ch in ("_", "-", "/"):
            if ch in instrument:
                return instrument.split(ch)[0].upper()
        return instrument.upper()

    async def preflight_check(self) -> dict:
        """Run connectivity pre-flight checks for all configured exchanges and OMS.

        Returns a dict with per-exchange and OMS status:
        {
            "ok": bool,          # all checks pass
            "can_start": bool,   # at least 2 exchanges + OMS health OK
            "checks": { "extended": {...}, "nado": {...}, "oms": {...} }
        }
        """
        checks: dict[str, dict] = {}
        passing_exchanges = 0

        # ── Exchange checks: positions + balance ──
        for exch_name in self.config.exchanges:
            result: dict[str, Any] = {"positions": False, "balance": False, "error": None}
            client = self._clients.get(exch_name)
            if not client:
                result["error"] = "Client not registered (missing API keys?)"
                checks[exch_name] = result
                continue
            # Positions
            try:
                await asyncio.wait_for(client.async_fetch_positions(), timeout=10)
                result["positions"] = True
            except asyncio.TimeoutError:
                result["error"] = "Positions request timed out"
            except Exception as exc:
                result["error"] = f"Positions: {exc}"
            # Balance / account summary
            try:
                if hasattr(client, "get_account_summary"):
                    await asyncio.wait_for(
                        asyncio.to_thread(client.get_account_summary), timeout=10,
                    )
                    result["balance"] = True
                else:
                    result["balance"] = None  # not supported
            except asyncio.TimeoutError:
                result["error"] = (result["error"] or "") + "; Balance request timed out"
            except Exception as exc:
                result["error"] = (result["error"] or "") + f"; Balance: {exc}"

            # Nado signer verification
            if exch_name == "nado" and hasattr(client, "verify_signer"):
                try:
                    signer_info = await asyncio.to_thread(client.verify_signer)
                    result["signer_ok"] = signer_info.get("ok", False)
                    result["signer_local"] = signer_info.get("local", "")
                    result["signer_remote"] = signer_info.get("remote", "")
                    result["signing_mode"] = signer_info.get("signing_mode", "")
                    if not signer_info.get("ok"):
                        err = signer_info.get("error", "Signer mismatch")
                        result["error"] = (result["error"] or "") + f"; {err}"
                except Exception as exc:
                    result["signer_ok"] = None
                    result["error"] = (result["error"] or "") + f"; Signer check: {exc}"

            if result["positions"] and result["balance"] is not False and result.get("signer_ok", True) is not False:
                passing_exchanges += 1
            checks[exch_name] = result

        # ── OMS checks: health + per-exchange book sample ──
        oms_result: dict[str, Any] = {"health": False, "books": {}, "error": None}
        oms_url = self.config.oms_url.rstrip("/")
        # Health
        try:
            req = urllib.request.Request(f"{oms_url}/health", method="GET")
            resp = await asyncio.wait_for(
                asyncio.to_thread(urllib.request.urlopen, req, None, 5), timeout=8,
            )
            data = json.loads(resp.read().decode())
            oms_result["health"] = data.get("status") == "ok"
            oms_result["feeds"] = data.get("feeds", 0)
        except Exception as exc:
            oms_result["error"] = f"Health: {exc}"

        # Per-exchange book sample (pick one known symbol per exchange)
        _sample_symbols = {
            "extended": "BTC-USD",
            "nado": "BTC-PERP",
            "grvt": "BTC_USDT_Perp",
        }
        for exch_name in self.config.exchanges:
            sym = _sample_symbols.get(exch_name, "BTC-USD")
            try:
                req = urllib.request.Request(
                    f"{oms_url}/book/{exch_name}/{sym}", method="GET",
                )
                resp = await asyncio.wait_for(
                    asyncio.to_thread(urllib.request.urlopen, req, None, 5), timeout=8,
                )
                book = json.loads(resp.read().decode())
                has_data = bool(book.get("bids")) and bool(book.get("asks"))
                oms_result["books"][exch_name] = has_data
            except Exception:
                oms_result["books"][exch_name] = False

        checks["oms"] = oms_result

        all_ok = (
            passing_exchanges == len(self.config.exchanges)
            and oms_result["health"]
            and all(oms_result["books"].values())
        )
        can_start = passing_exchanges >= 2 and oms_result["health"]

        return {"ok": all_ok, "can_start": can_start, "checks": checks}

    async def _fetch_existing_tokens(self) -> set[str]:
        """Fetch open positions from configured exchanges and return token set."""
        tokens: set[str] = set()
        for exchange_name in self.config.exchanges:
            client = self._clients.get(exchange_name)
            if not client:
                continue
            try:
                positions = await client.async_fetch_positions()
                for p in positions:
                    size = float(p.get("size", 0))
                    if size != 0:
                        token = self._token_from_instrument(p.get("instrument", ""))
                        if token:
                            tokens.add(token)
            except Exception as exc:
                logger.warning("DNA: failed to fetch positions from %s: %s", exchange_name, exc)
        return tokens

    async def start(self) -> None:
        """Start the DNA bot: connect to OMS via WebSocket and begin watching."""
        if self._running:
            return

        # Auto-exclude tokens with existing positions on configured exchanges
        if self.config.auto_exclude_open_positions:
            existing = await self._fetch_existing_tokens()
            if existing:
                before = set(t.upper() for t in self.config.excluded_tokens)
                added = existing - before
                if added:
                    self.config.excluded_tokens = list(before | existing)
                    logger.info("DNA: auto-excluded tokens with open positions: %s", sorted(added))
                    self._log_activity("auto_exclude", f"Auto-excluded tokens: {sorted(added)}")

        self._running = True
        self._ws_task = asyncio.create_task(self._ws_loop())
        self._signer_check_task: asyncio.Task | None = None
        if "nado" in self.config.exchanges:
            self._signer_check_task = asyncio.create_task(self._nado_signer_watchdog())
        self._log_activity("started", f"DNA bot started (max_pos={self.config.max_positions}, "
                           f"size=${self.config.position_size_usd}, exit={self.config.exit_mode}, "
                           f"excluded={self.config.excluded_tokens})")
        logger.info("DNA bot '%s' started (exit_mode=%s, excluded=%s)",
                    self.config.bot_id, self.config.exit_mode, self.config.excluded_tokens)

    async def stop(self) -> None:
        """Stop the DNA bot (does NOT close positions)."""
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
        if getattr(self, "_signer_check_task", None):
            self._signer_check_task.cancel()
            try:
                await self._signer_check_task
            except (asyncio.CancelledError, Exception):
                pass
        self._save_state()
        self._log_activity("stopped", "DNA bot stopped")
        logger.info("DNA bot '%s' stopped", self.config.bot_id)

    async def reset(self) -> None:
        """Reset bot: stop, clear all positions and activity log, delete state from disk."""
        if self._running:
            await self.stop()
        old_count = len(self._positions)
        self._positions.clear()
        self._activity_log.clear()
        # Remove persisted state
        state_dir = self._state_dir()
        for fname in ("positions.json", "config.json"):
            p = state_dir / fname
            if p.exists():
                p.unlink()
        self._log_activity("reset", f"Bot reset — cleared {old_count} positions")
        logger.info("DNA bot '%s' reset", self.config.bot_id)

    # ── OMS WebSocket connection ─────────────────────────────────

    @staticmethod
    def _fetch_json(url: str) -> dict | None:
        """Blocking HTTP GET returning parsed JSON dict."""
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as exc:
            logger.debug("DNA fetch failed (%s): %s", url, exc)
            return None

    async def _fetch_oms_config(self) -> None:
        """Fetch OMS /arb/config to cache fee thresholds."""
        url = f"{self.config.oms_url.rstrip('/')}/arb/config"
        try:
            data = await asyncio.get_event_loop().run_in_executor(
                None, self._fetch_json, url,
            )
            if data:
                self._oms_fee_config = data
                logger.info("DNA: cached OMS fee config: %s", data.get("min_profit_bps", {}))
        except Exception as exc:
            logger.warning("DNA: failed to fetch OMS config: %s", exc)

    async def _nado_signer_watchdog(self) -> None:
        """Periodically verify that the Nado linked signer matches the local trading key.

        If a mismatch is detected, the bot is auto-stopped to prevent order failures.
        Runs every 5 minutes while the bot is active.
        """
        _INTERVAL = 300  # 5 minutes
        try:
            while self._running:
                await asyncio.sleep(_INTERVAL)
                if not self._running:
                    break
                client = self._clients.get("nado")
                if not client or not hasattr(client, "verify_signer"):
                    continue
                try:
                    info = await asyncio.to_thread(client.verify_signer)
                except Exception as exc:
                    logger.warning("DNA: Nado signer check failed: %s", exc)
                    continue
                if info.get("ok"):
                    logger.debug("DNA: Nado signer OK (local=%s)", info.get("local", "?")[:10])
                    continue
                # Mismatch detected — auto-stop
                msg = (f"Nado signer mismatch detected — local={info.get('local', '?')} "
                       f"remote={info.get('remote', '?')} — auto-stopping bot")
                logger.error("DNA: %s", msg)
                self._log_activity("signer_mismatch", msg)
                await self.stop()
                break
        except asyncio.CancelledError:
            pass

    def _build_subscribe_filter(self) -> dict:
        """Build the subscribe_opportunities filter dict for OMS /ws/arb."""
        filt: dict[str, Any] = {"exchanges": self.config.exchanges}
        mode = self.config.spread_mode
        if mode == "custom":
            filt["min_profit_bps"] = self.config.custom_min_spread_bps
        elif mode == "half_neutral":
            if self._oms_fee_config:
                min_bps_map = self._oms_fee_config.get("min_profit_bps", {})
                if min_bps_map:
                    avg_threshold = sum(min_bps_map.values()) / len(min_bps_map)
                    filt["min_profit_bps"] = round(avg_threshold * 0.5, 2)
                else:
                    filt["min_profit_bps"] = 0
            else:
                filt["min_profit_bps"] = 0
        # delta_neutral: no min_profit_bps → OMS uses full fee threshold
        return filt

    async def _ws_loop(self) -> None:
        """Unified WebSocket loop: entry signals + exit monitoring via OMS /ws/arb."""
        import websockets

        # Fetch fee config once (needed for half_neutral subscribe filter)
        await self._fetch_oms_config()

        ws_url = self.config.oms_url.rstrip("/").replace("http://", "ws://").replace("https://", "wss://") + "/ws/arb"
        logger.info("DNA: connecting unified WS to %s", ws_url)

        backoff = 1.0
        while self._running:
            try:
                async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                    backoff = 1.0
                    self._log_activity("ws_connected", f"Connected to OMS WS/arb at {ws_url}")
                    logger.info("DNA: unified WS connected")

                    # Subscribe to opportunity stream
                    sub_filter = self._build_subscribe_filter()
                    await ws.send(json.dumps({
                        "action": "subscribe_opportunities",
                        **sub_filter,
                    }))
                    logger.info("DNA: subscribed to opportunities (filter=%s)", sub_filter)

                    # Register watches for all open positions (exit monitoring)
                    watched: set[tuple[str, str, str]] = set()
                    for pos in self._positions:
                        if pos.status == "open" and pos.exit_mode != "manual":
                            key = (pos.token, pos.buy_exchange, pos.sell_exchange)
                            if key not in watched:
                                await ws.send(json.dumps({
                                    "action": "watch",
                                    "token": pos.token,
                                    "buy_exchange": pos.buy_exchange,
                                    "sell_exchange": pos.sell_exchange,
                                }))
                                watched.add(key)

                    while self._running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            # Check for new positions that need watching
                            for pos in self._positions:
                                if pos.status == "open" and pos.exit_mode != "manual":
                                    key = (pos.token, pos.buy_exchange, pos.sell_exchange)
                                    if key not in watched:
                                        await ws.send(json.dumps({
                                            "action": "watch",
                                            "token": pos.token,
                                            "buy_exchange": pos.buy_exchange,
                                            "sell_exchange": pos.sell_exchange,
                                        }))
                                        watched.add(key)
                            continue

                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        msg_type = msg.get("type", "")

                        # ── Entry: opportunity signals from OMS ──
                        if msg_type == "arb_opportunity":
                            try:
                                await self._handle_signal(msg)
                            except Exception as exc:
                                logger.error("DNA signal handler error: %s", exc, exc_info=True)
                            continue

                        # ── Exit: spread updates for watched positions ──
                        if msg_type not in ("arb_status", "arb_close"):
                            continue

                        token = msg.get("token", "")
                        buy_exch = msg.get("buy_exchange", "")
                        sell_exch = msg.get("sell_exchange", "")
                        spread_bps = msg.get("spread_bps", 999.0)
                        now = time.time()

                        for pos in list(self._positions):
                            if pos.status != "open":
                                continue
                            if pos.exit_mode == "manual":
                                continue
                            if pos.token != token or pos.buy_exchange != buy_exch or pos.sell_exchange != sell_exch:
                                continue

                            hold_elapsed = now - pos.opened_at
                            if hold_elapsed < pos.exit_min_hold_s:
                                continue

                            if spread_bps <= pos.exit_threshold_bps:
                                logger.info(
                                    "DNA EXIT TRIGGER [%s] %s: spread=%.2f bps ≤ threshold=%.2f bps, held %.0fs",
                                    pos.position_id, pos.token, spread_bps, pos.exit_threshold_bps, hold_elapsed,
                                )
                                await self._close_position(pos, spread_bps, "arb_closed")

                                key = (token, buy_exch, sell_exch)
                                still_open = any(
                                    p.status == "open" and p.token == token
                                    and p.buy_exchange == buy_exch and p.sell_exchange == sell_exch
                                    for p in self._positions
                                )
                                if not still_open:
                                    await ws.send(json.dumps({
                                        "action": "unwatch",
                                        "token": token,
                                        "buy_exchange": buy_exch,
                                        "sell_exchange": sell_exch,
                                    }))
                                    watched.discard(key)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("DNA WS error: %s (reconnecting in %.0fs)", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    def _compute_exit_hold_s(self) -> float:
        """Compute min hold duration in seconds from current exit config."""
        mode = self.config.exit_mode
        if mode == "direct":
            return self.config.exit_min_hold_minutes * 60
        elif mode == "hours":
            return self.config.exit_min_hold_hours * 3600
        elif mode == "days":
            return self.config.exit_min_hold_days * 86400
        # manual → infinite
        return float("inf")

    def _compute_min_threshold(self, fee_threshold_bps: float) -> float:
        """Compute the effective minimum spread threshold based on spread_mode.

        Args:
            fee_threshold_bps: Full fee threshold for this exchange pair (from OMS).

        Returns:
            Minimum net_profit_bps required for this signal to be actionable.
        """
        mode = self.config.spread_mode
        if mode == "half_neutral":
            return fee_threshold_bps * 0.5
        elif mode == "custom":
            return self.config.custom_min_spread_bps
        # delta_neutral: full fee threshold
        return fee_threshold_bps

    async def _handle_signal(self, signal: dict) -> None:
        """Evaluate an arb signal and potentially open a position."""
        token = signal.get("token", "")
        buy_exchange = signal.get("buy_exchange", "")
        sell_exchange = signal.get("sell_exchange", "")
        buy_price = signal.get("buy_price_bbo", 0)
        sell_price = signal.get("sell_price_bbo", 0)
        net_profit_bps = signal.get("net_profit_bps", 0)
        fee_threshold_bps = signal.get("fee_threshold_bps", 0)
        max_qty = signal.get("max_qty", 0)
        buy_max_leverage = signal.get("buy_max_leverage", 1)
        sell_max_leverage = signal.get("sell_max_leverage", 1)
        buy_min_order_size = signal.get("buy_min_order_size", 0.0)
        sell_min_order_size = signal.get("sell_min_order_size", 0.0)
        buy_qty_step = signal.get("buy_qty_step", 0.0)
        sell_qty_step = signal.get("sell_qty_step", 0.0)

        # Skip if max positions reached
        open_positions = [p for p in self._positions if p.status == "open"]
        if len(open_positions) >= self.config.max_positions:
            return

        # Skip if already have a position in this token+direction
        for p in open_positions:
            if p.token == token and p.buy_exchange == buy_exchange and p.sell_exchange == sell_exchange:
                return  # already positioned

        # Skip excluded tokens
        if token.upper() in (t.upper() for t in self.config.excluded_tokens):
            return

        # Skip if exchanges not in our config
        if buy_exchange not in self.config.exchanges or sell_exchange not in self.config.exchanges:
            return

        # Spread mode filter: check against mode-specific threshold
        min_threshold = self._compute_min_threshold(fee_threshold_bps)
        if net_profit_bps < min_threshold:
            return

        # Legacy min_profit_bps override (if explicitly set > 0)
        if self.config.min_profit_bps > 0 and net_profit_bps < self.config.min_profit_bps:
            return

        # Check we have clients for both exchanges
        if buy_exchange not in self._clients or sell_exchange not in self._clients:
            logger.warning("DNA: missing client for %s or %s", buy_exchange, sell_exchange)
            return

        # Calculate quantity: target notional / mid price
        if buy_price <= 0 or sell_price <= 0:
            return
        mid_price = (buy_price + sell_price) / 2.0
        target_qty = self.config.position_size_usd / mid_price

        # Cap at OMS-reported max executable quantity
        if max_qty > 0:
            target_qty = min(target_qty, max_qty)

        if target_qty <= 0:
            return

        logger.info(
            "DNA: opening position %s — BUY %s on %s @ %.4f, SELL on %s @ %.4f, qty=%.6f, spread=%.1f bps (mode=%s, threshold=%.1f bps)",
            token, token, buy_exchange, buy_price, sell_exchange, sell_price, target_qty, net_profit_bps,
            self.config.spread_mode, min_threshold,
        )

        self._log_activity("signal", f"{token}: BUY {buy_exchange} @ {buy_price:.4f}, "
                           f"SELL {sell_exchange} @ {sell_price:.4f}, spread={net_profit_bps:.1f}bps "
                           f"(mode={self.config.spread_mode}, min={min_threshold:.1f}bps)")

        # Execute both legs simultaneously
        await self._open_position(
            token=token,
            buy_exchange=buy_exchange,
            buy_symbol=signal.get("buy_symbol", ""),
            sell_exchange=sell_exchange,
            sell_symbol=signal.get("sell_symbol", ""),
            quantity=target_qty,
            buy_price=buy_price,
            sell_price=sell_price,
            net_profit_bps=net_profit_bps,
            buy_max_leverage=buy_max_leverage,
            sell_max_leverage=sell_max_leverage,
            buy_min_order_size=buy_min_order_size,
            sell_min_order_size=sell_min_order_size,
            buy_qty_step=buy_qty_step,
            sell_qty_step=sell_qty_step,
        )

    # ── Position execution ────────────────────────────────────────

    @staticmethod
    def _get_qty_step(client: Any, symbol: str) -> Decimal:
        """Get the quantity step size from any exchange client."""
        if hasattr(client, 'get_qty_step'):        # Extended
            return client.get_qty_step(symbol)
        if hasattr(client, 'get_min_order_size'):   # GRVT, Nado
            step = client.get_min_order_size(symbol)
            if step and step > 0:
                return step
        return Decimal("1")

    @staticmethod
    def _get_min_order_size(client: Any, symbol: str) -> Decimal:
        """Get the minimum order size for a symbol from any exchange client."""
        if hasattr(client, 'get_min_order_size'):
            v = client.get_min_order_size(symbol)
            if v and v > 0:
                return v
        if hasattr(client, 'get_qty_step'):
            v = client.get_qty_step(symbol)
            if v and v > 0:
                return v
        return Decimal("0")

    def _harmonize_qty(self, buy_client: Any, buy_symbol: str,
                       sell_client: Any, sell_symbol: str,
                       qty: Decimal,
                       oms_buy_step: float = 0.0,
                       oms_sell_step: float = 0.0) -> Decimal:
        """Round qty down to a value valid for BOTH exchanges."""
        step_buy = Decimal(str(oms_buy_step)) if oms_buy_step > 0 else self._get_qty_step(buy_client, buy_symbol)
        step_sell = Decimal(str(oms_sell_step)) if oms_sell_step > 0 else self._get_qty_step(sell_client, sell_symbol)
        # Use the larger step to guarantee both sides accept the qty
        step = max(step_buy, step_sell)
        harmonized = (qty / step).to_integral_value(rounding="ROUND_DOWN") * step
        if harmonized != qty:
            logger.info("DNA: harmonized qty %.6f → %.6f (step_buy=%s, step_sell=%s, used=%s)",
                        qty, harmonized, step_buy, step_sell, step)
        return harmonized

    async def _open_position(
        self,
        token: str,
        buy_exchange: str,
        buy_symbol: str,
        sell_exchange: str,
        sell_symbol: str,
        quantity: float,
        buy_price: float,
        sell_price: float,
        net_profit_bps: float,
        buy_max_leverage: int = 1,
        sell_max_leverage: int = 1,
        buy_min_order_size: float = 0.0,
        sell_min_order_size: float = 0.0,
        buy_qty_step: float = 0.0,
        sell_qty_step: float = 0.0,
    ) -> None:
        """Execute both legs quasi-simultaneously and record the position."""
        position_id = str(uuid.uuid4())[:8]
        buy_client = self._clients[buy_exchange]
        sell_client = self._clients[sell_exchange]
        qty_decimal = self._harmonize_qty(
            buy_client, buy_symbol, sell_client, sell_symbol, Decimal(str(quantity)),
            oms_buy_step=buy_qty_step, oms_sell_step=sell_qty_step,
        )
        if qty_decimal <= 0:
            logger.warning("DNA %s: harmonized qty is 0 — skipping", token)
            return

        # Pre-flight: ensure qty meets minimum order size on BOTH exchanges
        buy_min = Decimal(str(buy_min_order_size)) if buy_min_order_size > 0 else self._get_min_order_size(buy_client, buy_symbol)
        sell_min = Decimal(str(sell_min_order_size)) if sell_min_order_size > 0 else self._get_min_order_size(sell_client, sell_symbol)
        effective_min = max(buy_min, sell_min)
        if effective_min > 0 and qty_decimal < effective_min:
            logger.warning(
                "DNA %s: qty %.6f below min_order_size (buy=%s/%s, sell=%s/%s) — skipping",
                token, qty_decimal, buy_exchange, buy_min, sell_exchange, sell_min,
            )
            self._log_activity("qty_too_small",
                               f"{token}: qty={qty_decimal:.6f} below min "
                               f"(buy {buy_exchange}={buy_min}, sell {sell_exchange}={sell_min}) — skipped")
            return

        # Ensure max leverage is set (only fires once per symbol, ~0ms after first call)
        await asyncio.gather(
            self._ensure_leverage(buy_client, buy_exchange, buy_symbol, buy_max_leverage),
            self._ensure_leverage(sell_client, sell_exchange, sell_symbol, sell_max_leverage),
        )

        t_start = time.time()

        if self.config.simulation:
            # Simulated execution
            buy_result = DNALegResult(
                success=True, exchange=buy_exchange, symbol=buy_symbol,
                side="buy", quantity=quantity, fill_price=buy_price,
                order_id=f"sim-{position_id}-buy",
            )
            sell_result = DNALegResult(
                success=True, exchange=sell_exchange, symbol=sell_symbol,
                side="sell", quantity=quantity, fill_price=sell_price,
                order_id=f"sim-{position_id}-sell",
            )
        else:
            # Real execution: both legs simultaneously via market orders
            buy_result, sell_result = await asyncio.gather(
                self._execute_leg(buy_client, buy_symbol, "buy", qty_decimal),
                self._execute_leg(sell_client, sell_symbol, "sell", qty_decimal),
            )

        elapsed_ms = (time.time() - t_start) * 1000

        # Evaluate results
        if buy_result.success and sell_result.success:
            # Both legs filled — check quantities match within tolerance
            buy_qty = buy_result.quantity
            sell_qty = sell_result.quantity
            qty_diff_pct = abs(buy_qty - sell_qty) / max(buy_qty, sell_qty, 1e-9) * 100

            if qty_diff_pct > self.config.size_tolerance_pct:
                logger.warning(
                    "DNA %s: qty mismatch %.2f%% (buy=%.6f, sell=%.6f) — exceeds tolerance %.1f%%",
                    position_id, qty_diff_pct, buy_qty, sell_qty, self.config.size_tolerance_pct,
                )
                # Use the smaller quantity as effective position size
                effective_qty = min(buy_qty, sell_qty)
            else:
                effective_qty = min(buy_qty, sell_qty)

            actual_buy = buy_result.fill_price or buy_price
            actual_sell = sell_result.fill_price or sell_price
            mid = (actual_buy + actual_sell) / 2 if actual_buy and actual_sell else (buy_price + sell_price) / 2
            actual_spread_bps = ((actual_sell - actual_buy) / actual_buy * 10000) if actual_buy > 0 else net_profit_bps

            position = DNAPosition(
                position_id=position_id,
                token=token,
                buy_exchange=buy_exchange,
                buy_symbol=buy_symbol,
                sell_exchange=sell_exchange,
                sell_symbol=sell_symbol,
                quantity=effective_qty,
                buy_fill_price=actual_buy,
                sell_fill_price=actual_sell,
                buy_fill_qty=buy_qty,
                sell_fill_qty=sell_qty,
                entry_spread_bps=actual_spread_bps,
                notional_usd=effective_qty * mid,
                opened_at=time.time(),
                exit_mode=self.config.exit_mode,
                exit_min_hold_s=self._compute_exit_hold_s(),
                exit_threshold_bps=self.config.exit_threshold_bps,
                simulation=self.config.simulation,
            )
            self._positions.append(position)
            self._save_state()

            logger.info(
                "DNA POSITION OPENED [%s] %s: BUY %s@%.4f, SELL %s@%.4f, qty=%.6f, notional=$%.2f (%.0fms)",
                position_id, token, buy_exchange, position.buy_fill_price,
                sell_exchange, position.sell_fill_price, effective_qty,
                position.notional_usd, elapsed_ms,
            )
            self._log_activity("position_opened",
                               f"[{position_id}] {token}: BUY {buy_exchange}@{position.buy_fill_price:.4f}, "
                               f"SELL {sell_exchange}@{position.sell_fill_price:.4f}, "
                               f"qty={effective_qty:.6f}, notional=${position.notional_usd:.2f}")

            # Post-fill: verify actual exchange positions match
            await self._verify_position_balance(
                position_id, token,
                buy_exchange, buy_symbol,
                sell_exchange, sell_symbol,
                effective_qty,
            )

        elif not buy_result.success and not sell_result.success:
            # Both failed — no unwind needed
            logger.error(
                "DNA %s: BOTH LEGS FAILED (%.0fms) — buy: %s, sell: %s",
                position_id, elapsed_ms, buy_result.error, sell_result.error,
            )
            self._log_activity("entry_failed",
                               f"[{position_id}] {token}: BOTH legs failed — "
                               f"buy: {buy_result.error}, sell: {sell_result.error}")

        else:
            # One leg failed — need to unwind the successful leg
            success_leg = buy_result if buy_result.success else sell_result
            failed_leg = sell_result if buy_result.success else buy_result

            logger.error(
                "DNA %s: ONE LEG FAILED (%.0fms) — %s %s OK (qty=%.6f), %s %s FAIL: %s — UNWINDING",
                position_id, elapsed_ms,
                success_leg.side, success_leg.exchange, success_leg.quantity,
                failed_leg.side, failed_leg.exchange, failed_leg.error,
            )

            # Unwind: reverse the successful leg via market order
            unwind_side = "sell" if success_leg.side == "buy" else "buy"
            unwind_client = self._clients[success_leg.exchange]

            unwind = await self._execute_leg(
                unwind_client, success_leg.symbol, unwind_side,
                Decimal(str(success_leg.quantity)),
            )

            if unwind.success:
                logger.info("DNA %s: unwind successful (%.6f %s on %s)",
                            position_id, unwind.quantity, unwind_side, success_leg.exchange)
            else:
                logger.error("DNA %s: UNWIND FAILED — %s. MANUAL INTERVENTION NEEDED!",
                             position_id, unwind.error)

            self._log_activity("entry_partial_unwind",
                               f"[{position_id}] {token}: {failed_leg.side} on {failed_leg.exchange} failed: "
                               f"{failed_leg.error} — unwound {success_leg.side} on {success_leg.exchange}")

    async def _ensure_leverage(self, client: Any, exchange: str, symbol: str, max_lev: int) -> None:
        """Set max leverage for a symbol on an exchange (only once per symbol)."""
        key = (exchange, symbol)
        if key in self._leverage_applied or max_lev <= 1:
            return
        try:
            if hasattr(client, 'async_set_leverage'):
                await client.async_set_leverage(symbol, max_lev)
                logger.info("DNA: leverage set %s %s → %dx", exchange, symbol, max_lev)
            else:
                logger.debug("DNA: no async_set_leverage on %s — skipping", exchange)
        except Exception as exc:
            logger.warning("DNA: failed to set leverage %s %s → %dx: %s", exchange, symbol, max_lev, exc)
        self._leverage_applied.add(key)

    async def _execute_leg(
        self, client: Any, symbol: str, side: str, quantity: Decimal,
    ) -> DNALegResult:
        """Execute a market order on an exchange and poll for fill."""
        try:
            # Use synchronous create_market_order wrapped in thread
            resp = await asyncio.to_thread(
                client.create_market_order,
                symbol=symbol, side=side, amount=quantity,
            )

            # Extract order id — different clients use different keys
            # GRVT: metadata.client_order_id; Extended: id / external_id
            metadata = resp.get("metadata", {}) or {}
            state = resp.get("state", {}) or {}
            order_id = (
                metadata.get("client_order_id")
                or resp.get("id")
                or resp.get("external_id")
                or state.get("order_id")
            )
            # GRVT returns fill info in state.traded_size (may be list like ["320","USDT"])
            traded_raw = state.get("traded_size") or resp.get("traded_qty") or 0
            traded_qty = float(traded_raw[0]) if isinstance(traded_raw, list) else float(traded_raw)
            # GRVT avg fill price may also be list like ["0.1499","USDT"]
            fp_raw = (
                state.get("avg_fill_price")
                or resp.get("fill_price")
                or resp.get("price")
                or resp.get("avg_price")
                or resp.get("limit_price")
                or 0
            )
            fill_price = float(fp_raw[0]) if isinstance(fp_raw, list) else float(fp_raw)
            status = state.get("status") or resp.get("status", "")

            # NADO IOC: traded_qty is now verified by the client.
            # If still 0, use fill_price hint but let poll loop verify actual fill.
            if traded_qty <= 0 and resp.get("status") == "success" and resp.get("digest"):
                if fill_price <= 0:
                    fill_price = float(resp.get("limit_price", 0))
            reject = state.get("reject_reason", "")

            logger.info(
                "DNA leg %s %s %s: order_id=%s status=%s traded=%.6f reject=%s",
                client.name, side, symbol, order_id, status, traded_qty, reject,
            )

            # Poll for fill confirmation (Extended returns id only;
            # fill arrives asynchronously).  Retry up to ~4s.
            if traded_qty <= 0 and order_id:
                for delay in (0.5, 0.8, 1.0, 1.2):
                    await asyncio.sleep(delay)
                    try:
                        info = await client.async_check_order_fill(
                            str(order_id) if not isinstance(order_id, str) else order_id
                        )
                        traded_qty = float(info.get("traded_qty", 0.0))
                        fp = info.get("avg_price") or info.get("price")
                        if fp is not None:
                            fill_price = float(fp)
                        status = info.get("status", status)
                        if traded_qty > 0 or info.get("filled"):
                            break
                    except Exception as exc:
                        logger.debug("DNA fill poll %s/%s: %s", client.name, order_id, exc)

            if traded_qty > 0:
                return DNALegResult(
                    success=True, exchange=client.name, symbol=symbol,
                    side=side, quantity=traded_qty,
                    fill_price=fill_price,
                    order_id=str(order_id) if order_id else None,
                )
            else:
                return DNALegResult(
                    success=False, exchange=client.name, symbol=symbol,
                    side=side, quantity=0,
                    error=f"No fill after poll (status={status}, order_id={order_id})",
                )

        except Exception as exc:
            return DNALegResult(
                success=False, exchange=client.name if hasattr(client, 'name') else "?",
                symbol=symbol, side=side, quantity=0,
                error=str(exc),
            )

    async def _verify_position_balance(
        self, position_id: str, token: str,
        buy_exchange: str, buy_symbol: str,
        sell_exchange: str, sell_symbol: str,
        expected_qty: float,
    ) -> None:
        """Verify actual exchange positions match after a trade.

        Queries both exchanges for the specific symbol, compares sizes,
        and fires a corrective IOC order if there's an imbalance.
        """
        buy_client = self._clients.get(buy_exchange)
        sell_client = self._clients.get(sell_exchange)
        if not buy_client or not sell_client:
            return

        try:
            await asyncio.sleep(1.0)  # allow settlement
            buy_positions, sell_positions = await asyncio.gather(
                asyncio.to_thread(buy_client.fetch_positions, [buy_symbol]),
                asyncio.to_thread(sell_client.fetch_positions, [sell_symbol]),
            )

            buy_pos = next((p for p in buy_positions if p.get("instrument") == buy_symbol), None)
            sell_pos = next((p for p in sell_positions if p.get("instrument") == sell_symbol), None)

            buy_size = abs(float(buy_pos["size"])) if buy_pos else 0.0
            sell_size = abs(float(sell_pos["size"])) if sell_pos else 0.0

            if buy_size == 0.0 and sell_size == 0.0:
                return  # both flat — position was closed or never opened

            diff = abs(buy_size - sell_size)
            max_size = max(buy_size, sell_size, 1e-9)
            diff_pct = diff / max_size * 100

            if diff_pct <= self.config.size_tolerance_pct:
                logger.info(
                    "DNA %s %s: position balance OK — %s=%s %.6f, %s=%s %.6f (diff=%.2f%%)",
                    position_id, token, buy_exchange, buy_symbol, buy_size,
                    sell_exchange, sell_symbol, sell_size, diff_pct,
                )
                return

            logger.warning(
                "DNA %s %s: POSITION IMBALANCE — %s=%s %.6f, %s=%s %.6f (diff=%.6f, %.2f%%)",
                position_id, token, buy_exchange, buy_symbol, buy_size,
                sell_exchange, sell_symbol, sell_size, diff, diff_pct,
            )
            self._log_activity("position_imbalance",
                               f"[{position_id}] {token}: {buy_exchange}={buy_size:.6f}, "
                               f"{sell_exchange}={sell_size:.6f} (diff={diff:.6f}, {diff_pct:.2f}%)")

            # Fire corrective order on the side with smaller position
            if buy_size < sell_size:
                # Need more on buy side
                correction_qty = Decimal(str(sell_size - buy_size))
                correction_client = buy_client
                correction_symbol = buy_symbol
                correction_side = "buy"
                correction_exchange = buy_exchange
            else:
                # Need more on sell side
                correction_qty = Decimal(str(buy_size - sell_size))
                correction_client = sell_client
                correction_symbol = sell_symbol
                correction_side = "sell"
                correction_exchange = sell_exchange

            logger.info(
                "DNA %s %s: firing corrective %s %s %.6f on %s",
                position_id, token, correction_side, correction_symbol,
                correction_qty, correction_exchange,
            )
            corrective = await self._execute_leg(
                correction_client, correction_symbol, correction_side, correction_qty,
            )
            if corrective.success:
                logger.info(
                    "DNA %s %s: corrective fill OK — %s %.6f on %s",
                    position_id, token, correction_side, corrective.quantity, correction_exchange,
                )
                self._log_activity("position_corrected",
                                   f"[{position_id}] {token}: corrective {correction_side} "
                                   f"{corrective.quantity:.6f} on {correction_exchange}")
            else:
                logger.error(
                    "DNA %s %s: CORRECTIVE FAILED — %s %s on %s: %s",
                    position_id, token, correction_side, correction_symbol,
                    correction_exchange, corrective.error,
                )
                self._log_activity("corrective_failed",
                                   f"[{position_id}] {token}: corrective {correction_side} on "
                                   f"{correction_exchange} FAILED: {corrective.error}")

        except Exception as exc:
            logger.warning("DNA %s %s: position balance check error: %s", position_id, token, exc)

    async def _close_position(self, pos: DNAPosition, spread_bps: float, reason: str) -> bool:
        """Close a position by executing reverse market orders on both sides.

        Returns True if both legs closed successfully.
        """
        pos.status = "closing"
        self._save_state()
        self._log_activity("position_closing",
                           f"[{pos.position_id}] {pos.token}: closing (reason={reason}, spread={spread_bps:.2f}bps)")

        # Reverse: SELL on buy_exchange, BUY on sell_exchange
        sell_client = self._clients.get(pos.buy_exchange)
        buy_client = self._clients.get(pos.sell_exchange)

        # Use per-leg fill quantities (fall back to pos.quantity for old positions)
        # Round each to the exchange's step size to avoid rejection
        sell_qty_raw = Decimal(str(pos.buy_fill_qty or pos.quantity))
        buy_qty_raw = Decimal(str(pos.sell_fill_qty or pos.quantity))
        if sell_client:
            step = self._get_qty_step(sell_client, pos.buy_symbol)
            sell_qty_decimal = (sell_qty_raw / step).to_integral_value(rounding="ROUND_DOWN") * step
        else:
            sell_qty_decimal = sell_qty_raw
        if buy_client:
            step = self._get_qty_step(buy_client, pos.sell_symbol)
            buy_qty_decimal = (buy_qty_raw / step).to_integral_value(rounding="ROUND_DOWN") * step
        else:
            buy_qty_decimal = buy_qty_raw

        if not sell_client or not buy_client:
            logger.error("DNA CLOSE [%s]: missing client for %s or %s", pos.position_id, pos.buy_exchange, pos.sell_exchange)
            pos.status = "open"  # revert, retry later
            self._save_state()
            return False

        t_start = time.time()

        use_sim = pos.simulation
        logger.info("DNA CLOSE [%s] %s: mode=%s (pos.simulation=%s, config.simulation=%s)",
                    pos.position_id, pos.token, "SIMULATION" if use_sim else "LIVE",
                    pos.simulation, self.config.simulation)

        if use_sim:
            sell_result = DNALegResult(
                success=True, exchange=pos.buy_exchange, symbol=pos.buy_symbol,
                side="sell", quantity=pos.quantity, fill_price=pos.buy_fill_price,
                order_id=f"sim-close-{pos.position_id}-sell",
            )
            buy_result = DNALegResult(
                success=True, exchange=pos.sell_exchange, symbol=pos.sell_symbol,
                side="buy", quantity=pos.quantity, fill_price=pos.sell_fill_price,
                order_id=f"sim-close-{pos.position_id}-buy",
            )
        else:
            sell_result, buy_result = await asyncio.gather(
                self._execute_leg(sell_client, pos.buy_symbol, "sell", sell_qty_decimal),
                self._execute_leg(buy_client, pos.sell_symbol, "buy", buy_qty_decimal),
            )

        elapsed_ms = (time.time() - t_start) * 1000

        if sell_result.success and buy_result.success:
            pos.status = "closed"
            pos.closed_at = time.time()
            pos.close_spread_bps = spread_bps
            pos.close_reason = reason
            pos.close_buy_fill_price = buy_result.fill_price or 0.0
            pos.close_sell_fill_price = sell_result.fill_price or 0.0
            self._save_state()

            logger.info(
                "DNA POSITION CLOSED [%s] %s: SELL %s@%.4f, BUY %s@%.4f, reason=%s (%.0fms)",
                pos.position_id, pos.token, pos.buy_exchange,
                sell_result.fill_price or 0, pos.sell_exchange,
                buy_result.fill_price or 0, reason, elapsed_ms,
            )
            self._log_activity("position_closed",
                               f"[{pos.position_id}] {pos.token}: closed ({reason}), "
                               f"spread={spread_bps:.2f}bps, held {(pos.closed_at - pos.opened_at):.0f}s")

            # Post-close: verify both sides are flat / balanced
            await self._verify_position_balance(
                pos.position_id, pos.token,
                pos.buy_exchange, pos.buy_symbol,
                pos.sell_exchange, pos.sell_symbol,
                0.0,
            )
            return True
        else:
            # One or both legs failed — revert to open for retry
            failed_legs = []
            if not sell_result.success:
                failed_legs.append(f"SELL {pos.buy_exchange}: {sell_result.error}")
            if not buy_result.success:
                failed_legs.append(f"BUY {pos.sell_exchange}: {buy_result.error}")

            pos.status = "open"
            self._save_state()

            logger.error(
                "DNA CLOSE FAILED [%s] %s (%.0fms): %s — will retry",
                pos.position_id, pos.token, elapsed_ms, "; ".join(failed_legs),
            )
            self._log_activity("position_close_failed",
                               f"[{pos.position_id}] {pos.token}: close failed — {'; '.join(failed_legs)}")
            return False

    async def close_position(self, position_id: str, reason: str = "manual") -> dict:
        """Public method: manually close a specific position."""
        pos = next((p for p in self._positions if p.position_id == position_id and p.status == "open"), None)
        if not pos:
            return {"error": f"No open position with id '{position_id}'"}

        # For manual close, use current spread as 0 (unknown)
        success = await self._close_position(pos, spread_bps=0.0, reason=reason)
        return {"status": "closed" if success else "close_failed", "position_id": position_id}

    def delete_position(self, position_id: str) -> dict:
        """Delete a closed position from history."""
        pos = next((p for p in self._positions if p.position_id == position_id), None)
        if not pos:
            return {"error": f"No position with id '{position_id}'"}
        if pos.status != "closed":
            return {"error": f"Position '{position_id}' is not closed (status={pos.status})"}
        self._positions.remove(pos)
        self._save_state()
        logger.info("DNA: deleted position %s (%s) from history", position_id, pos.token)
        return {"status": "deleted", "position_id": position_id}

    # ── State persistence ─────────────────────────────────────────

    def _state_dir(self) -> Path:
        d = _DNA_DIR / self.config.bot_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _save_state(self) -> None:
        """Persist positions to disk."""
        path = self._state_dir() / "positions.json"
        data = [asdict(p) for p in self._positions]
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)

    def _load_state(self) -> None:
        """Load positions from disk."""
        path = self._state_dir() / "positions.json"
        if not path.exists():
            return
        try:
            with open(path) as fh:
                data = json.load(fh)
            self._positions = [DNAPosition(**d) for d in data]
            logger.info("DNA '%s': restored %d positions from disk", self.config.bot_id, len(self._positions))
        except Exception as exc:
            logger.warning("DNA '%s': failed to load state: %s", self.config.bot_id, exc)

    def _load_config(self) -> None:
        """Load persisted config from disk (merge into current config)."""
        path = self._state_dir() / "config.json"
        if not path.exists():
            return
        try:
            with open(path) as fh:
                data = json.load(fh)
            for key, val in data.items():
                if hasattr(self.config, key):
                    setattr(self.config, key, val)
            logger.info("DNA '%s': restored config from disk (simulation=%s, exit_mode=%s)",
                        self.config.bot_id, self.config.simulation, self.config.exit_mode)
        except Exception as exc:
            logger.warning("DNA '%s': failed to load config: %s", self.config.bot_id, exc)

    def _save_config(self) -> None:
        """Persist config to disk."""
        path = self._state_dir() / "config.json"
        data = asdict(self.config)
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2)

    # ── Status / API ──────────────────────────────────────────────

    def get_status(self) -> dict:
        """Return current bot status."""
        open_pos = [p for p in self._positions if p.status == "open"]
        return {
            "bot_id": self.config.bot_id,
            "running": self._running,
            "config": {
                "position_size_usd": self.config.position_size_usd,
                "max_positions": self.config.max_positions,
                "spread_mode": self.config.spread_mode,
                "custom_min_spread_bps": self.config.custom_min_spread_bps,
                "exchanges": self.config.exchanges,
                "simulation": self.config.simulation,
                "exit_mode": self.config.exit_mode,
                "exit_min_hold_minutes": self.config.exit_min_hold_minutes,
                "exit_min_hold_hours": self.config.exit_min_hold_hours,
                "exit_min_hold_days": self.config.exit_min_hold_days,
                "exit_threshold_bps": self.config.exit_threshold_bps,
                "excluded_tokens": self.config.excluded_tokens,
                "auto_exclude_open_positions": self.config.auto_exclude_open_positions,
            },
            "positions": {
                "open": len(open_pos),
                "max": self.config.max_positions,
                "total_notional_usd": sum(p.notional_usd for p in open_pos),
                "details": [asdict(p) for p in open_pos],
            },
            "all_positions": [asdict(p) for p in self._positions],
            "trade_history": [
                asdict(p) for p in sorted(
                    (p for p in self._positions if p.status == "closed"),
                    key=lambda p: p.closed_at or 0, reverse=True,
                )
            ],
            "activity_log": self._activity_log[-50:],
        }

    def _log_activity(self, event: str, message: str) -> None:
        """Add an entry to the activity log."""
        entry = {
            "timestamp": time.time(),
            "event": event,
            "message": message,
        }
        self._activity_log.append(entry)
        # Keep last 500 entries
        if len(self._activity_log) > 500:
            self._activity_log = self._activity_log[-500:]
        # Forward to Cloudflare Analytics Engine
        if self._activity_forwarder:
            self._activity_forwarder.forward(event, message, "dna", self.config.bot_id)
