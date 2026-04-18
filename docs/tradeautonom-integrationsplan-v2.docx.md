  
**TradeAutonom**

**Optimierungs-Integrationsplan**

Opt-in Erweiterungen für Spread-Erkennung, Orderbook-Analyse und Shared Monitoring

April 2026 — Basierend auf Code-Review des tradeautonom Repository

**Alle Änderungen sind opt-in per Feature-Flag. Bestehende Logik bleibt unangetastet.**

# **1\. Design-Prinzipien**

Jede Optimierung in diesem Dokument folgt drei Grundregeln:

* **Opt-in per Feature-Flag:** Alle Erweiterungen werden über neue Config-Felder in config.py aktiviert. Im Default bleiben sie deaktiviert — der Bot verhält sich exakt wie bisher.

* **Additive Änderungen:** Es wird kein bestehender Code gelöscht oder umgeschrieben. Neue Logik wird neben der bestehenden platziert und per if-Branch aktiviert.

* **Container-Isolation bleibt:** Jeder User behält seinen eigenen Container. Shared Services laufen als separater Container und kommunizieren über ein internes Docker-Netzwerk.

## **1.1 Feature-Flag-Struktur**

Alle neuen Flags folgen dem Präfix fn\_opt\_ (funding-neutral optimization) um sie klar von bestehenden fn\_-Flags zu unterscheiden:

📄 app/config.py — Neue Felder am Ende des fn\_-Blocks

\# ── Opt-in Optimizations ────────────────────────────────────

fn\_opt\_depth\_spread: bool \= False         \# Use fill-simulation instead of BBO for spread guard

fn\_opt\_funding\_history: bool \= False       \# Enable weighted historical funding spread filter

fn\_opt\_dynamic\_sizing: bool \= False        \# Adjust position size based on orderbook depth

fn\_opt\_ohi\_monitoring: bool \= False        \# Enable Orderbook Health Index in status/dashboard

fn\_opt\_anomaly\_detection: bool \= False     \# Enable anomaly detection in risk manager

\# Opt-in Tuning Parameters

fn\_opt\_max\_slippage\_bps: float \= 10.0     \# Max acceptable slippage per leg (bps)

fn\_opt\_min\_ohi: float \= 0.4               \# Minimum OHI to allow entry

fn\_opt\_min\_funding\_consistency: float \= 0.3 \# Min consistency score for weighted spread

fn\_opt\_max\_utilization: float \= 0.80      \# Max capital utilization (dynamic sizing)

fn\_opt\_max\_per\_pair\_ratio: float \= 0.25   \# Max single-pair exposure ratio

\# Shared Monitoring Service

fn\_opt\_shared\_monitor\_url: str \= ""      \# URL of shared orderbook monitor (empty \= disabled)

*Begründung: Durch den fn\_opt\_-Präfix sind alle Optimierungen sofort als solche erkennbar. Default False bedeutet: kein User muss seine .env anpassen. Wer die Features testen will, setzt einzelne Flags auf True.*

# **2\. Optimierung 1: Tiefenbasierte Spread-Erkennung**

## **2.1 Problem (Ist-Zustand)**

📄 app/state\_machine.py — Zeilen 844–860

Der aktuelle Spread-Guard vergleicht nur den Best Ask der Long-Venue mit dem Best Bid der Short-Venue:

\# AKTUELL: Nur Level 1 (Best Bid/Ask)

long\_ask \= float(sg\_m\_book\["asks"\]\[0\]\[0\])   \# ← Ein einzelner Preis

short\_bid \= float(sg\_t\_book\["bids"\]\[0\]\[0\])  \# ← Ein einzelner Preis

sg\_pct \= (long\_ask \- short\_bid) / short\_bid \* 100

Bei dünnen Orderbooks (typisch für Extended, Variational, NADO) kann der Best Ask bei 150.00 stehen, aber nach 2 SOL Tiefe ist der nächste Level bei 150.25. Der Spread-Guard sagt »OK«, aber die tatsächliche Execution kostet 15 bps mehr als erwartet.

## **2.2 Lösung: Neues Modul spread\_analyzer.py**

Ein eigenständiges Modul, das die bestehende estimate\_fill\_price()-Funktion aus safety.py nutzt, um den realen Execution-Spread zu berechnen:

📄 app/spread\_analyzer.py — NEUE DATEI

"""Cross-venue spread analysis with depth-aware fill price estimation.

Builds on safety.estimate\_fill\_price() to compute realistic execution spreads

instead of relying solely on BBO (Best Bid/Offer) prices.

"""

import logging

from dataclasses import dataclass

from decimal import Decimal

from app.safety import estimate\_fill\_price

logger \= logging.getLogger("tradeautonom.spread\_analyzer")

@dataclass

