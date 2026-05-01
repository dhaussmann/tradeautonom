# Release Notes

---

## [Unreleased] — 2026-04-29

### Bug Fixes

#### SSE bot-stream silently dropped exit_min/max_spread_pct
**Symptom:** Editing `exit_min_spread_pct` / `exit_max_spread_pct` in `BotDetailView` had no visible effect. Apply-button persisted the value to the engine + disk, but the UI pill snapped back to the default after the next SSE message (~2s later). User concluded "Apply doesn't work".

**Root cause:** `app/server.py::fn_bot_stream` rebuilt the status payload manually from `engine.config.*` and **omitted both `exit_*_spread_pct` fields entirely**. Other fields like `fn_opt_max_per_pair_ratio` were also missing. The initial REST snapshot via `GET /fn/bots/{id}/status` (which calls `engine.get_status()`) had all fields correctly — so the UI showed the right value at first, then the SSE overwrite cleared them and Vue's `?? -0.5` fallback in `BotDetailView.vue:190-191` kicked in.

**Fix (`app/server.py`):** SSE now uses `engine.get_status()` as the single source of truth, identical to the REST snapshot. Eliminates the entire drift-risk class:
```python
status = engine.get_status()
data = {**status, "bot_id": bot_id, "ts": now}
```

**Affected only V2 (Cloudflare Container `user-v2`).** NAS containers are deprecated and out of scope.

### Features

#### DNA bot: live pre-trade cross-quote guard + entry telemetry
The DNA entry path now validates opportunities against a fresh OMS `/quote/cross`
snapshot before placing orders.

- Added pre-trade guards in `app/dna_bot.py`:
  - stale signal reject (`max_signal_age_ms`),
  - infeasible/stale cross-quote reject (`max_quote_age_ms`),
  - signal-to-live erosion reject (`max_signal_erosion_pct`),
  - unprofitable-after-fees reject (`net_profit_bps_after_fees <= 0`).
- Added optional fail-closed mode when cross-quote is unavailable:
  - `dna_require_cross_quote=true` skips entries,
  - default `false` keeps fail-open legacy behavior.
- Added live quantity re-harmonization from cross-quote (`harmonized_qty`) before execution.
- Added structured activity telemetry event `dna_entry_telemetry` with signal/quote/realized spread fields and slippage diagnostics.

**New DNA settings (`app/config.py`, wired in `app/server.py`):**
- `dna_max_signal_age_ms` (default `1500`)
- `dna_max_quote_age_ms` (default `2000`)
- `dna_max_signal_erosion_pct` (default `40.0`)
- `dna_require_cross_quote` (default `false`)

#### Bot creation: maker-asymmetry hint + smart spread defaults
Step 4 of `BotCreateModal` now adapts to the maker-leg choice.

- **Asymmetry hint:** dezenter Hinweistext direkt unter dem Spread-Window.
  - Maker = Long → "max_spread_pct ist der wichtigere Hebel — empfohlen ≥ +0.30%."
  - Maker = Short → "min_spread_pct ist der wichtigere Hebel — empfohlen ≤ −0.30%."
- **Smart defaults (aggressive ±0.10% / ±0.40%):** beim Wechsel der Maker-Wahl werden Entry- und Exit-Spread-Felder automatisch asymmetrisch vorbelegt — solange der User noch keinen der vier Spread-Inputs editiert hat. Sobald der User selbst einen Wert ändert, wird die Auto-Belegung deaktiviert (`userTouchedSpread`-Flag).
- Reine Frontend-Änderung in `BotCreateModal.vue`. Rationale: Spread-Definition ist `(long − short) / short`. Bei Maker = Short profitiert die Maker-Order von steigendem Short-Bid → Spread driftet während Wartezeit Richtung negativ → `min_spread_pct` braucht mehr Headroom unten.

---

## [Unreleased] — 2026-04-27

### Features

#### Bot creation: minimum order size validation (FN bot)
The `BotCreateModal` now validates position size and chunk count against per-exchange minimums before allowing submission.

