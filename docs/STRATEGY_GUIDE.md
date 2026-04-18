# Strategie-Dokument: Delta-Neutral Funding Arbitrage

## Was macht der Bot?

Der Bot verdient Geld durch **Funding-Rate-Differenzen** zwischen zwei Exchanges. Er geht gleichzeitig auf einer Exchange Long und auf einer anderen Short — auf dasselbe Asset. Da die Positionen sich gegenseitig absichern (Delta-Neutral), spielt die Preisbewegung des Assets keine Rolle. Der Gewinn kommt aus dem **Funding**, das zwischen den Exchanges unterschiedlich hoch ist.

**Beispiel**: SOL auf Extended zahlt +0.01% Funding pro Stunde (Longs zahlen an Shorts), auf Variational nur +0.003%. Wer auf Variational Long geht und auf Extended Short, kassiert die Differenz: 0.007% pro Stunde ≈ 61% APR.

---

## Grundprinzipien

### 1. Delta-Neutralität

Zu jeder Zeit gilt: **Long-Position ≈ Short-Position** (gleiche Größe, gegensätzliche Richtung). Wenn SOL um 10% steigt, gewinnt die Long-Seite und verliert die Short-Seite — in Summe ist der PnL aus Preisbewegung nahe null. Der Gewinn entsteht allein aus den Funding-Zahlungen.

### 2. Maker-Taker-Prinzip

Jede Position wird über zwei Seiten eröffnet:

- **Maker-Seite**: Platziert Limit Orders (Post-Only) → zahlt weniger oder gar keine Gebühren
- **Taker-Seite**: Reagiert mit Market Orders (IOC) sobald der Maker gefüllt ist → zahlt Taker-Gebühren, aber sichert sofort ab

Wer Maker und wer Taker ist, wird über die Konfiguration festgelegt (`maker_exchange`). In der Praxis ist es sinnvoll, die Exchange mit dem größeren Orderbook als Maker zu wählen, weil Limit Orders dort schneller gefüllt werden.

### 3. TWAP-Ausführung

Statt die gesamte Position auf einmal zu eröffnen, wird sie in **Chunks** aufgeteilt (Time-Weighted Average Price). Das reduziert Market Impact und Slippage.

**Beispiel**: 10 SOL in 5 Chunks à 2 SOL, alle 30 Sekunden.

---

## Der Spread: Was er bedeutet und wie er gemessen wird

### Was ist der Spread?

Der Spread ist die **Preisdifferenz** zwischen der Long-Exchange und der Short-Exchange. Er bestimmt, wie günstig oder teuer der Einstieg in die Position ist.

```
Spread = (Long-Ask - Short-Bid) / Short-Bid × 100%
```

- **Negativer Spread** (z.B. -0.05%): Die Long-Seite ist günstiger als die Short-Seite → Einstieg ist profitabel, man kassiert die Differenz sofort
- **Spread = 0%**: Beide Exchanges handeln gleich → kostenneutraler Einstieg
- **Positiver Spread** (z.B. +0.08%): Die Long-Seite ist teurer → man zahlt drauf beim Einstieg und muss diese Kosten über Funding wieder reinholen

### BBO-Spread vs. Execution-Spread

Es gibt zwei Arten den Spread zu messen:

**BBO-Spread** (Best Bid/Offer):
Vergleicht nur den besten Preis auf jeder Seite. Das ist die klassische Messung, die man auf jeder Exchange sieht. Problem: Auf dünnen DEX-Orderbooks (z.B. Nado, Variational) liegt hinter dem besten Preis oft wenig Liquidität. Wenn man 10 SOL kaufen will, aber auf dem besten Level nur 2 SOL liegen, frisst man sich durch mehrere Levels und zahlt deutlich mehr.

**Execution-Spread** (VWAP-basiert, Opt-in):
Simuliert den tatsächlichen Fill-Preis über das gesamte Orderbook für die gewünschte Menge. Addiert man alle Fills gewichtet auf, erhält man den **Volume-Weighted Average Price** (VWAP) — den realistischen Preis, den man wirklich zahlen würde.