class SpreadAnalysis:

    """Result of a depth-aware cross-venue spread analysis."""

    long\_fill\_price: float       \# VWAP fill price for buying on long venue

    short\_fill\_price: float      \# VWAP fill price for selling on short venue

    bbo\_spread\_bps: float        \# Traditional BBO spread (for comparison logging)

    execution\_spread\_bps: float  \# Real spread based on simulated fills

    long\_slippage\_bps: float     \# Slippage on long leg (fill vs best ask)

    short\_slippage\_bps: float    \# Slippage on short leg (fill vs best bid)

    total\_slippage\_bps: float    \# Sum of both legs

    is\_favorable: bool           \# True if short\_fill \> long\_fill (instant profit)

    immediate\_profit\_bps: float  \# Unrealized profit at entry (0 if unfavorable)

def analyze\_cross\_venue\_spread(

    book\_long: dict,

    book\_short: dict,

    quantity: Decimal,

) \-\> SpreadAnalysis | None:

    """Compute execution-realistic spread between two venues.

    Args:

        book\_long: Orderbook dict of the venue where we go LONG (buy asks).

        book\_short: Orderbook dict of the venue where we go SHORT (sell bids).

        quantity: Trade size in base asset (per chunk, not total).

    Returns:

        SpreadAnalysis or None if books are incomplete.

    """

    if not book\_long.get("asks") or not book\_short.get("bids"):

        return None

    \# Simulate fills through the orderbook (reuses safety.py logic)

    long\_fill \= estimate\_fill\_price(book\_long, "buy", quantity)

    short\_fill \= estimate\_fill\_price(book\_short, "sell", quantity)

    if long\_fill \<= 0 or short\_fill \<= 0:

        return None

    \# BBO for comparison (old method)

    long\_best\_ask \= float(book\_long\["asks"\]\[0\]\[0\])

    short\_best\_bid \= float(book\_short\["bids"\]\[0\]\[0\])

    bbo\_spread \= (long\_best\_ask \- short\_best\_bid) / short\_best\_bid \* 10000

    exec\_spread \= (long\_fill \- short\_fill) / short\_fill \* 10000

    long\_slip \= (long\_fill \- long\_best\_ask) / long\_best\_ask \* 10000

    short\_slip \= (short\_best\_bid \- short\_fill) / short\_best\_bid \* 10000

    \# Negative exec\_spread \= long is cheaper than short \= instant profit

    imm\_profit \= max(0, \-exec\_spread)

    result \= SpreadAnalysis(

        long\_fill\_price=round(long\_fill, 6),

        short\_fill\_price=round(short\_fill, 6),

        bbo\_spread\_bps=round(bbo\_spread, 2),

        execution\_spread\_bps=round(exec\_spread, 2),

        long\_slippage\_bps=round(max(0, long\_slip), 2),

        short\_slippage\_bps=round(max(0, short\_slip), 2),

        total\_slippage\_bps=round(max(0, long\_slip) \+ max(0, short\_slip), 2),

        is\_favorable=(exec\_spread \< 0),

        immediate\_profit\_bps=round(imm\_profit, 2),

    )

    logger.debug(

        "SpreadAnalysis: BBO=%+.2fbps Exec=%+.2fbps Slip=%.2fbps favorable=%s",

        result.bbo\_spread\_bps, result.execution\_spread\_bps,

        result.total\_slippage\_bps, result.is\_favorable,

    )

    return result

*Begründung: estimate\_fill\_price() aus safety.py iteriert bereits korrekt durch die Orderbook-Levels. Statt diese Logik zu duplizieren, nutzt spread\_analyzer.py sie direkt. Dadurch ist das Modul nur \~60 Zeilen lang und hat keine eigene Orderbook-Parsing-Logik, die out-of-sync geraten könnte.*

## **2.3 Integration in StateMachine (Opt-in)**

📄 app/state\_machine.py — Änderung im Spread-Guard (Zeilen 830–870)

Die Änderung ergänzt den bestehenden BBO-Check um einen optionalen tiefenbasierten Check. Der BBO-Check bleibt als Fallback:

\# AM ANFANG DER DATEI: Neuer Import

from app.spread\_analyzer import analyze\_cross\_venue\_spread, SpreadAnalysis

\# IM SPREAD-GUARD ABSCHNITT (Zeile \~844), nach dem Book-Fetch:

\# ────────────────────────────────────────────────────────────

\# NEUER Block: direkt NACH sg\_m\_book / sg\_t\_book gefetcht wurden