**Rules enforced (Step 4 of the wizard):**
- **Nado as maker:** total notional ≥ 1000 USD AND chunk notional ≥ 100 USD (Nado's published `min_notional_usd`).
- **Any maker exchange with a base-qty floor (Extended, GRVT):** `chunk_qty ≥ min_order_size`.
- **Variational as maker:** no validation (no min-size data published).
- **Taker-side mismatch:** soft warning only (engine auto-reduce stays in place).

**Implementation:**
- New `frontend/src/lib/oms-meta.ts` — fetches OMSv2 `/meta/{exchange}/{symbol}` from the browser with 5min in-memory cache.
- New `frontend/src/lib/bot-validation.ts` — pure validation function `validateBotCreate()` with constants `NADO_MAKER_MIN_TOTAL_USD = 1000`, `NADO_MAKER_MIN_CHUNK_NOTIONAL_USD = 100`.
- `BotCreateModal.vue` Step 4 now shows live validation feedback and blocks "Next" if a hard rule fails.
- New env var: `VITE_OMS_META_URL` (defaults to `https://oms-v2.defitool.de` in `.env.production`).

**OMS V2 Worker — CORS on `/meta`, `/quote`, `/book`:**
- `aggregator.ts` now emits `Access-Control-Allow-Origin` headers and handles `OPTIONS` preflight for these public read-only endpoints. Required for browser-direct access from the Vue SPA.

**Backend untouched:** `app/server.py POST /fn/bots` remains permissive; engine `engine.py:902-915` auto-reduce-chunks stays as defense-in-depth. DNA bot and adopt-bot are out of scope.

**Deploy order:** OMS V2 worker first (CORS fix) → frontend (`./deploy/cloudflare/deploy.sh`).

---

## [Unreleased] — 2026-04-20

### Bug Fixes

#### Extended SDK — `__markets_info_module` AttributeError
**Symptom:** `'PerpetualTradingClient' object has no attribute '_PerpetualTradingClient__markets_info_module'`
**Root cause:** x10 Python SDK v1.3.1 renamed internal private attributes. Name-mangled references broke silently after the package update.
**Fix (`app/extended_client.py`):**
- `__markets_info_module.get_markets_dict()` → public property `markets_info.get_markets_dict()`
- `__stark_account` → public property `stark_account`
- `__config` → public property `config`

#### Extended — `float / Decimal` TypeError on maker order placement
**Symptom:** `unsupported operand type(s) for /: 'float' and 'decimal.Decimal'` during chunk execution.
**Root cause:** `_round_qty`, `_round_price`, and `_round_to_tick` assumed the input was already `Decimal`, but callers occasionally passed `float` values.
**Fix (`app/extended_client.py`):** Defensively wrap input in `Decimal(str(amount))` / `Decimal(str(price))` before dividing by the tick/step size.

#### OMS — Extended orderbooks showing `×` in DNA Bot pre-flight (82 WS connections)
**Symptom:** All Extended symbols showed as disconnected in DNA Bot. OMS logs showed `InvalidStatusCode: HTTP 429` / connection refusals.
**Root cause:** OMS opened one WebSocket per tracked symbol (82 simultaneous connections). Extended rate-limited or rejected the flood.
**Fix (`deploy/monitor/monitor_service.py`):**
- Replaced 82 per-symbol connections with a single shared WS to `wss://…/v1/orderbooks` (no market parameter → server pushes all markets).
- New `_run_extended_ws_all()` function handles reconnection and routes incoming messages by the `m` (market) field.
- Removed semaphore / stagger logic; no longer needed.

#### OMS — Nado orderbook inversion for ZRO, ZEC, XMR and similar symbols
**Symptom:** Spread gate immediately aborted because best bid > best ask on Nado for affected symbols.
**Root cause:** Nado sends bids and asks in swapped fields for a subset of symbols.
**Fix (`deploy/monitor/monitor_service.py`):** After each Nado delta is applied, detect inversion (`bids[0][0] > asks[0][0]`) and swap + re-sort both sides in-place.

#### `tradeautonom-v3` (port 8005) not connected to OMS
**Root cause:** Container `.env` and bot `config.json` files still referenced the old server IP (`192.168.133.253`) after the server migration to `192.168.133.100`.
**Fix:** Updated `NAS_HOST` in `/opt/tradeautonom-v3/.env` and `fn_opt_shared_monitor_url` in all bot config files to `192.168.133.100`.

---

### Improvements

#### Pre-round taker depth gate (`app/state_machine.py`, `app/safety.py`)
**Problem:** The spread gate only checked the best bid/ask (BBO). For large chunk quantities, the actual fill price on the taker side could be significantly worse — or the taker book might not have enough depth at all, resulting in partial fills or no fills.

**New behaviour:**
1. **BBO check** (unchanged) — reject if BBO spread outside `max_spread_pct`.
2. **Taker depth check** — walk the taker book for the full `remaining_qty`:
   - If `unfilled_qty > 5%` of `remaining_qty` → insufficient depth, wait 2 s and retry.
   - Compute depth-weighted spread (VWAP-based). If it exceeds `max_spread_pct` → wait 2 s.
   - On pass: store `taker_sweep_price` (the worst price level needed to fill `remaining_qty`).
3. **Taker IOC limit price** reuses `taker_sweep_price` instead of the old hardcoded `best ± 50 ticks`. This ensures the IOC sweeps exactly enough depth to fill the full quantity.
4. `taker_sweep_price` is invalidated on each reprice round so the next gate run recomputes from a fresh book snapshot.

**New helper (`app/safety.py`):** `walk_book(order_book, side, quantity) → (vwap, worst_price, unfilled_qty)` — walks orderbook levels for a given qty and returns the VWAP fill price, the deepest level touched (sweep limit), and any unfilled remainder. `estimate_fill_price` now delegates to `walk_book`.

#### Nado taker fill — retry polling (`app/state_machine.py`)
**Problem:** Nado's matching engine is asynchronous. `async_check_order_fill` queried immediately after placement returned `traded_qty=0` even for successful fills, causing false "NOT FILLED" results and unnecessary emergency unwinds.

**Fix in `_check_taker_fill`:** For the `status=success/FILLED/CLOSED` path (Nado), retry the REST poll up to 5× with 500 ms delay between attempts. Returns the first non-zero `traded_qty` found; logs a warning if still 0 after all attempts.

---

### Deployment

- `manage.sh update` syncs code, rebuilds the Docker image, and restarts all registered user containers.
- Stale `testbot1` / `testbot2` entries removed from `users.json` registry (ports 9001/9002 were in conflict with live user containers `ta-user-eta9u0ir` / `ta-user-kWKpWCKa`).
- All 8 user containers (`ta-user-*`) and `tradeautonom-v3` restarted and confirmed healthy.
- OMS container (`oms`) was already running the single-WS Extended fix from a prior deploy; no OMS restart required.

---

## Previous Releases

### 2026-04-19 — Server Migration + Activity Analytics
- Migrated all deployment targets from `192.168.133.253` (old NAS) to `192.168.133.100` (new server).
- Added activity analytics pipeline and admin activity UI.
- User containers now use a read-only code volume mount instead of baking app code into the image.

### Earlier
See `git log` for full history.
