# Maker-Taker TWAP Execution — Detaillierte Dokumentation

> **Datei:** `app/state_machine.py`
> **Klasse:** `StateMachine`
> **Zweck:** Ausführung von delta-neutralen Entry/Exit-Positionen über zwei Exchanges hinweg mittels Maker-Taker TWAP (Time-Weighted Average Price).

---

## 1. Überblick: Was ist Maker-Taker Execution?

Bei einer Funding-Rate-Arbitrage hält der Bot gleichzeitig eine **Long-Position** auf Exchange A und eine **Short-Position** auf Exchange B (oder umgekehrt). Um diese Positionen aufzubauen oder abzubauen, wird die Gesamtmenge in **Chunks** aufgeteilt (TWAP), wobei jeder Chunk einem zweistufigen Ablauf folgt:

1. **Maker-Order** (Post-Only Limit): Wird passiv ins Orderbuch gelegt → zahlt niedrigere Fees (Maker-Fee statt Taker-Fee)
2. **Taker-Hedge** (IOC Market): Sobald der Maker gefüllt ist, wird sofort auf der Gegenseite ein aggressiver IOC-Order geschickt, um die Position zu hedgen

**Warum dieses Design?**
- **Kostenersparnis:** Maker-Fees sind deutlich günstiger als Taker-Fees (oft 0% vs. 0.05%)
- **Slippage-Kontrolle:** Post-Only garantiert, dass man den Preis bekommt, den man will
- **Delta-Neutralität:** Durch sofortiges Hedging nach jedem Maker-Fill bleibt das Exposure minimal

---

## 2. Zustandsmaschine (State Machine)

### 2.1 Hauptzustände (`JobState`)

```
IDLE → ENTERING → HOLDING → EXITING → IDLE
```

| Zustand | Beschreibung |
|---------|-------------|
| `IDLE` | Keine Position, bereit für neuen Entry |
| `ENTERING` | Position wird aufgebaut (Chunks werden ausgeführt) |
| `HOLDING` | Position vollständig aufgebaut, Funding wird gesammelt |
| `EXITING` | Position wird abgebaut (umgekehrte Chunks) |
| `ERROR` | Schwerer Fehler (manuelles Eingreifen nötig) |

### 2.2 Chunk-Substates (`ChunkState`)

Jeder einzelne Chunk durchläuft:

```
MAKER_PLACE → MAKER_WAIT → TAKER_HEDGE → CHUNK_DONE
```

| Substate | Was passiert |
|----------|-------------|
| `MAKER_PLACE` | Post-Only Limit-Order wird an die Maker-Exchange geschickt |
| `MAKER_WAIT` | Warten auf Fill (WS-Events + REST-Polling als Fallback) |
| `TAKER_HEDGE` | IOC-Order auf der Taker-Exchange zum Hedging |
| `CHUNK_DONE` | Chunk abgeschlossen, Positionen verifiziert |

---

## 3. Konfiguration (`MakerTakerConfig`)

```python
@dataclass
class MakerTakerConfig:
    maker_exchange: str       # z.B. "extended" — Exchange für passive Maker-Orders
    taker_exchange: str       # z.B. "grvt" — Exchange für aggressive IOC-Hedges
    maker_symbol: str         # z.B. "SOL-PERP" — Instrument auf der Maker-Exchange
    taker_symbol: str         # z.B. "SOL_USDT_Perp" — Instrument auf der Taker-Exchange
    maker_side: str           # "buy" oder "sell" — Richtung des Makers
    taker_side: str           # Immer das Gegenteil von maker_side
    total_qty: Decimal        # Gesamtmenge über alle Chunks
    num_chunks: int           # Anzahl geplanter Chunks (TWAP-Intervalle)
    chunk_interval_s: float   # Wartezeit zwischen Chunks (Default: 2s)
    maker_timeout_ms: int     # Wie lange auf Maker-Fill warten (Default: 5000ms)
    maker_reprice_ticks: int  # Ticks für Reprice bei fehlgeschlagenem Book-Fetch
    maker_max_chase_rounds: int  # (Legacy, nicht mehr als Hard-Limit verwendet)
    maker_offset_ticks: int   # Offset vom besten Preis (0 = Top-of-Book)
    simulation: bool          # True = kein echtes Trading, nur Logging
    reduce_only: bool         # True bei Exit — verhindert Positions-Erhöhung
```

---

## 4. Entry-Flow im Detail (`execute_entry`)

**Code:** `state_machine.py` Zeile 182–219