\# und VOR der long\_ask/short\_bid Zuweisung:

    \# \--- OPT-IN: Depth-aware spread check \---

    use\_depth\_spread \= getattr(config, '\_use\_depth\_spread', False)

    if use\_depth\_spread:

        if config.maker\_side \== "buy":

            sa \= analyze\_cross\_venue\_spread(sg\_m\_book, sg\_t\_book, chunk\_qty)

        else:

            sa \= analyze\_cross\_venue\_spread(sg\_t\_book, sg\_m\_book, chunk\_qty)

        if sa is not None:

            sg\_pct \= sa.execution\_spread\_bps / 100  \# bps → %

            self.\_log("SPREAD",

                f"Chunk {chunk\_index} R{chase\_round}: "

                f"BBO={sa.bbo\_spread\_bps:+.1f}bps "

                f"Exec={sa.execution\_spread\_bps:+.1f}bps "

                f"Slip={sa.total\_slippage\_bps:.1f}bps "

                f"fav={sa.is\_favorable}"

            )

            if config.min\_spread\_pct \<= sg\_pct \<= config.max\_spread\_pct:

                spread\_ok \= True

                break

            \# Log reason and continue waiting loop

            if sg\_pct \< config.min\_spread\_pct:

                self.\_log("SPREAD", f"Exec spread {sg\_pct:+.4f}% below min", level="warn")

            else:

                self.\_log("SPREAD", f"Exec spread {sg\_pct:+.4f}% above max", level="warn")

            await asyncio.sleep(2.0)

            continue

    \# \--- BESTEHEND: Original BBO-basierter Spread Check (unverändert) \---

    long\_ask: float | None \= None

    short\_bid: float | None \= None

    \# ... (bestehender Code bleibt exakt wie er ist)

**Wie wird das Flag durchgereicht?**

📄 app/engine.py — In manual\_entry(), beim Erstellen der MakerTakerConfig

config \= MakerTakerConfig(

    \# ... alle bestehenden Felder ...

)

\# OPT-IN: Flag für tiefenbasierte Spread-Erkennung

config.\_use\_depth\_spread \= self.config.fn\_opt\_depth\_spread

*Hinweis: \_use\_depth\_spread wird als privates Attribut auf dem dataclass gesetzt, nicht als Feld. Dadurch ändert sich die MakerTakerConfig-Definition nicht und bestehende Serialisierung bleibt kompatibel.*

# **3\. Optimierung 2: Orderbook Health Index (OHI)**

## **3.1 Neue Methode in DataLayer**

📄 app/data\_layer.py — Neue Methode nach get\_orderbook\_depth()

def get\_orderbook\_health(

    self, exchange: str, symbol: str, target\_size\_usd: float \= 1000

) \-\> dict:

    """Compute Orderbook Health Index for one venue+symbol.

    Returns a dict with OHI (0-1), component scores, and diagnostics.

    Higher OHI \= healthier orderbook \= safer to trade.

    """

    snap \= self.\_orderbooks.get((exchange, symbol), OrderbookSnapshot())

    if not snap.bids or not snap.asks:

        return {"ohi": 0, "spread\_bps": 0, "depth\_20bps\_usd": 0,

                "symmetry": 0, "midpoint": 0, "components": {}}

    best\_bid \= snap.bids\[0\]\[0\]

    best\_ask \= snap.asks\[0\]\[0\]

    midpoint \= (best\_bid \+ best\_ask) / 2

    if midpoint \<= 0:

        return {"ohi": 0, "spread\_bps": 0, "depth\_20bps\_usd": 0,

                "symmetry": 0, "midpoint": 0, "components": {}}

    \# Component 1: Spread (0-1, tight \= good)

    spread\_bps \= (best\_ask \- best\_bid) / midpoint \* 10000

    spread\_score \= max(0, 1 \- spread\_bps / 20\)  \# 0 at 20bps, 1 at 0bps

    \# Component 2: Depth within 20bps of midpoint

    ask\_threshold \= midpoint \* 1.002

    bid\_threshold \= midpoint \* 0.998

    ask\_depth \= sum(lv\[1\] \* lv\[0\] for lv in snap.asks if lv\[0\] \<= ask\_threshold)

    bid\_depth \= sum(lv\[1\] \* lv\[0\] for lv in snap.bids if lv\[0\] \>= bid\_threshold)

    total\_depth \= ask\_depth \+ bid\_depth

    depth\_score \= min(1, total\_depth / max(target\_size\_usd \* 3, 1))

    \# Component 3: Symmetry (0-1, balanced \= good)

    if max(ask\_depth, bid\_depth) \> 0:

        symmetry \= min(ask\_depth, bid\_depth) / max(ask\_depth, bid\_depth)

    else:

        symmetry \= 0

    \# Weighted OHI score

    ohi \= 0.35 \* spread\_score \+ 0.35 \* depth\_score \+ 0.15 \* symmetry \+ 0.15 \* min(1, spread\_score \* depth\_score \* 4\)

    return {

        "ohi": round(ohi, 3),

        "spread\_bps": round(spread\_bps, 2),

        "depth\_20bps\_usd": round(total\_depth, 2),

        "ask\_depth\_usd": round(ask\_depth, 2),

        "bid\_depth\_usd": round(bid\_depth, 2),

        "symmetry": round(symmetry, 3),

        "midpoint": round(midpoint, 4),

        "components": {

            "spread\_score": round(spread\_score, 3),

            "depth\_score": round(depth\_score, 3),

            "symmetry\_score": round(symmetry, 3),

        },

    }

