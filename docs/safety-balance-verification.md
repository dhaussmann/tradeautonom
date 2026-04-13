# Safety & Balance-Verification — Detaillierte Dokumentation

> **Dateien:** `app/state_machine.py`, `app/risk_manager.py`, `app/engine.py`
> **Zweck:** Sicherstellen, dass Long- und Short-Positionen jederzeit balanciert bleiben und bei Abweichungen automatisch repariert werden.

---

## 1. Überblick: Warum Balance-Verification?

Bei delta-neutraler Arbitrage hält der Bot **gleichzeitig** eine Long- und Short-Position auf zwei verschiedenen Exchanges. Diese müssen **exakt gleich groß** sein. Jede Abweichung ("Gap") bedeutet ungedecktes Marktrisiko:

| Szenario | Risiko |
|----------|--------|
| Long > Short | Bot ist netto-long → Verlust bei fallendem Markt |
| Short > Long | Bot ist netto-short → Verlust bei steigendem Markt |
| Long = Short | Delta-neutral → kein Richtungsrisiko ✓ |

**Ein Gap von 1 SOL bei $140 = $140 ungedecktes Exposure.**

Die Safety-Systeme sind in drei Ebenen aufgebaut:

```
Ebene 1: Intra-Chunk Balance (nach jedem einzelnen Chunk)
Ebene 2: Pre-Trade Checks (vor jedem Entry/Exit)
Ebene 3: Background Risk Monitoring (kontinuierlich während HOLDING)
```

---

## 2. Ebene 1: Intra-Chunk Balance Verification

### 2.1 Baseline-Snapshot (`_snapshot_baseline_positions`)

**Code:** `state_machine.py` Zeile 916–940

**Was:** Vor dem ersten Chunk werden die aktuellen Positionen auf beiden Exchanges abgefragt und als Baseline gespeichert.

**Warum:** Die Exchanges können Restpositionen von vorherigen Runs oder manuellen Trades haben. Wenn der Bot die absoluten Positionen vergleichen würde, könnte er eine alte 80-SOL-Position als "Gap" interpretieren und versuchen, sie zu reparieren — was katastrophale Konsequenzen hätte (exponentiell wachsende Positionen).

**Wie:**
```python
async def _snapshot_baseline_positions(self, config) -> tuple[Decimal, Decimal]:
    # WS-Cache first (DataLayer), REST als Fallback
    maker_size = await self._get_position_size(config.maker_exchange, config.maker_symbol, maker_client)
    taker_size = await self._get_position_size(config.taker_exchange, config.taker_symbol, taker_client)
    return maker_size, taker_size
```

**Datenquelle (seit WS Position-Cache):**
- **Extended:** WS Account-Stream (`/v1/account`) → POSITION-Events → DataLayer-Cache
- **Nado:** WS `position_change` Stream → DataLayer-Cache
- **GRVT:** REST-Polling alle 2s (kein WS-Position-Stream verfügbar)
- **Fallback:** REST `async_fetch_positions()` wenn Cache stale (>3s)

**Beispiel:**
```
Baseline:  Maker=80.0 SOL, Taker=80.0 SOL  (alte Position)
Nach Chunk 1: Maker=90.0 SOL, Taker=90.0 SOL
→ Delta:   Maker=10.0, Taker=10.0
→ Gap:     |10.0 - 10.0| = 0.0 ✓

OHNE Baseline:
→ Gap wäre |90.0 - 90.0| = 0.0 (zufällig OK)
ABER wenn Taker erst 89.5:
→ Gap wäre |90.0 - 89.5| = 0.5 (korrekt)
vs. bei ungleicher Baseline:
→ Gap wäre |90.0 - 79.5| = 10.5 (FALSCH! → phantom repair)
```

### 2.2 Exchange-Position-Verification (`_verify_exchange_positions`)

**Code:** `state_machine.py` Zeile 942–993

**Wann:** Nach **jedem** Chunk (+ 500ms Delay für Exchange-Settlement).

**Was:** Echte Positionen von beiden Exchanges abfragen und den Gap berechnen.