### 4.1 Vorbedingungen
- State muss `IDLE` sein (sonst `RuntimeError`)
- Long/Short-Exchange und -Symbol werden aus der Config abgeleitet

### 4.2 Ablauf
1. **State-Transition:** `IDLE → ENTERING`
2. **`_execute_maker_taker(config, "ENTER")` wird aufgerufen** (siehe Abschnitt 5)
3. **VWAP Entry-Preise berechnen:** `_compute_entry_prices()` — gewichteter Durchschnittspreis über alle Chunks
4. **Ergebnis-Handling:**
   - **Erfolg:** `ENTERING → HOLDING` — Position steht
   - **Maker gefüllt, Taker nicht:** **Emergency Unwind** — sofortiger Rückbau der Maker-Position
   - **Kein Fill:** `ENTERING → IDLE` — zurück zum Ausgangszustand
   - **Partiell:** `ENTERING → HOLDING` — Teilposition wird gehalten
5. **State auf Disk speichern** (`save_state()`)

### 4.3 Warum Emergency Unwind?
Wenn der Maker auf Exchange A gefüllt wurde, der Taker auf Exchange B aber nicht — dann hat der Bot eine ungedeckte Position. Das ist das gefährlichste Szenario. Der Emergency Unwind schickt sofort eine aggressive IOC-Order auf Exchange A in die Gegenrichtung, um die offene Position zu schließen.

---

## 5. Kern-Execution: `_execute_maker_taker()`

**Code:** `state_machine.py` Zeile 411–625

Dies ist die zentrale TWAP-Schleife, die sowohl für Entry als auch Exit verwendet wird.

### 5.1 Initialisierung

```
1. Baseline-Positions-Snapshot auf beiden Exchanges aufnehmen
2. Minimum-Ordergröße der Taker-Exchange abfragen (für Repair-Schwellwert)
3. Chunk-Größe berechnen: total_qty / num_chunks
```

**Warum Baseline-Snapshot?**
Die Exchanges können Restpositionen von vorherigen Runs haben. Der Bot darf nur die **Deltas** (Änderungen) vergleichen, nicht die absoluten Positionen. Sonst würde er eine 80-SOL-Restposition als "Gap" sehen und versuchen, sie zu reparieren.

### 5.2 Chunk-Schleife

Für jeden Chunk `i` von `0` bis `num_chunks-1` (+ eventuelle Extra-Chunks):

```
1. Chunk-Größe bestimmen
   - Letzter geplanter Chunk: restliche Menge (total_qty - filled_so_far)
   - Extra-Chunks: was noch fehlt
   - Carry-Over-Gap aus vorigem Chunk subtrahieren

2. Wartezeit zwischen Chunks (chunk_interval_s, außer beim ersten)

3. Bei reduce_only (Exit): Chunk-Größe an verbleibende Maker-Position kappen
   → Verhindert, dass mehr verkauft wird als vorhanden

4. _execute_single_chunk() ausführen (siehe Abschnitt 6)

5. Position inkrementell aktualisieren
6. Exchange-Positionen verifizieren (Safety, siehe Abschnitt 8)
7. Bei Imbalance: Repair-IOC auf Taker-Seite
8. filled_so_far tracken für Extra-Chunk-Logik
```

### 5.3 Extra-Chunks

Wenn nach allen geplanten Chunks die Zielmenge nicht erreicht ist (z.B. weil ein Maker nur partiell gefüllt wurde), werden **Extra-Chunks** generiert. Safety-Cap: maximal 20 Extra-Chunks.

### 5.4 Carry-Over-Gap

Wenn nach einem Chunk ein kleiner Gap bleibt, der unterhalb der minimalen Ordergröße liegt (z.B. 0.008 SOL), kann dieser nicht repariert werden. Stattdessen wird er als `_carry_over_gap` zum nächsten Chunk übertragen — der nächste Maker-Order wird um diesen Betrag reduziert, so dass die Taker-Seite wieder aufholt.

---

## 6. Einzelner Chunk: `_execute_single_chunk()`

**Code:** `state_machine.py` Zeile 627–912

### 6.1 Step 1: Orderbook lesen & Maker-Preis berechnen

```python
book = await self._get_book(maker_exchange, maker_symbol, maker_client)
tick = await maker_client.async_get_tick_size(maker_symbol)

if maker_side == "buy":
    best = book["bids"][0][0]          # Bester Bid
    maker_price = best + tick * offset  # Default offset=0 → Top-of-Book
else:
    best = book["asks"][0][0]          # Bester Ask
    maker_price = best - tick * offset
```