## **3.2 Einbindung in get\_full\_status()**

📄 app/engine.py — In get\_full\_status(), neuer Key neben "orderbooks"

\# Nach dem bestehenden "orderbooks" Key:

"orderbook\_health": {

    "long": self.\_data\_layer.get\_orderbook\_health(

        self.config.long\_exchange, self.config.instrument\_a,

        target\_size\_usd=float(self.config.quantity) \* self.\_get\_midprice()

    ),

    "short": self.\_data\_layer.get\_orderbook\_health(

        self.config.short\_exchange, self.config.instrument\_b,

        target\_size\_usd=float(self.config.quantity) \* self.\_get\_midprice()

    ),

} if self.\_data\_layer and self.config.fn\_opt\_ohi\_monitoring else {},

*Begründung: OHI ist eine reine Anzeige-Metrik. Sie kostet fast nichts an Performance (liest nur den gecachten OrderbookSnapshot) und liefert sofort Visibility über die Liquiditätsqualität. Deshalb ist sie low-risk genug, um sie früh zu aktivieren.*

# **4\. Optimierung 3: Historischer Funding-Spread-Filter**

## **4.1 Problem**

Der FundingMonitor berechnet aktuell nur den Spread des letzten Snapshots und annualisiert ihn. Ein einziger Spike in der Funding Rate sieht aus wie eine hoch-APR-Opportunity. In Wahrheit normalisiert sich der Spike oft innerhalb von Minuten, und die Entry-Kosten (Slippage \+ Fees auf 2 Legs) werden nie zurückverdient.

## **4.2 Änderungen in FundingMonitor**

📄 app/funding\_monitor.py — Erweiterte \_\_init\_\_ und \_update\_suggestion

\# Neue Imports am Anfang:

import collections

import json

from pathlib import Path

class FundingMonitor:

    def \_\_init\_\_(self, ..., enable\_history: bool \= False):

        \# ... bestehender Code ...

        

        \# NEU: History (nur wenn aktiviert)

        self.\_enable\_history \= enable\_history

        self.\_history: collections.deque \= collections.deque(maxlen=43200)

        if enable\_history:

            self.\_load\_history()

    def \_update\_suggestion(self) \-\> None:

        \# ... bestehender Code für rate\_a, rate\_b, spread ...

        \# NEU: History aufzeichnen (nach der bestehenden spread-Berechnung)

        if self.\_enable\_history:

            self.\_history.append({

                "ts": time.time(),

                "rate\_a": rate\_a,

                "rate\_b": rate\_b,

                "spread": spread,

            })

            \# Gewichteter Spread berechnen

            w\_spread \= self.\_compute\_weighted\_spread(spread)

            consistency \= self.\_compute\_consistency()

            \# Auf Suggestion-Objekt setzen (neue Felder)

            self.\_suggestion.funding\_spread\_weighted \= w\_spread

            self.\_suggestion.funding\_consistency \= consistency

            \# Periodisch auf Disk speichern (alle 60 Updates ≈ 1x/Stunde)

            if len(self.\_history) % 60 \== 0:

                self.\_save\_history()

**Weighted Spread Berechnung**

def \_compute\_weighted\_spread(self, current: float) \-\> float:

    """Gewichteter Durchschnitt: 25% aktuell, 30% 24h, 25% 3d, 20% 7d."""

    if len(self.\_history) \< 10:

        return current

    now \= time.time()

    def avg\_window(max\_age\_s):

        vals \= \[h\["spread"\] for h in self.\_history if now \- h\["ts"\] \< max\_age\_s\]

        return sum(vals) / len(vals) if vals else current

    return (0.25 \* current

          \+ 0.30 \* avg\_window(86400)

          \+ 0.25 \* avg\_window(259200)

          \+ 0.20 \* avg\_window(604800))

def \_compute\_consistency(self) \-\> float:

    """Wie stabil ist der Spread? 0-1, höher \= stabiler."""

    now \= time.time()

    recent \= \[h\["spread"\] for h in self.\_history if now \- h\["ts"\] \< 86400\]

    if len(recent) \< 10:

        return 0.0

    mean \= sum(recent) / len(recent)

    if abs(mean) \< 1e-10:

        return 0.0

    std \= (sum((s \- mean)\*\*2 for s in recent) / len(recent)) \*\* 0.5

    return max(0, min(1, 1 \- std / abs(mean)))