**Wie:**
```python
async def _verify_exchange_positions(self, config, chunk_index):
    # 1. Positionen lesen — WS-Cache first (<3s), REST als Fallback
    maker_size = await self._get_position_size(maker_exchange, maker_symbol, maker_client)
    taker_size = await self._get_position_size(taker_exchange, taker_symbol, taker_client)

    # 2. Delta von Baseline berechnen (NUR was dieser Run erstellt hat)
    maker_delta = maker_size - self._baseline_maker_size
    taker_delta = taker_size - self._baseline_taker_size

    # 3. Gap = Differenz der Deltas
    gap = abs(maker_delta - taker_delta)

    # 4. UI-Position synchronisieren (absolute Werte)
    self._long_qty = float(maker_size)   # oder taker_size, je nach Seite
    self._short_qty = -float(taker_size)

    return gap, maker_delta, taker_delta
```

**Kritische Design-Entscheidung: Decimal-Arithmetik**
Alle Berechnungen verwenden `Decimal`, nicht `float`. Floating-Point-Rundungsfehler könnten sonst Phantom-Gaps erzeugen (z.B. `10.0 - 9.999999999999998 = 0.000000000000002`).

### 2.3 Effective Gap: Exchange ist autoritativ

**Code:** `state_machine.py` Zeile 514–521

```python
# Exchange-Position-Gap ist AUTORITATIV
if pos_gap is not None:
    effective_gap = pos_gap        # ← Exchange sagt Gap=0? Dann IST es 0.
else:
    effective_gap = chunk_gap      # ← Nur als Fallback wenn Exchange nicht erreichbar
```

**Warum nicht `max(pos_gap, chunk_gap)`?**

Dies war ein kritischer Bug: Die Taker-Fill-Prüfung meldete manchmal `traded_qty: 0`, obwohl GRVT den Order tatsächlich gefüllt hatte. `max(0.0, 10.0) = 10.0` → der Bot dachte es gäbe einen Gap und schickte einen Repair-IOC. Dieser Repair erzeugte einen echten Gap auf der Taker-Seite, den der nächste Chunk als neuen Gap sah → exponentielles Cascading:

```
Chunk 1: chunk_gap=10, pos_gap=0 → max()=10 → repair 10 → FALSCH
Chunk 2: sieht Taker+10 extra → chunk_gap=10 → repair 10 → FALSCH
Chunk 3: sieht Taker+20 extra → repair 20 → KATASTROPHE
```

**Fix:** `effective_gap = pos_gap` wenn Exchange-Daten verfügbar. Die Exchange ist die einzige Wahrheit.

### 2.4 Repair-Mechanismus (`_repair_imbalance`)

**Code:** `state_machine.py` Zeile 1016–1071

**Wann:** Wenn `effective_gap > min_repair_qty`.

**Was:** Ein IOC-Order auf der Taker-Seite in der Größe des Gaps.

**Ablauf:**
```
1. Taker-Orderbook lesen
2. IOC-Preis berechnen (best + 50 ticks buffer)
3. IOC-Order platzieren
4. Fill prüfen (WS-first, REST-fallback)
5. Wenn voll gefüllt → Balance wiederhergestellt ✓
6. Wenn partiell → return False, Caller versucht nochmal
7. Wenn nicht gefüllt → return False
```

**Multi-Attempt Repair:**
```
Attempt 1: Repair-IOC → prüfe Ergebnis
  → Nicht gefüllt? 500ms warten, Exchange nochmal abfragen
Attempt 2: Neuer Repair mit aktuellem Gap
  → Nicht gefüllt? 1s warten
Attempt 3: Letzter Versuch
  → Nicht gefüllt? → Carry-Over oder TWAP-Abort
```

**Code:** `state_machine.py` Zeile 532–581
```python
for attempt in range(1, 4):   # Maximal 3 Versuche
    if remaining_gap < min_repair_qty:
        repair_ok = True      # Gap ist unter Minimum → akzeptieren
        break
    repair_ok = await self._repair_imbalance(config, i, chunk, remaining_gap)
    if repair_ok:
        break
    # Re-query: vielleicht hat der Repair teilweise gefüllt
    await asyncio.sleep(0.5)
    new_gap, _, _ = await self._verify_exchange_positions(config, i)
    if new_gap <= min_repair_qty:
        repair_ok = True      # Jetzt OK
        break
    remaining_gap = new_gap   # Nächster Versuch mit aktuellem Gap
```

### 2.5 Carry-Over bei nicht-reparierbarem Rest