```
Beispiel: 10 SOL kaufen

Orderbook Ask-Seite:
  Level 1: 150.00 × 3 SOL
  Level 2: 150.05 × 4 SOL
  Level 3: 150.15 × 5 SOL

BBO-Price:  150.00  (nur Level 1)
VWAP-Price: 150.06  (3×150.00 + 4×150.05 + 3×150.15) / 10

→ Slippage = 6 Cents pro SOL = 4 Basis Points
```

Die Differenz zwischen BBO und VWAP ist der **Slippage** — die zusätzlichen Kosten durch mangelnde Liquidität.

### Die Spread-Fenster

Der Bot arbeitet mit zwei Spread-Grenzen:

| Parameter | Typischer Wert | Bedeutung |
|-----------|---------------|-----------|
| **min_spread_pct** | -0.5% | Untergrenze. Unter diesem Wert ist der Spread so negativ (Long viel günstiger), dass etwas nicht stimmt — möglicherweise stale Daten oder ein Flash-Event. Der Bot wartet. |
| **max_spread_pct** | +0.05% | Obergrenze. Darüber wird der Einstieg zu teuer — die Funding-Einnahmen würden lange brauchen um die Einstiegskosten zu kompensieren. Der Bot wartet. |

**Visualisierung**:

```
← Long günstiger              Long teurer →
━━━━━━━━┿━━━━━━━━━━━━━━━━━━━━━━┿━━━━━━━━━
     -0.5%    HANDELN OK    +0.05%
   min_spread              max_spread
```

Innerhalb dieses Fensters wird die Order platziert. Außerhalb wartet der Bot alle 2 Sekunden und prüft erneut.

---

## Positionseröffnung: Schritt für Schritt

### Phase 1: Vorbereitung

Bevor der erste Trade abgesetzt wird, passiert folgendes:

1. **Positions-Check**: Sind auf den beiden Exchanges bereits offene Positionen für dieses Symbol? Falls ja → Abbruch mit Fehlermeldung.

2. **Leverage setzen**: Auf beiden Exchanges wird der gewünschte Hebel eingestellt.

3. **Risiko-Prüfung**: Der RiskManager prüft für beide Seiten, ob die Order platziert werden darf (z.B. Orderbook-Tiefe, Circuit-Breaker).

4. **Spread-Vorprüfung**: Einmaliger BBO-Spread-Check. Das Ergebnis wird geloggt, blockiert aber nicht den Entry — die eigentliche Spread-Kontrolle findet pro Chunk statt.

5. **Dynamische Positionsgröße** (optional): Wenn aktiviert, wird die Positionsgröße automatisch berechnet. Drei Limits werden parallel ermittelt, das kleinste gewinnt:

   - **Kapitallimit**: Wie viel kann ich mir mit meinem Collateral und Hebel leisten?
   - **Pair-Limit**: Maximal X% des Kapitals in einem einzelnen Pair
   - **Liquiditätslimit**: Wie viel kann ich handeln, ohne zu viel Slippage zu erzeugen?

   Das Liquiditätslimit wird über eine **Binary Search** ermittelt: Der Algorithmus probiert verschiedene Mengen durch und prüft jeweils, ob der simulierte VWAP-Fill-Preis innerhalb des Slippage-Budgets liegt.

### Phase 2: TWAP-Ausführung (Chunk für Chunk)

Die Gesamtmenge wird in N Chunks aufgeteilt. Für jeden Chunk:

**Schritt A — Spread-Prüfung**

Vor jeder Order wird der aktuelle Spread geprüft. Dabei gibt es zwei aufeinanderfolgende Gates:

1. **Depth-Spread Gate** (optional): Simuliert den VWAP-Fill-Preis für die verbleibende Chunk-Menge auf beiden Seiten. Wenn der erwartete Slippage über dem Budget liegt (z.B. > 10 Basis Points), wartet der Bot 2 Sekunden und prüft erneut. Das verhindert, dass man in ein dünnes Orderbook hineinhandelt.

2. **BBO-Spread Gate**: Vergleicht Long-Ask mit Short-Bid. Der Spread muss innerhalb des konfigurierten Fensters liegen (min_spread_pct bis max_spread_pct). Außerhalb wartet der Bot erneut.