**`_get_book()` Priorisierung:**
1. **WS-Cache** (DataLayer) — wenn Daten vorhanden und frisch (<5 Sekunden)
2. **REST-Fallback** — nur wenn WS leer oder zu alt
3. **WS auch bei Seq-Gap** — ein leicht veraltetes WS-Book ist besser als 1s REST-Latenz

### 6.2 Step 2: Maker Post-Only Order + Chase-Loop

Der Maker-Order wird als **Post-Only** (passiv) platziert. Wenn er nicht innerhalb des Timeouts gefüllt wird, wird er gecancelt und zum aktuellen Best-Price repriced. Dieser Loop läuft unbegrenzt (kein Hard-Limit), mit folgenden Sicherheitsmechanismen:

**Chase-Loop Ablauf:**
```
while True:
    1. Prüfe ob Execution extern abgebrochen wurde (State != ENTERING/EXITING)
    2. Platziere Post-Only Order
    3. Warte auf Fill (maker_timeout_ms)
    4. Wenn gefüllt → break (weiter zu Taker)
    5. Wenn partiell gefüllt → cancel Restmenge, break mit Teilmenge
    6. Wenn nicht gefüllt → cancel, reprice, nächste Runde
```

**Post-Only Rejection Handling:**
Wenn die Exchange den Post-Only Order ablehnt (Preis würde sofort matchen = wäre Taker), wird:
1. 100ms gewartet
2. Orderbook neu gelesen
3. Preis auf aktuellen Best Bid/Ask re-anchored
4. Nach 50 aufeinanderfolgenden Rejections wird der Chunk abgebrochen

**Warum re-anchor statt Tick-Shift?**
In Märkten mit 1-Tick-Spread gibt es nur einen gültigen Post-Only-Preis. Tick-Shift würde zu einem schlechteren Preis führen. Re-Anchoring zum Market stellt sicher, dass der nächste Versuch den besten passiven Preis verwendet.

### 6.3 Step 2b: Maker-Fill Erkennung (`_wait_for_maker_fill`)

**Code:** `state_machine.py` Zeile 1160–1214

Hybrides System aus WS-Events und REST-Polling:

```
Deadline = now + maker_timeout_ms

while now < deadline:
    1. WS-Fill-Events prüfen (sofort, kein Netzwerk)    ← Primär
    2. REST-Poll alle 500ms (async_check_order_fill)      ← Fallback
    3. Auf nächstes WS-Event warten (100ms max)
    4. Wiederholen

Final: WS nochmal prüfen, dann letzter REST-Check
```

**Warum WS-First?**
- WS-Fill-Events kommen in ~50-100ms
- REST-Poll dauert 200-500ms pro Roundtrip
- WS reduziert die Gesamtlatenz pro Chunk um ~1.7s

### 6.4 Step 2c: Partial-Fill + Cancel Race Condition

Wenn der Maker nur partiell gefüllt ist und gecancelt wird, gibt es ein Zeitfenster zwischen "Cancel gesendet" und "Cancel verarbeitet", in dem weitere Fills ankommen können:

```
Zeitlinie:
  t0: Maker partial fill erkannt (0.9 SOL)
  t1: Cancel-Request gesendet
  t2: Weiterer Fill kommt an (0.1 SOL) ← vor Cancel-Verarbeitung
  t3: Cancel verarbeitet (order already fully filled)

Ohne Recheck: Bot denkt Maker hat 0.9, hedgt nur 0.9 → 0.1 SOL Imbalance
Mit Recheck:  Bot wartet 300ms, prüft nochmal → sieht 1.0, hedgt 1.0 ✓
```

**Code:**
```python
await maker_client.async_cancel_order(str(maker_order_id))
await asyncio.sleep(0.3)  # Warten auf Exchange-Verarbeitung
final_check = await maker_client.async_check_order_fill(str(maker_order_id))
if final_qty > maker_filled_qty:
    maker_filled_qty = final_qty  # Korrektur nach oben
```

### 6.5 Step 2d: Post-Cancel Fill-Check

Auch wenn der Cancel "erfolgreich" war, könnte der Order zwischenzeitlich gefüllt worden sein (Race Condition auf Exchange-Seite):

