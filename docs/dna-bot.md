# DNA Bot — Delta-Neutral Arbitrage

## Überblick

Der DNA Bot erkennt Preisunterschiede desselben Tokens zwischen zwei Börsen und eröffnet gegenläufige Positionen (Long + Short), um die Preisdifferenz als Gewinn zu sichern. Die Position ist **delta-neutral**: Preisbewegungen des Tokens haben keinen Einfluss auf den Gewinn, da beide Seiten sich gegenseitig absichern. Der Gewinn entsteht ausschließlich aus dem Spread bei Eröffnung minus dem Spread bei Schließung.

**Unterstützte Börsen:** Extended, GRVT, Nado (beliebige Kombination aus mindestens zwei)

---

## 1. Pre-Flight Checks

Bevor der Bot gestartet werden kann, werden Verbindungsprüfungen durchgeführt:

| Check | Was geprüft wird | Erforderlich |
|-------|-------------------|-------------|
| **Positions** | Können offene Positionen von der Börse abgefragt werden? | Ja |
| **Balance** | Kann der Kontostand / die Equity abgefragt werden? | Ja |
| **OMS Health** | Ist der Order Monitoring Service erreichbar und gesund? | Ja |
| **OMS Books** | Liefert der OMS Orderbuch-Daten für jede konfigurierte Börse? | Ja |

**Startbedingung:** Mindestens 2 Börsen müssen alle Checks bestehen + OMS Health muss OK sein.

Die Checks laufen mit einem Timeout von 10 Sekunden pro Börse und 8 Sekunden für den OMS.

---

## 2. Start-Prozess

Beim Start durchläuft der Bot folgende Schritte:

1. **Auto-Exclude:** Alle Tokens, die bereits offene Positionen auf den konfigurierten Börsen haben, werden automatisch ausgeschlossen (wenn `auto_exclude_open_positions` aktiv). Damit wird verhindert, dass der Bot eine zweite Position in einem Token eröffnet, in dem der User bereits manuell positioniert ist.

2. **Unified WebSocket starten:** Ein einziger Task verbindet sich per WebSocket mit dem OMS (`/ws/arb`). Über diese Verbindung laufen sowohl **Entry-Signale** (Arbitrage-Opportunities) als auch **Exit-Monitoring** (Spread-Updates für offene Positionen).

---

## 3. Signal-Erkennung (WebSocket)

### 3.1 Datenquelle

Der Bot empfängt Arbitrage-Signale in Echtzeit über eine WebSocket-Verbindung zum OMS (`/ws/arb`). Bei Verbindungsaufbau sendet der Bot ein `subscribe_opportunities`-Kommando mit optionalem Filter (Börsen, Mindest-Spread). Der OMS streamt daraufhin bei jedem Scan-Zyklus (~200ms) alle aktuellen Arbitrage-Opportunities als `arb_opportunity`-Nachrichten.

Der OMS vergleicht kontinuierlich die Orderbücher aller Börsen und liefert Signale, wenn ein Token auf einer Börse günstiger zu kaufen ist als auf einer anderen zu verkaufen.

Jedes Signal enthält:
- **Token** (z.B. BTC, ETH, HYPE)
- **Buy-Exchange + Symbol** (wo günstiger gekauft werden kann)
- **Sell-Exchange + Symbol** (wo teurer verkauft werden kann)
- **Buy-Preis / Sell-Preis** (Best Bid/Ask aus den Orderbüchern)
- **Net Profit BPS** (Nettogewinn in Basispunkten nach Gebühren)
- **Fee Threshold BPS** (Mindest-Spread, der die Gebühren beider Börsen deckt)
- **Max Qty** (maximale ausführbare Menge basierend auf Orderbuch-Tiefe)
- **Max Leverage** (pro Token UND pro Börse — z.B. BTC kann auf Extended 50x haben, auf Nado 20x)
- **Min Order Size** (pro Börse — minimale Ordergröße in Base-Einheiten, z.B. Extended SOL=0.1, GRVT SOL=0.01)
- **Qty Step** (pro Börse — kleinste Mengen-Abstufung, auf die gerundet wird, z.B. Extended SOL=0.01)

Diese Market-Metadata (Leverage, Min Order Size, Qty Step) werden vom OMS bei der Auto-Discovery einmalig von den Börsen-APIs erfasst und in jedem Signal mitgeliefert:

| Börse | Leverage-Quelle | Min Order Size | Qty Step |
|-------|----------------|----------------|----------|
| **Extended** | `tradingConfig.maxLeverage` | `tradingConfig.minOrderSize` (Base Qty) | `tradingConfig.minOrderSizeChange` |
| **GRVT** | Fest 10x | `min_size` (Base Qty) | `min_size` (= Step) |
| **Nado** | `max_leverage` | `min_size` (x18, USD Notional) | `size_increment` (x18) |