Beide Gates müssen bestanden werden, bevor die Order platziert wird.

**Schritt B — Maker Order**

Eine Post-Only Limit Order wird auf der Maker-Exchange platziert. Der Preis wird relativ zum aktuellen BBO berechnet:

- Bei **Buy (Long)**: Preis = Best Bid + Offset Ticks
- Bei **Sell (Short)**: Preis = Best Ask - Offset Ticks

Der Offset sorgt dafür, dass die Order knapp innerhalb des Spreads liegt — aggressiv genug um gefüllt zu werden, aber als Limit Order mit Maker-Gebühren.

Falls die Order nicht gefüllt wird, wird sie nach einem Timeout storniert und mit einem angepassten Preis neu platziert (**Repricing**). Dieser Zyklus wiederholt sich bis zur maximalen Anzahl von Chase-Runden.

**Taker-Drift-Guard** (optional): Während der Maker-Order auf Fill wartet, läuft ein paralleler Monitor, der alle ~1 Sekunde den Taker-Mid-Price prüft. Wenn der Taker-Kurs seit dem Maker-Placement mehr als den konfigurierten Schwellwert driftet (Default: 3 Basis Points), wird die Maker-Order **proaktiv gecancelt** und die Spread-Gates werden komplett neu evaluiert. Das verhindert, dass ein Fill auf der Maker-Seite zu einem ungünstigen Hedge auf der Taker-Seite führt.

**Schritt C — Taker Hedge**

Sobald die Maker-Order (teilweise) gefüllt ist, feuert sofort die Gegenseite:

- Eine **IOC (Immediate-Or-Cancel)** Market Order auf der Taker-Exchange
- Die Menge entspricht dem, was auf der Maker-Seite gefüllt wurde
- Preis: BBO ± großzügiger Slippage-Buffer (50 Ticks), um sicher gefüllt zu werden

Die Fill-Erkennung läuft primär über den **WebSocket Account-Stream** (Latenz < 100ms) mit REST als Fallback.

**Schritt D — Positions-Verifikation & Repair**

Nach jedem Chunk prüft der Bot die **tatsächlichen Positionen** auf beiden Exchanges per REST-API:

```
Erwartung:  Maker +2.0 SOL, Taker -2.0 SOL → Gap = 0
Realität:   Maker +2.0 SOL, Taker -1.8 SOL → Gap = 0.2 SOL
```

Falls ein Gap existiert (Taker hat weniger als Maker), wird ein **Repair-IOC** auf der Taker-Seite geschickt, um die Differenz auszugleichen. Mehrere Sicherheitsmechanismen verhindern Endlosschleifen:

- **Taker-Oversized-Check**: Wenn die Taker-Seite bereits *mehr* hat als der Maker, wird kein Repair durchgeführt — sonst snowballt die Differenz
- **Sanity Cap**: Repair darf maximal 2× die Chunk-Größe betragen
- **Position-Query mit Force-REST**: Während Repairs werden Positionen immer direkt per REST abgefragt, nie aus dem WS-Cache (der veraltet sein könnte)

**Schritt E — Inter-Chunk Pause**

Zwischen den Chunks wird die konfigurierte TWAP-Pause eingehalten (z.B. 30 Sekunden). In dieser Zeit:

- Kann der User den Bot pausieren oder stoppen
- Kühlt sich das Orderbook ab (neue Liquidität fließt nach)
- Reduziert sich der Market Impact

### Phase 3: Abschluss

Nach dem letzten Chunk:

- **VWAP Entry Prices** werden über alle Chunks berechnet
- **Position wird auf Disk persistiert** (überlebt Container-Neustarts)
- **State wechselt zu HOLDING**
- Falls nicht alle Chunks die Ziel-Menge erreicht haben, werden **Extra-Chunks** nachgelegt (maximal 20 zusätzliche)

---

## Positionsschließung (Exit)

Der Exit-Flow ist spiegelbildlich zum Entry:

- Maker-Seite **verkauft** statt zu kaufen (oder umgekehrt)
- Taker-Seite **kauft** statt zu verkaufen
- `reduce_only`-Flag ist aktiv → Orders können Positionen nur verkleinern, nie vergrößern
- Vor jedem Chunk wird die **aktuelle Maker-Position** abgefragt und die Chunk-Größe gedeckelt — man kann nicht mehr verkaufen als man hat

---

## Die Holding-Phase: Was passiert im Hintergrund

Während der Bot die Position hält, läuft im Hintergrund:

### Orderbook-Monitoring

Die Orderbook-Daten fließen kontinuierlich — entweder direkt per WebSocket zu den Exchanges oder in Echtzeit über den OMS per WebSocket (<10ms Latenz). Daraus werden berechnet:

- **Live-Preise** (Best Bid/Ask/Mid) für beide Seiten
- **OHI** (Orderbook Health Index): Wie gesund ist das Orderbook? (Spread, Tiefe, Symmetrie)
- **Price Spread**: Aktuelle Preisdifferenz zwischen den Exchanges

### Funding-Monitoring

Der FundingMonitor pollt regelmäßig die aktuellen Funding-Rates beider Exchanges und berechnet:

- **Funding Spread**: Differenz der Rates → das ist der Gewinn pro Funding-Intervall
- **Annualisiert**: Hochgerechnet auf ein Jahr
- **Empfehlung**: Welche Seite sollte Long, welche Short sein

Optional (V4 Funding History): Historische Daten von einer externen API zeigen, wie **stabil** der Funding-Spread über die Zeit war.

### Position-Tracking

Der Bot hält den aktuellen Positions-State im RAM und auf Disk:

```
Position:
  Long:   Extended  SOL-USDC    +10.0 SOL @ 150.03
  Short:  Variational P-SOL-USDC  -10.0 SOL @ 150.07
  Net Delta: 0.0 SOL (delta-neutral)
```

Bei einem Container-Neustart wird der State von Disk wiederhergestellt. Die Exchange-Positionen bleiben unabhängig vom Bot bestehen.

---

## Zusammenspiel der Spread-Guards

Die verschiedenen Spread-Prüfungen greifen auf unterschiedlichen Ebenen:

```
                    Entry/Exit ausgelöst
                           │
                           ▼
            ┌──────────────────────────┐
            │  Depth-Spread Gate       │  ← Opt-in (fn_opt_depth_spread)
            │  "Ist genug Liquidität   │
            │   vorhanden für meine    │
            │   Order-Größe?"          │
            │                          │
            │  Misst: VWAP-Slippage    │
            │  Budget: max_slippage_bps│
            │  Bei Überschreitung:     │
            │  warte 2s, prüfe erneut  │
            └────────────┬─────────────┘
                         │ OK
                         ▼
            ┌──────────────────────────┐
            │  BBO-Spread Gate         │  ← Immer aktiv
            │  "Ist der Preisunterschied│
            │   zwischen den Exchanges │
            │   akzeptabel?"           │
            │                          │
            │  Misst: long_ask vs      │
            │         short_bid        │
            │  Fenster: [min, max]     │
            │  Bei Überschreitung:     │
            │  warte 2s, prüfe erneut  │
            └────────────┬─────────────┘
                         │ OK
                         ▼
                  Order wird platziert
```

Das Depth-Spread Gate prüft die **Machbarkeit** (genug Liquidität), das BBO-Spread Gate prüft die **Wirtschaftlichkeit** (Preis günstig genug). Beide werden vor jedem einzelnen Maker-Order innerhalb eines Chunks geprüft — nicht nur einmal pro Chunk.

---

## Wann lohnt sich ein Trade?

### Die Rechnung

```
Profit = Funding-Einnahmen - Einstiegskosten - Gebühren

Funding-Einnahmen = Positions-Größe × Funding-Spread × Haltezeit
Einstiegskosten   = Positions-Größe × Entry-Spread (+ Exit-Spread)
Gebühren           = Maker-Fee + Taker-Fee (Entry + Exit)
```

### Beispiel