```python
# Nach Cancel: War der Order vielleicht doch schon gefüllt?
post_cancel = await maker_client.async_check_order_fill(str(maker_order_id))
if post_cancel.get("filled") or post_cancel_qty > 0:
    maker_filled_qty = post_cancel_qty
    break  # → weiter zum Taker-Hedge

# Auch WS-Events checken (zweiter Kanal)
ws_qty = self._get_ws_filled_qty(str(maker_order_id))
if ws_qty > 0:
    maker_filled_qty = ws_qty
    break
```

### 6.6 Step 3: Taker IOC Hedge

Sobald der Maker gefüllt ist, wird **sofort** (ohne Delay) eine IOC-Order auf der Taker-Exchange platziert:

```python
# Preis: Best Ask/Bid + 50 Ticks Buffer
# Der Buffer ist absichtlich groß — bei IOC zahlt man den bestmöglichen
# Preis, nicht den Limit-Preis. Der Buffer stellt nur sicher, dass
# genug Liquidity gesweept wird.
taker_price = taker_best + taker_tick * 50  # buy-side
taker_price = taker_best - taker_tick * 50  # sell-side
```

**Warum 50 Ticks Buffer?**
Der IOC-Order füllt zum bestmöglichen Preis im Buch, nicht zum Limit. Der Limit-Preis definiert nur die äußerste Grenze. 50 Ticks geben genug Spielraum bei volatilen Märkten, ohne realistisch erreicht zu werden.

### 6.7 Step 3b: Taker-Fill Erkennung (`_check_taker_fill`)

**Code:** `state_machine.py` Zeile 1216–1270

IOC-Orders füllen sofort — die Fill-Info kommt schneller als bei Maker-Orders:

```
1. Direkte Response prüfen (traded_qty > 0?)          ← Manche Exchanges liefern sofort
2. Status "FILLED"/"success" → REST-Check              ← Nado z.B.
3. WS-Fill-Events sofort prüfen                        ← Instant Check
4. 500ms auf WS-Event warten (100ms Intervalle)        ← WS-Timeout
5. REST-Fallback (async_check_order_fill)              ← Letzter Versuch
```

**Warum ist die Reihenfolge anders als beim Maker?**
- IOC ist sofort oder nie → kein langes Warten nötig
- Manche Exchanges (Extended) geben `traded_qty: 0` in der Response zurück, obwohl gefüllt (Fill kommt erst per WS)
- 500ms WS-Timeout statt 5000ms beim Maker

---

## 7. Exit-Flow (`execute_exit`)

**Code:** `state_machine.py` Zeile 221–253

Identisch zum Entry, aber:
- State muss `HOLDING` sein
- Config hat `reduce_only: True` → Orders können Positionen nur reduzieren, nicht erhöhen
- Maker-/Taker-Seiten sind vertauscht (sell statt buy, buy statt sell)
- Bei reduce_only: Chunk-Größe wird an verbleibende Maker-Position gekappt

**Ergebnis-Handling:**
- **Erfolg / Positionen bei 0:** `EXITING → IDLE`
- **Maker gefüllt, Taker nicht:** Emergency Unwind → prüfe ob Position jetzt bei 0
- **Partiell:** `EXITING → HOLDING` — restliche Position bleibt, kann erneut geexited werden

---

## 8. Persistenz: State auf Disk

**Code:** `state_machine.py` Zeile 282–328

Position wird nach jedem Entry/Exit auf Disk gespeichert (`data/bots/{bot_id}/position.json`):

```json
{
  "state": "HOLDING",
  "long_qty": 10.0,
  "short_qty": -10.0,
  "long_exchange": "extended",
  "short_exchange": "grvt",
  "long_symbol": "SOL-PERP",
  "short_symbol": "SOL_USDT_Perp",
  "long_entry_price": 142.35,
  "short_entry_price": 142.41
}
```

**Beim Container-Restart:**
1. `load_state()` liest die Datei — nur `HOLDING` wird wiederhergestellt
2. `sync_position_from_exchange()` fragt die echten Positionen ab und überschreibt die gespeicherten Werte
3. Wenn beide Positionen 0 sind → automatisch `IDLE` setzen (Position wurde extern geschlossen)

---

## 9. VWAP Entry-Price Berechnung

**Code:** `state_machine.py` Zeile 379–407

Nach dem TWAP werden die Durchschnittspreise über alle Chunks berechnet:

```
maker_vwap = Σ(chunk_qty × chunk_price) / Σ(chunk_qty)
taker_vwap = Σ(chunk_qty × chunk_price) / Σ(chunk_qty)
```

Diese werden als `_long_entry_price` und `_short_entry_price` gespeichert und in der UI als PnL-Referenz verwendet.
