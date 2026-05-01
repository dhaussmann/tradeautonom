# DNA Bot — Wie und wann eine Order ausgeführt wird

Dieses Dokument richtet sich an Endanwender (Trader und Operatoren) des
DNA Bots. Es beschreibt verständlich, **wann** der Bot eine Order
auslöst, **welche Prüfungen** kurz vor der Ausführung durchlaufen
werden und **was nach dem Trade** passiert. Es ist bewusst frei von
Quellcode und Entwickler-Details.

Eine konzeptionelle Übersicht über das Bot-Design findest du in
`docs/dna-bot.md`. Dieses Dokument ergänzt sie um die operative
Sichtweise.

---

## 1. Was der DNA Bot tut

Der DNA Bot sucht **Preisunterschiede zwischen zwei Börsen für denselben
Token** und eröffnet gleichzeitig zwei gegenläufige Positionen:

- Auf der günstigeren Börse wird der Token **gekauft** (Long-Bein).
- Auf der teureren Börse wird der Token **verkauft** (Short-Bein).

Beide Positionen sind in der Größe identisch. Dadurch ist die
Gesamtposition **delta-neutral**: Bewegungen des Token-Preises gleichen
sich auf beiden Seiten aus. Gewinn entsteht ausschließlich daraus, dass
der Spread zwischen Kauf- und Verkaufspreis später wieder schrumpft. Der
DNA Bot ist also kein direktionaler Trading-Bot, sondern ein
Spread-Capture-Bot.

---

## 2. Voraussetzungen, damit überhaupt gehandelt wird

Bevor der Bot eine Order auslöst, müssen alle folgenden Bedingungen
erfüllt sein:

- Der Bot ist **gestartet** und läuft.
- Der Simulationsmodus ist **aus** (sofern echte Trades gewünscht sind).
- Der Vault ist entsperrt; API-Keys aller benötigten Börsen sind
  eingerichtet.
- Der Marktdaten-Service (OMS) ist erreichbar und liefert frische
  Orderbuch-Daten.
- Die gewählte Token-/Börsen-Kombination ist als **handelbar** markiert.
  Der Tradeability-Check läuft mindestens stündlich und blendet
  Märkte aus, deren Buch einseitig oder unrealistisch ist.
- Der Token ist nicht in der **Excluded-Liste**.
- Der Token steht nicht in einer **Cooldown-Phase** nach einem kürzlich
  automatisch geschlossenen Trade.
- Der Bot hält noch nicht das Maximum an gleichzeitigen Positionen.
- Es ist nicht bereits eine Position auf demselben Token offen.

Sind eine oder mehrere dieser Bedingungen verletzt, wird gar kein
Signal verarbeitet.

---

## 3. Wann ein Entry getriggert wird

Der Auslöser für eine Order ist immer ein **Live-Signal vom
Marktdaten-Service**. Der Ablauf:

1. Der OMS scannt fortlaufend die Orderbücher aller verbundenen Börsen.
2. Sobald der beste Verkaufspreis auf einer Börse höher ist als der
   beste Kaufpreis auf einer anderen Börse, berechnet er den
   **Netto-Profit in Basispunkten (BPS)** nach Gebühren.
3. Übersteigt der Profit die für dieses Börsenpaar konfigurierte
   Schwelle, geht ein Signal an alle abonnierten Bots.
4. Der DNA Bot empfängt das Signal über eine permanente
   Live-Verbindung.

Bevor das Signal überhaupt zur Order-Ausführung weitergereicht wird,
prüft der Bot mehrere früh greifende Filter. Wird einer davon verletzt,
verwirft er das Signal sofort:

- **Profit zu klein** für den eingestellten Spread-Modus
  (`delta_neutral`, `half_neutral` oder `custom`).
- **Signal zu alt:** Liegt der Zeitstempel des Signals länger zurück als
  erlaubt (Standard 1500 ms), gilt es als veraltet und wird verworfen.
- **Cooldown aktiv:** Der Token wurde kürzlich automatisch geschlossen,
  und der Cooldown läuft noch.
- **Position bereits offen** auf diesem Token (in derselben oder einer
  anderen Richtung).
- **Maximum** an gleichzeitigen Positionen erreicht.
- **Börse nicht in der Bot-Konfiguration** enthalten.