```
SOL: 10 SOL Position, Hebel 5×

Funding-Spread:     0.007% pro Stunde
Haltezeit:          24 Stunden
→ Funding-Gewinn:   10 × 150 × 0.00007 × 24 = $2.52

Entry-Spread:       +0.02%
Exit-Spread:        +0.02%
→ Einstiegskosten:  10 × 150 × 0.0002 × 2 = $0.60

Gebühren:           ~0.05% Taker × 2 Seiten × 2 (Entry+Exit)
→ Gebühren:         10 × 150 × 0.0005 × 4 = $3.00

Netto:              $2.52 - $0.60 - $3.00 = -$1.08 (Verlust!)
```

In diesem Beispiel lohnt es sich **nicht** für 24 Stunden. Aber bei:
- Höherem Funding-Spread (0.02%/h statt 0.007%)
- Längerer Haltezeit (1 Woche)
- Niedrigeren Fees (Maker statt Taker auf beiden Seiten)
- Besserem Entry-Spread (negativ = man wird sogar bezahlt)

... wird es profitabel. Die V4 Funding History hilft einzuschätzen, ob der aktuelle Spread historisch stabil ist oder nur ein kurzfristiger Ausreißer.

---

## Risikomanagement

### Immer-aktive Sicherungen

| Mechanismus | Was es schützt |
|-------------|---------------|
| **Delta-Verifikation** | Nach jedem Chunk werden die echten Exchange-Positionen abgefragt. Gap > Minimum → automatischer Repair |
| **Taker-Oversized-Guard** | Verhindert, dass Repairs die Taker-Seite über die Maker-Seite hinaus aufblasen |
| **Reduce-Only bei Exit** | Während dem Schließen können Orders die Position nur verkleinern, nie vergrößern oder umdrehen |
| **Sanity Cap** | Repair maximal 2× Chunk-Größe |
| **Extra-Chunk-Cap** | Maximal 20 zusätzliche Chunks bei Teilfüllungen |
| **Circuit Breaker** | Stoppt den Bot bei kumulativem Verlust über Schwellenwert |

### Optionale Sicherungen

| Feature | Was es macht |
|---------|-------------|
| **Depth Spread** | Verhindert Einstieg bei zu dünnem Orderbook |
| **OHI Monitoring** | Zeigt die Orderbook-Gesundheit im Dashboard |
| **V4 Funding History** | Warnt bei historisch instabilem Funding-Spread |
| **Dynamic Sizing** | Begrenzt die Positionsgröße auf das, was das Orderbook hergibt |
| **Taker Drift Guard** | Cancelt Maker-Order wenn Taker-Preis während Wartezeit driftet (Default: 3 bps) |

---

## Glossar

| Begriff | Erklärung |
|---------|-----------|
| **BBO** | Best Bid/Offer — der beste Kauf-/Verkaufspreis im Orderbook |
| **VWAP** | Volume-Weighted Average Price — mengengewichteter Durchschnittspreis |
| **Slippage** | Differenz zwischen erwartetem und tatsächlichem Ausführungspreis |
| **Basis Point (bps)** | 1/100 eines Prozents (0.01%) |
| **IOC** | Immediate-Or-Cancel — Market Order die sofort ausgeführt oder storniert wird |
| **Post-Only** | Limit Order die garantiert als Maker ins Orderbook geht |
| **Funding Rate** | Periodische Zahlung zwischen Long- und Short-Haltern auf Perpetual-Futures |
| **Delta-Neutral** | Long + Short gleicher Größe → kein Exposure zur Preisbewegung |
| **TWAP** | Time-Weighted Average Price — Aufteilung einer großen Order in zeitversetzte Chunks |
| **OHI** | Orderbook Health Index — Qualitätsmetrik für ein Orderbook (0-1) |
| **OMS** | Orderbook Management Service — zentraler Service der alle Orderbook-Feeds sammelt |
| **Maker** | Die Seite die Limit Orders platziert (niedrigere Gebühren) |
| **Taker** | Die Seite die Market Orders feuert (höhere Gebühren, aber sofortige Ausführung) |
| **Repair** | Automatische Nachbesserung wenn Maker- und Taker-Fills auseinanderlaufen |
| **Chase** | Wiederholtes Stornieren und Neuplatzieren einer Limit Order zu einem besseren Preis |
