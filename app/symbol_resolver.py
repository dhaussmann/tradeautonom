"""Cross-exchange symbol resolution helpers.

Phase F.4 / M9 — Variational symbol auto-correction.

When a user creates a bot with a Variational symbol like ``P-DOGE-USDC-3600`` and
Variational later changes the funding interval (e.g. moves DOGE from 1h to 8h, so
the live symbol becomes ``P-DOGE-USDC-28800``), the bot's stored config becomes
stale and the orderbook feed reports "no feed for variational:P-DOGE-USDC-3600".

This module provides ``resolve_variational_symbol`` which detects that situation
on bot start and returns the current authoritative symbol for the same underlying
token, so the engine can self-heal without operator intervention.

Resolution order:
  1. OMS-v2 ``/tracked`` endpoint (authoritative — auto-discovery polls Variational
     every 1.2s and tracks the live symbols).
  2. Direct Variational client ``async_fetch_markets`` fallback (when no OMS-v2
     URL is configured — V1 deployments).

The helper never silently corrects symbols outside the Variational ``P-...-USDC-...``
namespace, and never coerces to a different token (only different funding interval).
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("tradeautonom.symbol_resolver")

# Variational perp symbol format: P-<TOKEN>-USDC-<funding_interval_seconds>
# Tokens may contain digits and hyphens (e.g. "1000PEPE", "1MBABYDOGE"), but the
# trailing ``-USDC-<digits>`` is unambiguous, so we anchor on that.
_VARIATIONAL_SYMBOL_RE = re.compile(r"^P-(?P<token>.+)-USDC-(?P<interval>\d+)$")


def parse_variational_symbol(symbol: str) -> tuple[str, int] | None:
    """Return ``(token, funding_interval_s)`` or ``None`` if not a Variational perp."""
    if not symbol:
        return None
    m = _VARIATIONAL_SYMBOL_RE.match(symbol)
    if not m:
        return None
    return m.group("token"), int(m.group("interval"))


async def _resolve_via_oms(
    requested_symbol: str,
    token: str,
    oms_url: str,
) -> str | None:
    """Query OMS-v2 ``/tracked`` and return the live Variational symbol for the token.

    Returns ``None`` if OMS is unreachable, the token is unknown, or there is no
    ``variational`` entry for the token.

    Uses ``urllib.request`` in a worker thread (matching ``DataLayer._run_ob_oms_poll``
    in app/data_layer.py) — httpx has shown reliability issues from inside CF
    Containers when calling other CF Workers, while urllib works.
    """
    import asyncio
    import json
    import urllib.request

    url = f"{oms_url.rstrip('/')}/tracked"

    def _fetch() -> dict | None:
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                if resp.status != 200:
                    logger.warning(
                        "OMS /tracked returned HTTP %d for %s lookup",
                        resp.status,
                        token,
                    )
                    return None
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            logger.warning("OMS /tracked unreachable for %s: %s", token, exc)
            return None

    try:
        data = await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warning("OMS /tracked thread error for %s: %s", token, exc)
        return None
    if data is None:
        return None

    # /tracked shape: { "<TOKEN>": { "<exchange>": "<symbol>", ... }, ... }
    entry = data.get(token) or data.get(token.upper())
    if not isinstance(entry, dict):
        logger.info(
            "Variational symbol resolver: token %s not in OMS /tracked (requested %s)",
            token,
            requested_symbol,
        )
        return None
    live_symbol = entry.get("variational")
    if not isinstance(live_symbol, str) or not live_symbol:
        logger.info(
            "Variational symbol resolver: token %s has no variational entry in OMS /tracked",
            token,
        )
        return None
    return live_symbol


async def _resolve_via_variational_client(
    token: str,
    variational_client: Any,
) -> str | None:
    """Fallback: ask the Variational client directly via /metadata/stats."""
    try:
        markets = await variational_client.async_fetch_markets()
    except Exception as exc:
        logger.warning("Variational async_fetch_markets failed for %s: %s", token, exc)
        return None
    token_upper = token.upper()
    for m in markets or []:
        if str(m.get("underlying", "")).upper() == token_upper:
            sym = m.get("symbol")
            if isinstance(sym, str) and sym:
                return sym
    return None


async def resolve_variational_symbol(
    requested_symbol: str,
    *,
    oms_url: str | None,
    variational_client: Any | None,
) -> tuple[str, bool, str | None]:
    """Resolve a Variational symbol against the live Variational namespace.

    Args:
        requested_symbol: symbol from bot config (e.g. ``"P-DOGE-USDC-3600"``).
        oms_url: OMS-v2 base URL (``settings.fn_opt_shared_monitor_url``) or
            empty/None to skip OMS lookup.
        variational_client: a ``VariationalClient`` instance (or ``None``) used as
            fallback when OMS is unavailable.

    Returns:
        ``(resolved_symbol, was_corrected, source)``
          - ``resolved_symbol``: symbol the engine should actually use.
          - ``was_corrected``: True if the resolved symbol differs from requested.
          - ``source``: ``"oms"``, ``"variational"``, or ``None`` (no lookup performed).

        If ``requested_symbol`` is not a Variational perp at all, returns it
        unchanged with ``was_corrected=False, source=None``.

        If neither OMS nor the Variational client is usable, returns the
        requested symbol unchanged with ``was_corrected=False, source=None``
        (best-effort: never block bot start because of a lookup failure).
    """
    parsed = parse_variational_symbol(requested_symbol)
    if parsed is None:
        return requested_symbol, False, None
    token, _interval = parsed

    # Try OMS-v2 first (faster; uses cached auto-discovery state).
    if oms_url:
        live = await _resolve_via_oms(requested_symbol, token, oms_url)
        if live:
            return live, (live != requested_symbol), "oms"

    # Fallback: ask Variational directly.
    if variational_client is not None:
        live = await _resolve_via_variational_client(token, variational_client)
        if live:
            return live, (live != requested_symbol), "variational"

    # No source could resolve the symbol — leave as-is and let downstream code
    # surface the feed error in the usual way.
    return requested_symbol, False, None