---

## 4. Pre-Trade-Validierung — was direkt vor der Order geprüft wird

Hat das Signal die ersten Filter überstanden, durchläuft es drei
Sicherheitsstufen, bevor irgendeine Order an die Börse geht.

### 4.1 Mengen-Harmonisierung

- Der Bot berechnet die Trade-Menge aus deiner konfigurierten
  Positionsgröße in USD und dem aktuellen Mid-Preis.
- Die Menge wird auf die **Schrittgröße beider Börsen** abgerundet
  (z. B. 0,01 SOL bei Extended, 0,001 SOL bei GRVT).
- Liegt die resultierende Menge **unter der Mindestordergröße einer
  Börse**, wird der Trade abgebrochen.

### 4.2 Live-Cross-Quote-Check

Diese Stufe ist die wichtigste Verteidigung gegen schlecht ausgeführte
Trades. Der Bot fragt unmittelbar vor der Order einen frischen
Live-Quote vom Marktdaten-Service ab. Geprüft wird:

- **Ausführbarkeit beider Seiten:** Liefern beide Bücher genug Tiefe
  für die volle Menge?
- **Bücheralter:** Sind die Order-Books auf beiden Seiten frisch genug
  (Standardgrenze 2000 ms)? Veraltete Bücher ergeben unzuverlässige
  Trades.
- **Profitabilität nach Gebühren:** Ist der Spread auch nach Abzug der
  Gebühren beider Börsen noch positiv?
- **Spread-Erosion:** Wie stark hat sich der Spread zwischen
  Signaleingang und jetzigem Live-Quote verändert? Ist die Verschlechte‑
  rung größer als erlaubt (Standard 40 %), wird abgebrochen.
- **Live-Resize:** Falls die aktuelle Buchtiefe kleiner ist als die
  ursprünglich angepeilte Menge, verkleinert der Bot die Order
  automatisch auf die maximal sicher ausführbare Menge.

Jeder dieser Skips wird im Aktivitätsprotokoll mit einem klaren Grund
geloggt, sodass du nachvollziehen kannst, warum kein Trade
stattgefunden hat.

Falls der Marktdaten-Service nicht erreichbar ist, gibt es zwei Modi:

- **Fail-open** (Standard): Der Bot nutzt den älteren Pfad ohne
  Live-Validierung — handelt also wie früher.
- **Fail-closed** (per Setting aktivierbar): Ohne Live-Quote wird kein
  Trade ausgeführt.

### 4.3 Nado-Spezialprüfung