### 3.2 Spread-Modi

Der Bot unterstützt drei Modi, die bestimmen, ab welchem Spread ein Signal als profitabel gilt:

| Modus | Schwellenwert | Bedeutung |
|-------|--------------|-----------|
| **Delta-Neutral** | Voller Fee-Threshold vom OMS | Nur Signale, die alle Gebühren beider Börsen vollständig decken |
| **Half-Neutral** | 50% des Fee-Thresholds | Akzeptiert Signale, die mindestens die Hälfte der Gebühren decken — aggressiver |
| **Custom** | Manuell konfigurierter BPS-Wert | Volle Kontrolle über den Mindest-Spread |

### 3.3 Signal-Filter

Ein Signal wird **verworfen**, wenn:

1. Die maximale Anzahl offener Positionen bereits erreicht ist
2. Bereits eine Position im selben Token mit derselben Richtung (Buy-Exchange + Sell-Exchange) existiert
3. Der Token auf der Ausschluss-Liste steht
4. Die beteiligten Börsen nicht in der Bot-Konfiguration enthalten sind
5. Der Spread unter dem konfigurierten Schwellenwert liegt (abhängig vom Spread-Modus)
6. Kein Exchange-Client für eine der beiden Börsen registriert ist (fehlende API-Keys)

---

## 4. Position eröffnen

### 4.1 Mengenberechnung

1. **Ziel-Menge:** `position_size_usd / mid_price` (Mittelpreis aus Buy und Sell)
2. **Cap auf OMS-Maximum:** Die Menge wird auf `max_qty` vom OMS begrenzt (basierend auf verfügbarer Orderbuch-Tiefe)
3. **Harmonisierung:** Die Menge wird auf die größere `qty_step` beider Börsen abgerundet (Werte kommen vom OMS-Signal), sodass beide Seiten die exakt gleiche Menge akzeptieren
4. **Minimum-Check:** Die harmonisierte Menge muss die `min_order_size` beider Börsen erreichen (Werte kommen vom OMS-Signal), andernfalls wird das Signal mit einem `qty_too_small`-Log übersprungen

> **Fallback:** Falls der OMS keine `min_order_size` oder `qty_step` liefert (Wert = 0), werden die Werte direkt vom Exchange-Client abgefragt (`get_min_order_size()`, `get_qty_step()`).

### 4.2 Leverage

Vor der ersten Order in einem Symbol wird automatisch der maximale Hebel gesetzt (einmalig pro Symbol+Börse). Der Wert kommt vom OMS-Signal (`buy_max_leverage`, `sell_max_leverage`).

### 4.3 Order-Ausführung

Beide Legs werden **gleichzeitig** per `asyncio.gather` abgesendet:

- **Buy-Leg:** Market-Order (IOC) auf der Börse mit dem niedrigeren Ask-Preis
- **Sell-Leg:** Market-Order (IOC) auf der Börse mit dem höheren Bid-Preis

Jede Leg-Ausführung läuft wie folgt:

1. **Market-Order absenden** an die Börse (synchron in einem Thread-Pool, um den Event-Loop nicht zu blockieren)
2. **Fill prüfen:** Wenn die Börse sofort einen Fill zurückmeldet → fertig
3. **Fill pollen:** Wenn kein sofortiger Fill (z.B. Extended meldet nur die Order-ID), wird bis zu 4× nachgefragt (nach 0,5s, 0,8s, 1,0s, 1,2s) ob die Order gefüllt wurde
4. **Ergebnis:** Entweder ein erfolgreicher Fill mit Menge + Preis, oder ein Fehler

### 4.4 Ergebnis-Auswertung

| Szenario | Aktion |
|----------|--------|
| **Beide Legs erfolgreich** | Position wird als "open" gespeichert. Mengen-Differenz wird geprüft (Toleranz konfigurierbar, Standard 5%). Die effektive Positionsgröße ist das Minimum beider Fills. |
| **Beide Legs fehlgeschlagen** | Kein Handlungsbedarf — keine offene Exposure. |
| **Ein Leg erfolgreich, ein Leg fehlgeschlagen** | Das erfolgreiche Leg wird sofort rückgängig gemacht (Unwind) durch eine entgegengesetzte Market-Order. Wenn der Unwind fehlschlägt, ist manuelle Intervention erforderlich. |

### 4.5 Simulations-Modus

Im Simulations-Modus werden keine echten Orders abgesendet. Stattdessen werden die aktuellen OMS-Preise als Fill-Preise verwendet und die Position wird sofort als erfolgreich eröffnet gewertet.

