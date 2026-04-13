# TradeAutonom — Bot-Anleitung für Endbenutzer

## Was ist Funding-Arbitrage?

Perpetual Futures auf dezentralen Börsen (DEXs) verwenden einen **Funding-Rate-Mechanismus**, um den Futures-Preis an den Spot-Preis zu binden. Wenn der Futures-Preis über dem Spot liegt, zahlen Long-Positionen an Short-Positionen (positive Funding Rate) — und umgekehrt.

**Funding-Arbitrage** nutzt diese Zahlungen aus, indem gleichzeitig eine Long-Position auf einer Börse und eine Short-Position auf einer anderen Börse eröffnet wird. Die Positionen sind **delta-neutral** — Preisbewegungen heben sich gegenseitig auf. Der Gewinn kommt aus der Differenz der Funding Rates zwischen den beiden Börsen.

### Beispiel

| Börse | Position | Funding Rate (p.a.) |
|-------|----------|---------------------|
| Extended | LONG ETH | -5% (erhält 5%) |
| GRVT | SHORT ETH | +3% (zahlt 3%) |

**Netto-Ertrag**: 5% - 3% = **2% p.a.** auf das eingesetzte Kapital — bei minimalem Preisrisiko.

---

## Wie der Bot arbeitet

### Lebenszyklus

```
IDLE → [Start] → ENTERING → HOLDING → [Stop/Timer] → EXITING → IDLE
```

1. **IDLE**: Kein aktiver Trade. Bot wartet auf Startbefehl.
2. **ENTERING**: Bot eröffnet die delta-neutrale Position (Long + Short gleichzeitig).
3. **HOLDING**: Position ist offen. Bot sammelt Funding-Erträge. Dashboard zeigt PnL, Funding und Positionen in Echtzeit.
4. **EXITING**: Bot schließt beide Positionen gleichzeitig.
5. **IDLE**: Zurück im Ausgangszustand.

### Bot starten

Beim Start konfigurierst du:

| Parameter | Beschreibung | Beispiel |
|-----------|-------------|----------|
| **Long Exchange** | Börse für die Long-Position | Extended |
| **Short Exchange** | Börse für die Short-Position | GRVT |
| **Instrument** | Welches Asset gehandelt wird | ETH-USD / ETH_USDT_Perp |
| **Quantity** | Positionsgröße (in Token) | 20 ETH |
| **Leverage** | Hebel pro Börse | 25x / 25x |
| **Duration** | Wie lange der Bot läuft | 4h 0m |
| **TWAP Chunks** | In wie viele Teilorders die Position aufgeteilt wird | 10 |

### Timer

- Der Timer startet **nach** erfolgreichem Entry.
- Wenn der Timer abläuft, wird automatisch ein Exit ausgelöst.
- Du kannst den Timer jederzeit anpassen oder auf unbegrenzt setzen (0h 0m).
- Der Timer überlebt Container-Neustarts — bei einem Neustart wird der verbleibende Countdown fortgesetzt.

---

## Order Management

### Maker-Taker TWAP-Strategie

Der Bot verwendet eine **Maker-Taker-Strategie** mit **TWAP** (Time-Weighted Average Price), um Positionen möglichst kosteneffizient zu eröffnen und zu schließen.

#### Warum Maker-Taker?

- **Maker-Orders** (Post-Only Limit) zahlen niedrigere oder sogar negative Gebühren.
- **Taker-Orders** (IOC Market) sind teurer, aber garantieren sofortige Ausführung.
- Durch die Kombination beider wird eine Seite günstig per Maker gefüllt, die andere sofort per Taker gehedgt — so ist die Position jederzeit delta-neutral.

#### Ablauf pro Chunk

```
1. Orderbook lesen → besten Preis ermitteln
2. Maker Post-Only Order platzieren (z.B. SELL auf Extended)
3. Warten auf Fill (mit Chase-Logik bei Preisbewegung)
4. Sobald Maker gefüllt → Taker IOC Hedge (z.B. BUY auf GRVT)
5. Positionen verifizieren → bei Imbalance: Repair-IOC
```

#### Chase-Logik (Maker)

Wenn der Marktpreis sich bewegt und die Maker-Order nicht gefüllt wird:

1. Order wird automatisch storniert.
2. Neues Orderbook wird abgerufen.
3. Order wird zum neuen besten Preis erneut platziert.
4. Dies wiederholt sich, bis die Order gefüllt wird oder der Bot gestoppt wird.

#### Taker-Hedge

Nach jedem Maker-Fill wird sofort eine **IOC (Immediate-Or-Cancel)**-Order auf der Gegenböse platziert:

- Nutzt den aktuellen Orderbook-Preis plus Buffer (50 Ticks).
- Wird sofort ausgeführt oder storniert — kein Warten.
- Garantiert, dass die Position delta-neutral bleibt.

#### TWAP (Aufteilung in Chunks)