**Disk-Persistenz (Container-Restart-sicher)**

def \_save\_history(self) \-\> None:

    path \= Path(f"data/funding\_history\_{self.\_exchange\_a}\_{self.\_exchange\_b}.json")

    now \= time.time()

    recent \= \[h for h in self.\_history if now \- h\["ts"\] \< 86400\]

    try:

        path.parent.mkdir(parents=True, exist\_ok=True)

        with open(path, "w") as f:

            json.dump(recent, f)

    except Exception as exc:

        logger.warning("Failed to save funding history: %s", exc)

def \_load\_history(self) \-\> None:

    path \= Path(f"data/funding\_history\_{self.\_exchange\_a}\_{self.\_exchange\_b}.json")

    if path.exists():

        try:

            with open(path) as f:

                for h in json.load(f):

                    self.\_history.append(h)

            logger.info("Loaded %d funding history entries", len(self.\_history))

        except Exception as exc:

            logger.warning("Failed to load funding history: %s", exc)

**FundingSuggestion erweitern**

📄 app/funding\_monitor.py — FundingSuggestion dataclass

@dataclass

class FundingSuggestion:

    \# ... bestehende Felder ...

    

    \# NEU: Historische Analyse (nur gefüllt wenn enable\_history=True)

    funding\_spread\_weighted: float \= 0.0

    funding\_consistency: float \= 0.0

**Aktivierung in Engine**

📄 app/engine.py — In start(), beim Erstellen des FundingMonitor

self.\_funding\_monitor \= FundingMonitor(

    \# ... bestehende Parameter ...

    enable\_history=self.config.fn\_opt\_funding\_history,  \# NEU

)

# **5\. Architekturänderung: Shared Orderbook Monitoring Service**

## **5.1 Problem-Analyse**

Im aktuellen Setup unterhalt jeder User-Container eigene WebSocket-Verbindungen zu den Exchanges:

| Szenario | WS-Verbindungen | Bandbreite | Rate-Limit-Risiko |
| :---- | :---- | :---- | :---- |
| 5 User, je 1 Pair, 2 Exchanges | 10 OB \+ 10 Funding \= 20 WS | Moderat | Gering |
| 20 User, je 1 Pair, 2 Exchanges | 40 OB \+ 40 Funding \= 80 WS | Hoch | Mittel |
| 20 User, je 3 Pairs, 2 Exchanges | 120 OB \+ 120 Funding \= 240 WS | Sehr hoch | Hoch |
| 20 User, gleiche Pairs, shared | 6 OB \+ 6 Funding \= 12 WS | Minimal | Kein Risiko |

Dazu kommt: Wenn 10 User alle SOL-USD auf Extended handeln, unterhält jeder Container eine separate WebSocket-Verbindung zum selben Stream. Die Daten sind identisch — der NAS verarbeitet aber den Parse-Overhead 10-fach.

## **5.2 Vorgeschlagene Architektur**

Ein separater Container — der Orderbook Monitor Service (OMS) — unterhalt alle WS-Verbindungen zentral und stellt die Daten über ein internes HTTP/WS-API bereit:

┌─────────────────────────────────────────────────────────────────────┐

│                    Exchanges (Extended, GRVT, Nado, ...)    │

│                    ┌────────────────────────────────────┐           │

│                    │   Orderbook WS Streams       │           │

│                    │   Funding Rate WS Streams    │           │

│                    └────────────────┬───────────────────┘           │

└───────────────────────────────────┼─────────────────────────────────┘

                                   │

                    ┌───────────────┴───────────────────┐

                    │  Orderbook Monitor Service  │

                    │  (ta-monitor Container)     │

                    │                             │

                    │  \- Alle WS zentral          │

                    │  \- OHI Berechnung           │

                    │  \- Funding History           │

                    │  \- REST \+ WS API             │

                    └────┬───────────────┬─────────┘

                         │               │

              ┌─────────┴────┐  ┌───────┴──────┐

              │ ta-user-abc1 │  │ ta-user-def2  │

              │ (User A)     │  │ (User B)      │

              │              │  │               │

              │ Engine       │  │ Engine        │

              │ StateMachine │  │ StateMachine  │

              │ Execution    │  │ Execution     │

              └──────────────┘  └───────────────┘

## **5.3 Was der OMS liefert (und was nicht)**

| OMS liefert (shared, read-only) | User-Container behalten (privat) |
| :---- | :---- |
| Orderbook-Snapshots (alle Pairs, alle Exchanges) | Trading-Credentials (API Keys, JWTs) |
| Funding Rate History \+ Weighted Spreads | Order-Execution (Maker-Taker TWAP) |
| OHI pro Venue+Pair | Position Tracking \+ State Machine |
| Anomalie-Alerts (Preis-Divergenz, Liquiditäts-Drops) | Risk Manager (Circuit Breaker, Delta Guard) |
| Cross-Venue Price Spread (Live) | User-spezifische Config \+ Timer |