**Code:** `state_machine.py` Zeile 572–581

Wenn nach 3 Repair-Versuchen ein kleiner Gap bleibt (≤ 5× min_repair_qty), wird er nicht als Fehler behandelt, sondern zum nächsten Chunk **übertragen**:

```python
if final_gap <= min_repair_qty * 5:
    self._carry_over_gap = Decimal(str(final_gap))
    # → Nächster Chunk: maker_qty -= carry_over_gap
    # Dadurch "holt" die Taker-Seite natürlich auf
else:
    # Gap zu groß → TWAP abbrechen
    result.error = "Position imbalance repair failed"
    break
```

**Warum?**
Manche Exchanges haben Minimum-Ordergrößen (z.B. 0.01 SOL). Ein Gap von 0.005 SOL kann nicht direkt repariert werden. Durch Carry-Over wird der nächste Maker-Order um 0.005 reduziert → nach dem nächsten Taker-Hedge ist der Gap geschlossen.

### 2.6 Post-Repair Position Sync

**Code:** `state_machine.py` Zeile 523–530

Nach jedem Chunk wird `filled_so_far` mit der echten Exchange-Position synchronisiert:

```python
if actual_maker_delta is not None:
    actual_maker_dec = Decimal(str(actual_maker_delta))
    if actual_maker_dec > filled_so_far + Decimal("0.01"):
        filled_so_far = actual_maker_dec
        result.total_maker_qty = float(actual_maker_dec)
```

**Warum?**
Ohne diese Synchronisation könnte der Bot denken, er hätte erst 8 SOL gefüllt (weil ein Fill-Event verloren ging), obwohl die Exchange 10 SOL zeigt. Das würde zu unnötigen Extra-Chunks führen.

### 2.7 Taker-Error Clearing bei Exchange-Bestätigung

**Code:** `state_machine.py` Zeile 586–589

```python
# Taker meldet "nicht gefüllt" ABER Exchange zeigt Gap=0?
# → Taker WAR gefüllt, Fill-Check war falsch → Error löschen
if pos_gap <= min_repair_qty and chunk.error and "not filled" in chunk.error:
    chunk.error = None  # Exchange hat Recht, nicht der Fill-Check
```

Dies passiert häufig bei GRVT: Die IOC-Response sagt `traded_qty: 0`, aber die Position wurde trotzdem erhöht. Der Exchange-Position-Check ist die Wahrheit.

---

## 3. Ebene 2: Pre-Trade Checks (`RiskManager.pre_trade_check`)

**Code:** `risk_manager.py` Zeile 141–173

**Wann:** Vor jedem Entry und Exit, für **beide** Seiten (Maker und Taker).

**Was wird geprüft:**

### 3.1 Circuit-Breaker Status
```python
if self._is_halted:
    return False, "Trading halted by circuit breaker"
```
Wenn der Circuit-Breaker aktiv ist, werden keine neuen Trades erlaubt.

### 3.2 Minimum-Ordergröße
```python
min_size = await client.async_get_min_order_size(symbol)
if qty < min_size:
    return False, f"Qty {qty} below min order size {min_size}"
```
Jede Exchange hat eine Mindest-Ordergröße. Orders darunter würden von der Exchange rejected.

### 3.3 Orderbook-Sync-Status
```python
ob = self._data_layer.get_orderbook(exchange, symbol)
if not ob.is_synced:
    return False, f"Orderbook not synced for {exchange}:{symbol}"
```
Wenn das WebSocket-Orderbook nicht synchronisiert ist (Sequence-Gap, Verbindungsverlust), werden keine Trades erlaubt. Der Bot wartet, bis die Daten wieder zuverlässig sind.

### 3.4 Liquiditäts-Check
```python
levels = ob.asks if side == "buy" else ob.bids
total_available = sum(float(lv[1]) for lv in levels[:10])
if total_available < float(qty):
    return False, f"Insufficient liquidity: available={total_available} needed={qty}"
```
Die verfügbare Liquidität in den Top-10 Levels muss mindestens so groß sein wie die gewünschte Menge. Dies verhindert exzessiven Slippage.

### 3.5 Spread-Check (Nicht-blockierend)

**Code:** `risk_manager.py` Zeile 175–202