---

## 5. Position schließen

### 5.1 Exit-Monitor (automatisch)

Der Exit-Monitor läuft über dieselbe WebSocket-Verbindung wie die Signal-Erkennung. Für jede offene Position registriert der Bot ein `watch`-Kommando beim OMS, der daraufhin bei jeder Orderbuch-Änderung Echtzeit-Spread-Updates (`arb_status`/`arb_close`) für dieses spezifische Token+Börsen-Paar streamt.

**Schließ-Bedingung:** Eine Position wird automatisch geschlossen, wenn **beide** Kriterien erfüllt sind:

1. **Mindest-Haltezeit erreicht:** Die Position wurde mindestens so lange gehalten, wie in der Exit-Konfiguration festgelegt
2. **Spread unter Schwellenwert:** Der aktuelle Spread ist auf oder unter den konfigurierten `exit_threshold_bps` gefallen (d.h. die Arbitrage-Gelegenheit hat sich geschlossen)

### 5.2 Haltezeit-Modi

| Exit-Modus | Einheit | Standard |
|-----------|---------|---------|
| **Direct** | Minuten | 5 Minuten |
| **Hours** | Stunden | 8 Stunden |
| **Days** | Tage | 7 Tage |
| **Manual** | ∞ (nie automatisch) | — |

Die Haltezeit wird **beim Eröffnen** der Position in Sekunden berechnet und in der Position gespeichert. Spätere Konfigurationsänderungen wirken sich nicht auf bereits geöffnete Positionen aus.

### 5.3 Close-Ausführung

Das Schließen ist das inverse Spiegelbild des Eröffnens:

- **Ursprünglicher Buy-Leg → SELL** auf derselben Börse (gleiche Menge wie der ursprüngliche Fill)
- **Ursprünglicher Sell-Leg → BUY** auf derselben Börse (gleiche Menge wie der ursprüngliche Fill)

Beide Reverse-Legs werden **gleichzeitig** abgesendet (identisch zum Eröffnen).

| Szenario | Aktion |
|----------|--------|
| **Beide Legs erfolgreich** | Position wird als "closed" markiert. |
| **Ein oder beide Legs fehlgeschlagen** | Position bleibt als "open" markiert. Der Exit-Monitor wird beim nächsten Spread-Update erneut versuchen zu schließen. |

### 5.4 Manuelles Schließen

Über das Frontend kann jede offene Position manuell geschlossen werden — unabhängig von Haltezeit und Spread. Dabei wird derselbe Close-Mechanismus verwendet.

---

## 6. WebSocket-Verbindungsmanagement

Der Bot hält eine **einzige** persistente WebSocket-Verbindung zum OMS (`/ws/arb`), über die sowohl Entry-Signale als auch Exit-Monitoring laufen.

### 6.1 Verbindungsaufbau

Bei Verbindungsaufbau werden zwei Aktionen ausgeführt:
1. `subscribe_opportunities` — aktiviert den Opportunity-Stream (mit optionalem Filter für Börsen und Mindest-Spread)
2. `watch` für jede offene Position — aktiviert Spread-Monitoring für Exit-Erkennung

### 6.2 Nachrichten-Typen

| Typ | Richtung | Bedeutung |
|-----|----------|-----------|
| `subscribe_opportunities` | Bot → OMS | Opportunity-Stream starten (mit Filter) |
| `watch` | Bot → OMS | Spread-Monitoring für ein Paar starten |
| `unwatch` | Bot → OMS | Spread-Monitoring für ein Paar beenden |
| `arb_opportunity` | OMS → Bot | Neues Arbitrage-Signal (Entry) |
| `arb_status` | OMS → Bot | Spread-Update für beobachtetes Paar (profitable) |
| `arb_close` | OMS → Bot | Spread-Update: Arb nicht mehr profitabel (Exit-Trigger) |

### 6.3 Laufzeit-Verhalten

- **Neue Positionen:** Alle 5 Sekunden wird geprüft, ob neue offene Positionen hinzugekommen sind, die noch nicht beobachtet werden.
- **Geschlossene Positionen:** Nach dem Schließen wird ein `unwatch`-Befehl gesendet, wenn keine weiteren offenen Positionen in diesem Paar existieren.
- **Reconnect:** Bei Verbindungsabbruch wird mit exponentiellem Backoff (1s → 2s → 4s → ... → max 30s) neu verbunden. Nach Reconnect werden beide Subscriptions (Opportunities + Watches) automatisch wiederhergestellt.
- **Manueller Exit-Modus:** Positionen mit `exit_mode = "manual"` werden nicht beim Exit-Monitor registriert.

---

## 7. Zustandsverwaltung