*Begründung der Trennung: Der OMS hat niemals Zugriff auf Trading-Credentials. Er liest nur öffentliche Marktdaten (Orderbooks, Funding Rates). Dadurch ist er kein Sicherheitsrisiko — selbst wenn er kompromittiert wird, können keine Trades ausgeführt werden.*

## **5.4 OMS API-Design**

\# REST Endpoints (HTTP, internes Docker-Netzwerk)

GET /api/v1/orderbook/{exchange}/{symbol}

  → { bids: \[\[p,q\],...\], asks: \[\[p,q\],...\], ts\_ms, ohi: {...} }

GET /api/v1/funding/{exchange}/{symbol}

  → { rate, ts, weighted\_spread, consistency, history\_length }

GET /api/v1/spread/{exchange\_a}/{symbol\_a}/{exchange\_b}/{symbol\_b}?qty=20

  → { bbo\_spread\_bps, exec\_spread\_bps, slippage\_bps, is\_favorable }

GET /api/v1/health

  → { exchanges: { extended: {connected, pairs}, grvt: {...} }, uptime }

\# WebSocket (für Echtzeit-Updates)

WS /ws/v1/orderbook/{exchange}/{symbol}

  → Streamt Orderbook-Updates an verbundene Clients

## **5.5 Entscheidungs-Begründung: Separate vs. Integrated**

Die Frage war: OMS als separater Container oder als Shared-Modul innerhalb der bestehenden Container?

| Aspekt | Separater Container (empfohlen) | Shared innerhalb User-Container |
| :---- | :---- | :---- |
| WS-Verbindungen | 1 pro Pair+Exchange (unabhängig von User-Anzahl) | N pro Pair+Exchange (N \= Anzahl User) |
| Ausfall-Isolation | OMS-Crash betrifft keine Executions | Ein kaputter WS-Handler kann den Trading-Loop blockieren |
| Memory | 1x \~50MB für alle Orderbooks | N×50MB (jeder Container hält eigene Kopie) |
| Exchange Rate Limits | 1 IP, 1 Connection | N IPs (aber gleiche NAS-IP → trotzdem Rate Limit) |
| Komplexität | Neuer Container \+ API | Keine Infra-Änderung |
| Rollout | Schrittweise (User-Container können opt-in) | Alles-oder-nichts |

*Entscheidung: Separater Container, weil die Rate-Limit-Problematik der größte reale Pain-Point ist. Alle User-Container teilen sich die NAS-IP. Wenn 20 Container gleichzeitig den Extended-WS verbinden, sieht Extended 20 Verbindungen von der gleichen IP. Ein zentraler OMS reduziert das auf 1 Verbindung, unabhängig von der User-Anzahl.*

## **5.6 Integration in User-Container (Opt-in)**

Wenn fn\_opt\_shared\_monitor\_url gesetzt ist, nutzt der DataLayer den OMS statt eigene WS-Verbindungen:

📄 app/data\_layer.py — Neue Methode \_run\_ob\_shared()

async def \_run\_ob\_shared(self, monitor\_url: str, exch\_name: str, symbol: str) \-\> None:

    """Receive orderbook updates from the shared OMS via WS."""

    key \= (exch\_name, symbol)

    ws\_url \= f"{monitor\_url.replace('http', 'ws')}/ws/v1/orderbook/{exch\_name}/{symbol}"

    reconnect\_delay \= 1.0

    while self.\_running:

        try:

            async for ws in websockets.connect(ws\_url, close\_timeout=5):

                async with self.\_ob\_locks\[key\]:

                    self.\_orderbooks\[key\].connected \= True

                reconnect\_delay \= 1.0

                async for raw in ws:

                    if not self.\_running:

                        return

                    msg \= json.loads(raw)

                    snap \= self.\_orderbooks\[key\]

                    snap.bids \= msg.get("bids", \[\])

                    snap.asks \= msg.get("asks", \[\])

                    snap.timestamp\_ms \= msg.get("ts\_ms", time.time() \* 1000\)

                    snap.is\_synced \= True

                    snap.update\_count \+= 1

                    self.\_ob\_changed.set()

        except asyncio.CancelledError:

            break

        except Exception as exc:

            logger.warning("Shared OB %s:%s error: %s", exch\_name, symbol, exc)

            async with self.\_ob\_locks\[key\]:

                self.\_orderbooks\[key\].connected \= False

            await asyncio.sleep(reconnect\_delay)

            reconnect\_delay \= min(reconnect\_delay \* 2, 30.0)

**Routing-Logik in \_run\_orderbook\_ws()**