Die Gesamtmenge wird in mehrere kleinere Orders aufgeteilt:

- **Vorteil**: Weniger Market Impact, bessere Durchschnittspreise.
- **Interval**: Zwischen jedem Chunk wird eine konfigurierbare Pause eingelegt (Standard: 10s).
- Beispiel: 20 ETH in 10 Chunks = 2 ETH pro Chunk, alle 10 Sekunden.

---

## Risiko-Management

### Spread Guard

Vor jedem Chunk prüft der Bot den **Cross-Exchange-Spread** (Preisdifferenz zwischen den beiden Börsen):

- Wenn der Spread zu groß ist (konfigurierbar: z.B. max $1 oder 0.05%), wartet der Bot, bis sich der Spread normalisiert.
- Verhindert Einstiege zu ungünstigen Preisen.

### Pre-Trade Checks

Vor jeder Order werden automatisch geprüft:

| Check | Beschreibung |
|-------|-------------|
| **Circuit Breaker** | Wenn der kumulierte Verlust einen Schwellwert überschreitet (z.B. $500), wird der Handel gestoppt. |
| **Min Order Size** | Die Ordergröße muss die Mindestgröße der Börse erreichen. |
| **Orderbook Sync** | Das Orderbook muss über WebSocket synchronisiert sein. |
| **Liquiditätsprüfung** | Genügend Liquidität in den Top-10-Levels des Orderbooks. |

### Position Verification & Repair

Nach jedem Chunk verifiziert der Bot die tatsächlichen Positionen auf beiden Börsen:

1. **Sofortige Prüfung** (0.5s nach Chunk): Abfrage der realen Positionsgrößen.
2. **Verzögerte Prüfung** (3s nach Chunk): Erneute Prüfung, um späte Fills zu erfassen.
3. **Repair-Mechanismus**: Wenn ein Ungleichgewicht erkannt wird (Maker gefüllt, aber Taker nicht vollständig), wird automatisch eine Repair-IOC-Order auf der Taker-Seite platziert.

### Circuit Breaker

- Überwacht den kumulierten PnL aller Trades.
- Wenn der Verlust den konfigurierten Schwellwert überschreitet, wird der Handel automatisch gestoppt.
- Manueller Reset möglich.

### Delta-Überwachung

- Das Net Delta (Long-Menge + Short-Menge) sollte immer nahe 0 sein.
- Wird im Dashboard angezeigt.
- Größere Abweichungen deuten auf ein Problem hin (z.B. fehlgeschlagener Taker-Hedge).

---

## Notfall-Aktionen

### Stop (Graceful)

- Storniert den Timer.
- Führt einen vollständigen Exit durch (Maker-Taker TWAP wie beim Entry, aber in umgekehrter Richtung).
- Positionen werden sauber geschlossen.

### Kill (Emergency)

Wenn etwas schief geht:

- Storniert **sofort** alle laufenden Operationen (TWAP-Loop, Timer).
- Storniert alle offenen Orders auf allen Börsen.
- Setzt den State auf IDLE.
- **Positionen bleiben offen** — müssen manuell auf den Börsen geschlossen werden.

### Reset

- Setzt den internen State auf IDLE zurück.
- Für den Fall, dass Positionen bereits manuell auf den Börsen geschlossen wurden.
- Kein Trading — nur State-Reset.

---

## Dashboard-Metriken

| Metrik | Beschreibung |
|--------|-------------|
| **Total PnL** | Gesamter Netto-Gewinn/Verlust aller geschlossenen Positionen |
| **Point Factor** | Punkte pro $100K Handelsvolumen (Points-Effizienz) |
| **Active Bots** | Anzahl laufender / gesamter Bots |
| **Most Traded** | Top 3 gehandelte Token nach Volumen |
| **Paid Fees** | Gesamte gezahlte Handelsgebühren |
| **Paid Funding** | Gesamte erhaltene/gezahlte Funding-Zahlungen |
| **Avg Hold Time** | Durchschnittliche Haltedauer geschlossener Positionen |
| **Delta Neutral Factor** | Wie erfolgreich Positionen delta-neutral geschlossen wurden (>100% = beide Legs im Plus) |

---

## Tipps

1. **Funding Rates prüfen**: Starte den Bot nur, wenn eine signifikante Funding-Rate-Differenz zwischen den Börsen besteht.
2. **Kleine Positionen zuerst**: Teste mit kleinen Mengen, bevor du größere Positionen eröffnest.
3. **Timer nutzen**: Setze immer einen Timer, um sicherzustellen, dass Positionen automatisch geschlossen werden.
4. **Spread beachten**: Ein hoher Spread beim Entry kann den gesamten Funding-Ertrag auffressen.
5. **Leverage**: Höherer Hebel = weniger Margin benötigt, aber höheres Liquidationsrisiko bei extremen Preisbewegungen.