```python
def check_spread(self, exch_a, sym_a, exch_b, sym_b):
    mid_a = (best_bid_a + best_ask_a) / 2
    mid_b = (best_bid_b + best_ask_b) / 2
    spread_pct = abs(mid_a - mid_b) / avg_mid * 100

    if spread_pct > self._max_spread_pct:
        return False, spread_pct, "Spread too wide"
    return True, spread_pct, "OK"
```

**Wird im Engine als Log genutzt, blockiert aber nicht den Trade:**
```python
# engine.py Zeile 496
ok, spread_pct, reason = self._risk_manager.check_spread(...)
# → Nur geloggt, nicht als Blocker verwendet
```

### 3.6 Aufruf-Reihenfolge im Engine

**Code:** `engine.py` Zeile 488–493

```python
# Für BEIDE Seiten separat geprüft:
ok, reason = await self._risk_manager.pre_trade_check(maker_exch, maker_symbol, maker_side, qty)
if not ok:
    raise RuntimeError(f"Pre-trade check failed: {reason}")

ok, reason = await self._risk_manager.pre_trade_check(taker_exch, taker_symbol, taker_side, qty)
if not ok:
    raise RuntimeError(f"Pre-trade check failed: {reason}")
```

---

## 4. Ebene 3: Background Risk Monitoring

### 4.1 Monitor-Loop (`_monitor_loop`)

**Code:** `risk_manager.py` Zeile 206–215

```python
async def _monitor_loop(self):
    while self._running:
        await self._check_all()
        await asyncio.sleep(self._check_interval_s)  # Default: 5s
```

Läuft als Background-Task und prüft periodisch alle Risiko-Bedingungen.

### 4.2 Circuit Breaker

**Code:** `risk_manager.py` Zeile 97–104, 223–234

**Was:** Wenn der kumulative PnL unter den Schwellwert fällt (Default: -$500), wird **aller Handel gestoppt**.

**Wann:** Nach jedem abgeschlossenen Trade (`record_trade_pnl`) + periodisch im Monitor-Loop.

```python
def record_trade_pnl(self, pnl: float):
    self._cumulative_pnl += pnl
    if self._cumulative_pnl <= -self._circuit_breaker_loss_usd:
        self._trigger_circuit_breaker()

def _trigger_circuit_breaker(self):
    self._is_halted = True
    # Alert erstellen (für Dashboard)
    alert = RiskAlert(
        alert_type="CIRCUIT_BREAKER",
        severity="critical",
        message=f"Circuit breaker: PnL ${self._cumulative_pnl:.2f} < -${threshold}",
        auto_action="HALT",
    )
```

**Reset:** Nur manuell durch den User über `POST /fn/bots/{bot_id}/risk/reset-halt`.

### 4.3 Konfigurations-Parameter

| Parameter | Default | Beschreibung |
|-----------|---------|-------------|
| `delta_max_usd` | $50 | Maximale erlaubte Delta-Abweichung in USD |
| `circuit_breaker_loss_usd` | $500 | PnL-Schwelle für automatischen Halt |
| `max_spread_pct` | 0.05% | Maximale Cross-Exchange-Spread-Abweichung |
| `check_interval_s` | 5s | Intervall des Background-Monitors |

---

## 5. Emergency Unwind

**Code:** `state_machine.py` Zeile 1272–1308

**Wann:** Wenn der Maker gefüllt ist, aber der Taker-Hedge **komplett fehlschlägt** (0 Fill).

**Was:** Sofortige Rückabwicklung der Maker-Position durch einen aggressiven IOC auf der Maker-Exchange:

```python
async def _emergency_unwind(self, config, result):
    unwind_qty = result.total_maker_qty - result.total_taker_qty
    unwind_side = "sell" if config.maker_side == "buy" else "buy"

    # Sehr aggressiver Preis: best - 10 ticks (sell) oder best + 10 ticks (buy)
    price = best - tick * 10  # oder + 10

    resp = await maker_client.async_create_ioc_order(
        symbol=config.maker_symbol,
        side=unwind_side,
        amount=unwind_qty,
        price=price,
    )
```

**Worst Case:** Wenn auch der Emergency Unwind fehlschlägt, wird ein `CRITICAL`-Log geschrieben: `"EMERGENCY UNWIND FAILED — MANUAL INTERVENTION REQUIRED"`. Dann muss der User die Position manuell schließen.