### 7.1 Persistierung

| Datei | Inhalt | Wann gespeichert |
|-------|--------|-----------------|
| `data/dna_bot/{bot_id}/positions.json` | Alle Positionen (offen + geschlossen) | Nach jeder Zustandsänderung (open, close, close_failed) |
| `data/dna_bot/{bot_id}/config.json` | Bot-Konfiguration | Nur bei expliziter Config-Änderung über die API |

Beim Neustart werden Positionen und Konfiguration von der Festplatte geladen.

### 7.2 Activity Log

Der Bot führt ein In-Memory-Log der letzten 500 Aktionen. Dieses Log wird **nicht** auf die Festplatte geschrieben und geht bei einem Neustart verloren. Es dient der Echtzeit-Überwachung im Frontend.

Erfasste Ereignisse:
- `started` / `stopped` — Bot-Lifecycle
- `signal` — Eingehendes Arbitrage-Signal (mit Details)
- `position_opened` — Position erfolgreich eröffnet
- `position_closing` — Schließvorgang gestartet
- `position_closed` — Position erfolgreich geschlossen
- `position_close_failed` — Schließen fehlgeschlagen
- `entry_failed` — Beide Legs beim Eröffnen fehlgeschlagen
- `entry_partial_unwind` — Ein Leg fehlgeschlagen, erfolgreiches Leg rückgängig gemacht
- `qty_too_small` — Signal übersprungen wegen zu kleiner Menge
- `auto_exclude` — Token automatisch ausgeschlossen

---

## 8. Konfigurationsparameter

| Parameter | Standard | Beschreibung |
|-----------|---------|-------------|
| `position_size_usd` | 1.000 $ | Nominaler USD-Wert pro Position |
| `max_positions` | 3 | Maximale gleichzeitige offene Positionen |
| `spread_mode` | delta_neutral | Schwellenwert-Modus (delta_neutral / half_neutral / custom) |
| `custom_min_spread_bps` | 5,0 | Nur bei custom-Modus: manueller Mindest-Spread |
| `exchanges` | [extended, grvt, nado] | Aktive Börsen für Arbitrage |
| `simulation` | false | Paper-Trading-Modus |
| `slippage_tolerance_pct` | 0,5% | Maximale Slippage für IOC-Orders |
| `size_tolerance_pct` | 5,0% | Akzeptable Mengen-Differenz zwischen Buy und Sell |
| `exit_mode` | direct | Auto-Close-Modus (direct / hours / days / manual) |
| `exit_min_hold_minutes` | 5 | Mindest-Haltezeit im Direct-Modus |
| `exit_min_hold_hours` | 8 | Mindest-Haltezeit im Hours-Modus |
| `exit_min_hold_days` | 7 | Mindest-Haltezeit im Days-Modus |
| `exit_threshold_bps` | 0,01 | Spread-Schwelle für automatisches Schließen |
| `excluded_tokens` | [] | Manuell ausgeschlossene Tokens |
| `auto_exclude_open_positions` | true | Automatisch Tokens mit bestehenden Exchange-Positionen ausschließen |
| `tick_interval_s` | 0,5 | (Legacy) OMS Scan-Intervall — Opportunities werden jetzt per WebSocket gestreamt |

---

## 9. Ablaufdiagramm (Vereinfacht)

```
                    ┌─────────────────────┐
                    │   Bot Start          │
                    │   (Pre-Flight OK)    │
                    └──────────┬──────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │  Unified WebSocket  │
                    │  OMS /ws/arb        │
                    └──────────┬──────────┘
                               │
              ┌────────────────┼────────────────┐
              │ arb_opportunity                  │ arb_status / arb_close
              ▼                                 ▼
     ┌────────────────┐              ┌───────────────────┐
     │  Signal prüfen │              │  Spread-Update    │
     │  (Filter)      │              │  empfangen        │
     └───────┬────────┘              └───────┬───────────┘
             │ Pass                          │
             ▼                               ▼
     ┌────────────────┐              ┌───────────────────┐
     │  Qty berechnen │              │  Haltezeit OK?    │
     │  + harmonisieren│             │  Spread ≤ Thresh? │
     └───────┬────────┘              └───────┬───────────┘
             │                               │ Ja
             ▼                               ▼
     ┌────────────────────────────────────────────────┐
     │         Gleichzeitige Market-Orders             │
     │    BUY Exchange A  ←──gather──→  SELL Exchange B│
     └────────────────────────────────────────────────┘
             │                               │
             ▼                               ▼
     ┌────────────────┐              ┌───────────────────┐
     │  Position       │              │  Position          │
     │  "open"         │              │  "closed"          │
     └────────────────┘              └───────────────────┘
```