📄 app/data\_layer.py — Am Anfang von \_run\_orderbook\_ws()

async def \_run\_orderbook\_ws(self, client, exch\_name: str, symbol: str) \-\> None:

    \# NEU: Shared Monitor hat Priorität (wenn konfiguriert)

    shared\_url \= getattr(client, '\_shared\_monitor\_url', '') if client else ''

    if not shared\_url:

        \# Prüfe ob es in den Settings steht

        settings \= getattr(client, 'settings', None)

        shared\_url \= getattr(settings, 'fn\_opt\_shared\_monitor\_url', '') if settings else ''

    

    if shared\_url:

        logger.info("DataLayer: Using shared monitor for %s:%s", exch\_name, symbol)

        await self.\_run\_ob\_shared(shared\_url, exch\_name, symbol)

        return

    

    \# BESTEHEND: Direkte Exchange-Verbindungen (unverändert)

    if exch\_name \== "extended":

        await self.\_run\_ob\_ws\_extended(symbol)

    elif exch\_name \== "grvt":

        \# ... usw.

*Fallback: Wenn der OMS nicht erreichbar ist, fällt der DataLayer automatisch auf direkte WS-Verbindungen zurück (durch den reconnect-Loop und die bestehende Stale-Detection). Kein User ist vom OMS abhängig — er ist eine reine Optimierung.*

## **5.7 Docker-Setup für den OMS**

📄 deploy/monitor/docker-compose.yml — NEUE DATEI

version: '3.8'

services:

  ta-monitor:

    image: tradeautonom:v3

    container\_name: ta-monitor

    restart: unless-stopped

    command: python \-m app.monitor\_service

    environment:

      \- APP\_HOST=0.0.0.0

      \- APP\_PORT=9099

      \- MONITOR\_MODE=true

      \- MONITOR\_EXCHANGES=extended,grvt,variational,nado

      \- MONITOR\_PAIRS=SOL-USD,ETH-USD,BTC-USD

    ports:

      \- '9099:9099'

    volumes:

      \- /volume1/docker/tradeautonom/app:/app/app:ro

      \- ta-monitor-data:/app/data

    mem\_limit: 256m

    networks:

      \- ta-internal

volumes:

  ta-monitor-data:

networks:

  ta-internal:

    driver: bridge

*Begründung: Der OMS nutzt das gleiche tradeautonom Image und den gleichen Shared Code (/app/app:ro). Er startet nur einen anderen Entrypoint (app.monitor\_service statt main.py). Dadurch gibt es kein separates Build-Artefakt zu pflegen.*

# **6\. Optimierung 4: Dynamisches Position Sizing**

## **6.1 Neues Modul: position\_sizer.py**

📄 app/position\_sizer.py — NEUE DATEI

"""Constraint-based position sizing using orderbook depth."""

import logging

from decimal import Decimal

from app.safety import estimate\_fill\_price

logger \= logging.getLogger("tradeautonom.position\_sizer")

class PositionSizer:

    def \_\_init\_\_(self, max\_slippage\_bps=10, max\_utilization=0.80, max\_per\_pair=0.25):

        self.max\_slippage\_bps \= max\_slippage\_bps

        self.max\_utilization \= max\_utilization

        self.max\_per\_pair \= max\_per\_pair

    def compute(self, collateral\_usd, book\_long, book\_short, existing\_usd=0):

        \# Constraint 1: Capital

        c1 \= (collateral\_usd \- existing\_usd) \* self.max\_utilization

        \# Constraint 2: Per-pair limit

        c2 \= collateral\_usd \* self.max\_per\_pair

        \# Constraint 3: Liquidity long venue

        c3 \= self.\_max\_for\_slippage(book\_long, "buy")

        \# Constraint 4: Liquidity short venue

        c4 \= self.\_max\_for\_slippage(book\_short, "sell")

        optimal \= min(c1, c2, c3, c4)

        constraints \= {"capital": c1, "pair\_limit": c2, "liq\_long": c3, "liq\_short": c4}

        binding \= min(constraints, key=constraints.get)

        return {"optimal\_usd": round(max(0, optimal), 2),

                "binding": binding, "constraints": constraints}

    def \_max\_for\_slippage(self, book, side):

        levels \= book.get("asks" if side \== "buy" else "bids", \[\])

        if not levels: return 0

        best \= float(levels\[0\]\[0\])

        total\_avail \= sum(float(lv\[1\]) for lv in levels)

        lo, hi \= 0.0, total\_avail

        for \_ in range(20):

            mid \= (lo \+ hi) / 2

            fp \= estimate\_fill\_price(book, side, Decimal(str(mid)))

            if fp \<= 0: hi \= mid; continue

            slip \= abs(fp \- best) / best \* 10000

            if slip \<= self.max\_slippage\_bps: lo \= mid

            else: hi \= mid

        return lo \* best