Ist eine der beiden Börsen Nado, durchläuft die Order zusätzlich eine
spezielle Tiefenprüfung. Hintergrund: Nado nutzt FOK-Orders ("Fill or
Kill" — entweder vollständig ausgeführt oder gar nicht). Ein
Teil-Fill ist nicht möglich.

Der Bot:

- Geht das Nado-Buch durch und prüft, ob die volle Menge in einem
  vertretbaren Preis-Korridor erhältlich ist.
- Schätzt den Slippage-Aufschlag konservativ ab.
- Rechnet diesen Aufschlag und einen statischen Aufschlag für das
  andere Bein vom erwarteten Profit ab.
- Liegt der erwartete Netto-Profit nach diesen Aufschlägen bei null
  oder darunter, wird abgebrochen — nichts wird gehandelt.

So wird verhindert, dass das andere (schnellere) Bein bereits gefüllt
wird und der Nado-Teil dann nachträglich scheitert.

---

## 5. Die eigentliche Order-Ausführung

Sind alle Prüfungen bestanden, läuft die Ausführung in folgenden
Schritten ab:

1. **Hebel setzen.** Falls für dieses Symbol auf einer Börse noch nicht
   geschehen, setzt der Bot einmalig den maximalen Hebel.
2. **Baseline-Snapshot.** Der Bot liest die aktuell offenen Positionen
   auf beiden Börsen und merkt sich die Größen. Diese Snapshots dienen
   später als Referenz, um zu erkennen, ob die Order korrekt
   durchgegangen ist.
3. **Beide Beine gleichzeitig** abfeuern:
   - Auf der günstigeren Börse: Kauf-Order (Long-Bein).
   - Auf der teureren Börse: Verkaufs-Order (Short-Bein).
   - Die Orders sind sofort-aggressiv: je nach Börse als Marktorder
     mit Slippage-Limit, als IOC-Limit oder als FOK-Order. Maker-Orders
     werden in dieser Phase **nicht** verwendet.
4. **Auf Bestätigung warten.**
   - GRVT und Nado bestätigen den Fill üblicherweise sofort.
   - Extended liefert die Fill-Bestätigung manchmal asynchron; der Bot
     fragt deshalb mehrfach mit kurzem Abstand nach, bis der Fill
     bekannt ist (oder die Wartezeit abläuft).

---

## 6. Was nach dem Fill passiert

### Fall A — Beide Beine gefüllt

- Die Position wird intern gespeichert, mit den **echten** Fill-Preisen
  und Mengen, nicht den geschätzten Werten aus dem Signal.
- Im Aktivitätsprotokoll erscheint eine **Telemetrie-Zeile**:
  - Signal-Spread vs. Cross-Quote-Spread vs. tatsächlich realisierter
    Spread.
  - Slippage je Seite in BPS.
  - Indikator, ob der Long-Preis tatsächlich kleiner ist als der
    Short-Preis.
  - Ausführungsdauer in Millisekunden.
- Der Bot vergleicht die neuen Positionsgrößen auf beiden Börsen mit
  den Baseline-Snapshots. Stimmt die Differenz mit der erwarteten
  Menge überein, ist alles in Ordnung.
- Liegt eine Seite außerhalb der Toleranz (Standard 5 %), wird
  automatisch ein **Repair-Trade** ausgelöst:
  - Bei zu kleiner Position auf einer Seite: Nachkauf.
  - Bei zu großer Position auf einer Seite: Teilweises Zurückfahren.
- Schlägt die Reparatur fehl, erscheint eine deutliche Warnung im
  Aktivitätsprotokoll mit Hinweis auf manuelles Eingreifen.

### Fall B — Nur eine Seite gefüllt

- Der Bot **fährt das gefüllte Bein automatisch zurück** (Unwind),
  damit keine ungesicherte, gerichtete Position übrig bleibt.
- Der Vorgang wird mit Grund („ein Bein fehlgeschlagen") protokolliert.

### Fall C — Beide Seiten fehlgeschlagen

- Es ist kein Risiko entstanden, weil keine Position aufgemacht wurde.
- Der Vorgang wird mit beiden Fehlern protokolliert.

---

## 7. Wann eine Position wieder geschlossen wird

Der DNA Bot überwacht jede offene Position kontinuierlich auf
Schließbedingungen.

- **Spread-Konvergenz (Hauptfall):** Sobald der Spread auf das
  konfigurierte Exit-Niveau zurückfällt **und** die Mindesthaltezeit
  überschritten ist, schließt der Bot automatisch.
- **Manueller Close:** Du kannst eine Position jederzeit über den
  Close-Button im Frontend manuell schließen.
- **Polling-Sicherheitsnetz:** Falls der Bot kurz die Live-Verbindung
  verloren hat, prüft ein paralleler Polling-Loop alle 60 Sekunden, ob
  eine Schließbedingung erfüllt ist.
- **Cooldown nach Auto-Close:** Nach einem automatischen Schließen
  startet eine konfigurierbare Cooldown-Phase (Standard 300 Sekunden)
  für genau diesen Token. In dieser Zeit ignoriert der Bot neue
  Signale auf demselben Token. So werden Whipsaw-Re-Entries vermieden,
  die durch den eigenen Close den Markt kurzzeitig bewegen können. Bei
  einem manuellen Close greift der Cooldown bewusst nicht — du behältst
  die volle Kontrolle.

Das Schließen folgt demselben Schema wie das Eröffnen, nur mit
umgekehrten Seiten: das ehemalige Long-Bein wird verkauft, das ehemalige
Short-Bein zurückgekauft. Auch nach dem Close läuft eine
Verifikation, ob beide Seiten tatsächlich auf null stehen; falls nicht,
versucht der Bot, die verbleibende Restmenge zu glätten.

---

## 8. Welche Daten du sehen kannst

Im DNA-Bot-Frontend und auf der Admin-Aktivitätsseite findest du:

- **Aktivitätsprotokoll**, Klartext-Zeilen pro Ereignis, z. B.:
  - "Signal verworfen: Cross-Quote zu alt"
  - "Position eröffnet"
  - "Repair-Trade durchgeführt"
  - "Nur eine Seite gefüllt — Unwind durchgeführt"
- **Trade-Telemetrie** je erfolgreicher Eröffnung mit:
  - erwartetem Signal-Spread,
  - Live-Quote-Spread vor der Order,
  - tatsächlich realisiertem Spread,
  - Slippage je Seite,
  - Indikator "Long günstiger als Short".
- **Liste offener Positionen** mit Baseline-Snapshots,
  Eröffnungs-Spread und Halte-Dauer.
- **Aktive Cooldowns** je Token mit verbleibender Restzeit.
- **Trade-Historie** der bereits geschlossenen Positionen.

---

## 9. Konfigurationshebel im Überblick

Folgende Einstellungen kannst du anpassen, ohne den Bot neu zu deployen.
Standardwerte in Klammern.

| Einstellung | Bedeutung | Standard |
|-------------|-----------|----------|
| Positionsgröße in USD | Wie viel Notional pro Trade | 1000 |
| Maximale Anzahl Positionen | Wie viele Trades parallel | 3 |
| Spread-Modus | `delta_neutral`, `half_neutral`, `custom` | `delta_neutral` |
| Mindest-Profit BPS | Manueller Floor (0 = OMS-Schwelle nutzen) | 0 |
| Slippage-Toleranz | Maximaler Slippage je Order in % | 0.5 % |
| Maximales Signal-Alter | Verwirft alte Signale | 1500 ms |
| Maximales Quote-Alter | Verwirft veraltete Live-Bücher | 2000 ms |
| Maximale Spread-Erosion | Erlaubter Verlust Signal → Live-Quote | 40 % |
| Cross-Quote erforderlich | Fail-closed bei nicht erreichbarem OMS | aus |
| Cooldown nach Auto-Close | Whipsaw-Schutz je Token | 300 s |
| Excluded Tokens | Liste auszuschließender Tokens | (Setup-abhängig) |
| Auto-Exclude offene Positionen | Schließt manuell offene Tokens automatisch aus | an |
| Größen-Toleranz | Auslöser für Repair-Trade | 5 % |

---

## 10. Häufige Skip-Gründe und ihre Bedeutung

Wenn der Bot einen Trade nicht ausführt, erscheint einer der folgenden
Gründe im Aktivitätsprotokoll:

| Aktivitäts-Eintrag (sinngemäß) | Bedeutung |
|-------------------------------|-----------|
| "Signal zu alt" | Das Signal war länger unterwegs als erlaubt. |
| "Cooldown aktiv" | Der Token wurde kürzlich automatisch geschlossen. |
| "Cross-Quote nicht erreichbar" | Der Marktdaten-Service hat nicht geantwortet. Je nach Modus gilt fail-open oder fail-closed. |
| "Cross-Quote infeasible" | Eine Seite des Buchs ist leer oder ausgeschlossen. |
| "Cross-Quote zu alt" | Die Live-Bücher sind zu alt für eine zuverlässige Order. |
| "Cross-Quote unprofitabel" | Der Spread deckt die Gebühren nicht mehr. |
| "Erosion zu hoch" | Der Spread hat sich seit Signal stark verschlechtert. |
| "Mengen-Harmonisierung zu klein" | Nach Anpassung an Mindestgrößen ist nichts handelbar. |
| "Nado-Tiefe unzureichend" | Auf Nado fehlt die Liquidität für die volle Menge. |
| "Nado-Walk unprofitabel" | Nach Berücksichtigung des Slippage-Aufschlags bleibt kein Profit. |
| "Beide Beine fehlgeschlagen" | Keine Order ist durchgekommen, kein Risiko entstanden. |
| "Ein Bein fehlgeschlagen → Unwind" | Eine Seite ist gefüllt, wurde sofort glattgestellt. |
| "Positionsgröße abweichend → Repair" | Eine Seite war nach dem Fill außerhalb der Toleranz; automatischer Reparatur-Trade. |

---

## 11. Was der DNA Bot bewusst nicht tut

- **Keine Marktrichtungs-Wette.** Der Bot setzt nicht darauf, dass ein
  Token steigt oder fällt — er nimmt ausschließlich den Spread mit.
- **Keine Maker-Strategie aktuell.** Beide Beine werden als
  aggressive Taker-Orders ausgeführt. Maker-Pricing ist möglich, aber
  in dieser Version nicht aktiv.
- **Keine Fill-Garantie.** Bei dünner Liquidität bricht der Bot
  bewusst ab, statt zu schlechten Preisen zu kaufen.
- **Keine Trades ohne aktuelle Daten.** Stale Signale und veraltete
  Bücher führen zum Skip, nicht zu einer Order auf gut Glück.
- **Kein automatisches Wiedereröffnen** während eines Cooldowns.
- **Kein Eingriff in offene manuelle Positionen** — Tokens, in denen
  du selbst eine Position hältst, werden automatisch ausgeschlossen.

---

## 12. Glossar

- **BPS (Basispunkte):** 1 BPS = 0,01 %. Übliche Einheit für kleine
  Spread-Werte.
- **BBO (Best Bid / Best Offer):** Höchster Kaufpreis und niedrigster
  Verkaufspreis im Buch zur gleichen Zeit.
- **Spread:** Differenz zwischen einem Verkaufspreis auf einer Börse
  und einem Kaufpreis auf einer anderen — die zentrale
  Profitkennzahl des Bots.
- **VWAP (Volume Weighted Average Price):** Durchschnittspreis,
  gewichtet nach der gehandelten Menge auf jedem Preisniveau. Wird
  genutzt, um realistische Fill-Preise für größere Orders zu schätzen.
- **IOC (Immediate or Cancel):** Order, die so weit wie möglich sofort
  gefüllt wird; nicht ausführbarer Rest verfällt.
- **FOK (Fill or Kill):** Order wird **nur dann** ausgeführt, wenn die
  vollständige Menge sofort gefüllt werden kann; sonst wird die Order
  vollständig storniert.
- **Maker / Taker:** Maker stellt Liquidität bereit (passive Limit-Order
  im Buch), Taker nimmt Liquidität (aggressive Order, die das Buch
  trifft). Taker zahlen typischerweise höhere Gebühren.
- **Slippage:** Differenz zwischen erwartetem Preis und tatsächlich
  ausgeführtem Preis.
- **Tradeability:** Kennzeichnung des Marktdaten-Service, ob ein
  bestimmter Token auf einer bestimmten Börse aktuell verlässlich
  handelbar ist (Buch beidseitig vorhanden, nicht stale, nicht
  gekreuzt).
- **Cross-Quote:** Live-Pre-Trade-Snapshot vom Marktdaten-Service, der
  die ausführbaren VWAP-Preise und die Profitabilität nach Gebühren
  über zwei Börsen hinweg liefert.
- **Erosion:** Wie viel Profit zwischen Signaleingang und Live-Quote
  verloren geht. Hohe Erosion = Markt hat sich gegen uns bewegt.
- **Delta-Neutral:** Position, deren Wert unabhängig von der Richtung
  der Token-Preisbewegung bleibt, weil Long und Short sich
  gegenseitig absichern.
- **Cooldown:** Zeitfenster nach einem automatischen Close, in dem
  derselbe Token nicht erneut gehandelt wird.
- **Whipsaw:** Kurzfristiges Hin- und Herspringen des Markts, das ohne
  Cooldown zu sofortigem Re-Entry mit ungünstigem Preis führen
  könnte.
- **Baseline-Snapshot:** Aufnahme der bestehenden Positionsgrößen
  unmittelbar vor einer Order. Dient als Referenz für die spätere
  Verifikation des Fills.
- **Unwind:** Auflösen einer einseitig gefüllten Position durch eine
  Gegenorder, damit kein gerichtetes Risiko übrig bleibt.

---

Querverweise:

- Konzeptionelle Beschreibung des DNA Bots: `docs/dna-bot.md`.
- Strategie- und PnL-Hintergrund: `docs/STRATEGY_GUIDE.md`.
- Aktuelle Verhaltensänderungen: `RELEASENOTES.md`.