---

## 6. Execution-Abbruch (`abort_execution`)

**Code:** `state_machine.py` Zeile 266–278

Der User kann jederzeit einen laufenden TWAP abbrechen:

```python
async def abort_execution(self):
    if self._state in (JobState.ENTERING, JobState.EXITING):
        self._transition(JobState.IDLE)
        await asyncio.sleep(0.2)  # Chunk-Loop Zeit geben zum Erkennen
```

Die Chunk-Loop prüft am Anfang jeder Iteration:
```python
if self._state not in (JobState.ENTERING, JobState.EXITING):
    # Cancel offene Maker-Order
    await maker_client.async_cancel_order(maker_order_id)
    chunk.error = "Execution cancelled"
    return chunk
```

Dies stellt sicher, dass:
1. Keine neuen Orders platziert werden
2. Offene Maker-Orders gecancelt werden
3. Bereits gefüllte Positionen erhalten bleiben (kein automatischer Unwind)

---

## 7. WS Fill-Subscriptions

**Code:** `state_machine.py` Zeile 1097–1157

### 7.1 Aufbau

Für jede Exchange wird ein Background-Task gestartet, der WS-Fill-Events empfängt:

```python
async def start_fill_subscriptions(self, symbols_map):
    for exch_name, symbol in symbols_map.items():
        task = asyncio.create_task(
            self._run_fill_subscription(client, exch_name, symbol)
        )
```

### 7.2 Event-Speicherung

```python
# Dict: order_id → [fill_events]
self._fill_events: dict[str, list[dict]] = {}

# Event-Signal für Waiter
self._fill_event = asyncio.Event()

async def _on_fill_event(self, fill: dict):
    oid = str(fill["order_id"])
    self._fill_events.setdefault(oid, []).append(fill)
    self._fill_event.set()  # Weckt _wait_for_maker_fill / _check_taker_fill
```

### 7.3 Auto-Reconnect

```python
async def _run_fill_subscription(self, client, exch_name, symbol):
    while self._fill_subs_running:
        try:
            await client.async_subscribe_fills(symbol, self._on_fill_event)
        except Exception:
            await asyncio.sleep(3)  # Retry nach 3s
```

---

## 8. Zusammenfassung: Safety-Kette pro Chunk

```
VOR dem Entry:
  ├─ Pre-Trade Check Maker (min size, liquidity, OB sync, circuit breaker)
  ├─ Pre-Trade Check Taker (min size, liquidity, OB sync, circuit breaker)
  └─ Spread Check (log only)

Baseline Snapshot (einmalig vor Chunk 1):
  └─ Aktuelle Positionen beider Exchanges speichern

PRO Chunk:
  ├─ Maker Post-Only platzieren
  │   ├─ Post-Only Rejection → Re-Anchor (max 50x)
  │   ├─ Timeout → Cancel + Reprice (unbegrenzt)
  │   └─ Cancel Race → Post-Cancel Fill-Check + WS-Check
  │
  ├─ Taker IOC Hedge (sofort nach Maker-Fill)
  │   ├─ 50-Tick Buffer für Slippage-Schutz
  │   └─ WS-First Fill-Check (500ms), REST-Fallback
  │
  ├─ 500ms Settlement-Delay
  │
  ├─ Exchange Position Verification
  │   ├─ Delta von Baseline berechnen (nicht absolute Position!)
  │   ├─ Exchange-Gap ist AUTORITATIV (nicht chunk_gap)
  │   └─ filled_so_far mit Exchange synchronisieren
  │
  ├─ Repair bei Gap > min_repair_qty
  │   ├─ IOC auf Taker-Seite (max 3 Versuche)
  │   ├─ Re-Query nach jedem Versuch
  │   └─ Kleiner Rest → Carry-Over zum nächsten Chunk
  │
  └─ Taker-Error clearing wenn Exchange Gap=0 bestätigt

NACH allen Chunks:
  ├─ Erfolg? → HOLDING / IDLE
  ├─ Maker gefüllt, Taker nicht? → Emergency Unwind
  └─ State auf Disk speichern

WÄHREND HOLDING (Background):
  └─ RiskManager Monitor Loop (alle 5s)
      └─ Circuit Breaker Check (kumulative PnL)
```