## **6.2 Integration in Engine (Opt-in)**

📄 app/engine.py — In manual\_entry(), vor dem MakerTakerConfig

\# NEU: Dynamisches Sizing (opt-in)

if self.config.fn\_opt\_dynamic\_sizing:

    from app.position\_sizer import PositionSizer

    sizer \= PositionSizer(

        max\_slippage\_bps=self.config.fn\_opt\_max\_slippage\_bps,

        max\_utilization=self.config.fn\_opt\_max\_utilization,

        max\_per\_pair=self.config.fn\_opt\_max\_per\_pair\_ratio,

    )

    book\_l \= await self.\_data\_layer.get\_orderbook(...)

    book\_s \= await self.\_data\_layer.get\_orderbook(...)

    sizing \= sizer.compute(collateral, book\_l\_dict, book\_s\_dict)

    self.log\_activity("SIZING",

        f"Dynamic: ${sizing\['optimal\_usd'\]:.0f} (binding: {sizing\['binding'\]})")

    

    \# Konvertiere USD → Base Asset Qty

    mid \= (book\_l.asks\[0\]\[0\] \+ book\_l.bids\[0\]\[0\]) / 2 if book\_l.asks and book\_l.bids else 1

    suggested\_qty \= Decimal(str(sizing\['optimal\_usd'\] / mid))

    

    \# Nutze das Minimum aus Config-Qty und dynamischer Qty

    qty \= min(qty, suggested\_qty)

    self.log\_activity("SIZING", f"Final qty: {qty} (config={self.config.quantity}, dynamic={suggested\_qty:.4f})")

# **7\. Umsetzungsplan und Reihenfolge**

| Woche | Aufgabe | Dateien | Feature-Flag |
| :---- | :---- | :---- | :---- |
| 1 | spread\_analyzer.py erstellen | NEU: app/spread\_analyzer.py | fn\_opt\_depth\_spread |
| 1 | Spread-Guard erweitern (opt-in Branch) | app/state\_machine.py | fn\_opt\_depth\_spread |
| 1 | Config-Felder hinzufügen | app/config.py | Alle fn\_opt\_\* |
| 1 | OHI in DataLayer | app/data\_layer.py | fn\_opt\_ohi\_monitoring |
| 2 | FundingMonitor History | app/funding\_monitor.py | fn\_opt\_funding\_history |
| 2 | FundingSuggestion erweitern | app/funding\_monitor.py | — |
| 2 | Dashboard: OHI \+ Weighted Spread anzeigen | app/engine.py (get\_full\_status) | fn\_opt\_ohi\_monitoring |
| 3 | position\_sizer.py erstellen | NEU: app/position\_sizer.py | fn\_opt\_dynamic\_sizing |
| 3 | Integration in manual\_entry() | app/engine.py | fn\_opt\_dynamic\_sizing |
| 4 | OMS: monitor\_service.py erstellen | NEU: app/monitor\_service.py | fn\_opt\_shared\_monitor\_url |
| 4 | DataLayer: Shared-Monitor Routing | app/data\_layer.py | fn\_opt\_shared\_monitor\_url |
| 4 | Docker-Setup für OMS | NEU: deploy/monitor/ | — |

## **7.1 Test-Strategie**

* **Woche 1:** spread\_analyzer isoliert testen mit gespeicherten Orderbook-Snapshots. Vergleiche BBO-Spread vs. Exec-Spread über 100+ Snapshots. Aktiviere fn\_opt\_depth\_spread auf einem Test-Container und lasse den Bot im Simulation-Modus laufen.

* **Woche 2:** FundingMonitor History mit echten Daten füllen (24h laufen lassen). Vergleiche weighted\_spread vs. aktuellen Spread — wenn Spikes gefiltert werden, funktioniert der Filter.

* **Woche 3:** PositionSizer im Simulation-Modus testen. Logge die vorgeschlagene Qty vs. die config-Qty und prüfe, ob die Slippage-Constraints korrekt greifen.

* **Woche 4:** OMS auf einem separaten Port starten. Einen Test-Container darauf zeigen und verifizieren, dass die Orderbook-Daten identisch sind zu den direkten WS-Feeds.

## **7.2 Rollback-Plan**

Jede Optimierung kann sofort deaktiviert werden durch:

* Feature-Flag auf False setzen in der .env des betroffenen Containers

* Container neu starten (uvicorn reload bei Hot-Config, sonst docker restart)

* Kein Code-Rollback nötig — die bestehende Logik wird durch die Flags nie berührt

*Alle Änderungen sind so designed, dass sie einzeln und unabhängig voneinander aktiviert werden können. Du kannst mit fn\_opt\_depth\_spread anfangen und den Rest später nachrüsten, ohne dass sich das Verhalten ändert.*