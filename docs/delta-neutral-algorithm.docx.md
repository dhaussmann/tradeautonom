  
**Delta-Neutral Funding Rate**

**Arbitrage Algorithm**

Exchange-agnostisches mathematisches Framework

Technische Dokumentation v1.0

April 2026

*Dieses Dokument beschreibt ein vollständiges, exchange-agnostisches Algorithmus-Framework für delta-neutrale Funding-Rate-Arbitrage. Alle mathematischen Konzepte sind so formuliert, dass sie mit minimalem Anpassungsaufwand auf beliebige Perpetual-DEX-Paare angewendet werden können.*

# **1\. Systemübersicht und Architektur**

Das System besteht aus fünf lose gekoppelten Modulen, die jeweils eine klar definierte Verantwortung tragen. Diese Modularität ermöglicht es, einzelne Exchanges auszutauschen, ohne die Kernlogik zu verändern.

| Modul | Verantwortung | Input | Output |
| :---- | :---- | :---- | :---- |
| OrderbookScanner | Orderbook-Snapshots sammeln, Tiefe und Slippage berechnen | WebSocket/REST Feeds | LiquiditySnapshot |
| FundingAnalyzer | Funding Rates sammeln, historisch gewichten, Spreads berechnen | Funding Rate APIs | FundingOpportunity |
| PositionSizer | Optimale Positionsgröße basierend auf Liquidität und Risiko | LiquiditySnapshot \+ Config | PositionSize |
| ExecutionEngine | Order-Platzierung, Leg-Synchronisation, Fallback-Logik | PositionSize \+ Orderbooks | ExecutionResult |
| PortfolioManager | Bestehende Positionen verwalten, Rotation entscheiden | Alle Module | RotationDecision |

## **1.1 Exchange-Abstraktionsschicht**

Jede Exchange wird über ein einheitliches Interface abstrahiert. Dadurch bleibt die gesamte Algorithmik exchange-agnostisch:

interface ExchangeAdapter {

  getOrderbook(pair: string): Promise\<Orderbook\>

  getFundingRate(pair: string): Promise\<FundingRate\>

  getHistoricalFunding(pair: string, days: number): Promise\<FundingRate\[\]\>

  placeOrder(order: OrderRequest): Promise\<OrderResult\>

  getPosition(pair: string): Promise\<Position | null\>

  closePosition(pair: string): Promise\<OrderResult\>

  getCollateral(): Promise\<CollateralInfo\>

}

*Begründung: Durch die Abstraktion können Extended, Variational, Lighter, Hyperliquid oder jede andere Perp-DEX mit identischer Logik betrieben werden. Nur der Adapter muss angepasst werden.*

# **2\. Orderbook-Analyse-Algorithmus**

Das Orderbook ist die zentrale Datenquelle für Liquiditätsbewertung, Slippage-Schätzung und Positionsgrößen-Bestimmung. Der folgende Abschnitt beschreibt die mathematischen Grundlagen.

## **2.1 Orderbook-Datenstruktur**

Jedes Orderbook wird als sortierte Liste von Preis-Mengen-Paaren dargestellt:

interface OrderbookLevel {

  price: number;    // Preis auf diesem Level

  size: number;     // Verfügbare Menge (in Base Asset)

  total: number;    // Kumulierte Menge bis zu diesem Level

}

interface Orderbook {

  bids: OrderbookLevel\[\];  // Sortiert: höchster Preis zuerst

  asks: OrderbookLevel\[\];  // Sortiert: niedrigster Preis zuerst

  timestamp: number;       // Zeitstempel des Snapshots

  exchange: string;        // Exchange-Identifier

  pair: string;            // Trading-Pair

}

## **2.2 Slippage-Berechnung**

Die Slippage ist die zentrale Metrik, um zu bestimmen, ob eine Positionsgröße im aktuellen Orderbook effizient ausgeführt werden kann. Die Berechnung simuliert eine Market Order durch das Orderbook:

**Algorithmus: simulateMarketOrder()**

function simulateMarketOrder(levels: OrderbookLevel\[\], quantity: number):

  remaining \= quantity

  totalCost \= 0

  filledLevels \= \[\]

  for level in levels:

    fillAtThisLevel \= min(remaining, level.size)

    totalCost \+= fillAtThisLevel \* level.price

    remaining \-= fillAtThisLevel

    filledLevels.push({ price: level.price, filled: fillAtThisLevel })

    if remaining \<= 0:

      break

  if remaining \> 0:

    return { feasible: false, unfilled: remaining }

  avgPrice \= totalCost / quantity

  bestPrice \= levels\[0\].price

  slippageBps \= |avgPrice \- bestPrice| / bestPrice \* 10000

  return {

    feasible: true,

    avgPrice: avgPrice,

    bestPrice: bestPrice,

    slippageBps: slippageBps,

    filledLevels: filledLevels,

    totalCost: totalCost

  }

*Begründung: Die mengenbasierte Slippage-Berechnung ist präziser als ein einfacher Durchschnittspreis, weil sie die tatsächliche Verteilung der Liquidität über die Preis-Levels berücksichtigt. Ein »ungewichteter Durchschnittspreis« würde die Slippage bei dünnen Orderbooks systematisch unterschätzen.*

## **2.3 Tiefenanalyse (Depth Profile)**

Das Depth Profile zeigt, wie viel Liquidität innerhalb bestimmter Basispunkt-Bereiche vom Midpoint verfügbar ist:

function computeDepthProfile(orderbook: Orderbook, bpsLevels: number\[\]):

  midpoint \= (orderbook.bids\[0\].price \+ orderbook.asks\[0\].price) / 2

  profile \= {}

  for bps in bpsLevels:      // z.B. \[5, 10, 20, 50, 100\]

    askThreshold \= midpoint \* (1 \+ bps / 10000\)

    bidThreshold \= midpoint \* (1 \- bps / 10000\)

    askDepth \= sum(level.size for level in asks where level.price \<= askThreshold)

    bidDepth \= sum(level.size for level in bids where level.price \>= bidThreshold)

    profile\[bps\] \= {

      askDepthBase: askDepth,        // Menge in Base Asset

      askDepthUSD: askDepth \* midpoint,

      bidDepthBase: bidDepth,

      bidDepthUSD: bidDepth \* midpoint,

      totalDepthUSD: (askDepth \+ bidDepth) \* midpoint

    }

  return profile

| BPS-Level | Bedeutung | Typischer Anwendungsfall |
| :---- | :---- | :---- |
| 5 bps | Extrem enge Liquidität direkt am Midpoint | Nur für sehr kleine Positionen relevant |
| 10 bps | Guter Indikator für Spread-Qualität | Standard-Slippage-Budget für liquid Pairs |
| 20 bps | Moderate Tiefe | Typisches Slippage-Budget für mittlere Positionen |
| 50 bps | Breite Tiefe | Maximum-Slippage für große Positionen |
| 100 bps | Gesamtliquidität innerhalb 1% | Stress-Test der Venue-Liquidität |

## **2.4 Orderbook-Gesundheitsindex (OHI)**

Der OHI fasst mehrere Orderbook-Metriken in einen einzelnen Score zusammen, der die Eignung einer Venue für die Orderausführung bewertet:

function computeOHI(orderbook: Orderbook, targetSizeUSD: number):

  // 1\. Spread-Komponente (0-1, kleiner \= besser)

  spreadBps \= (asks\[0\].price \- bids\[0\].price) / midpoint \* 10000

  spreadScore \= max(0, 1 \- spreadBps / 20\)  // Normalisiert auf 20bps

  // 2\. Tiefe-Komponente (0-1, tiefer \= besser)

  depthAt20bps \= depthProfile\[20\].totalDepthUSD

  depthRatio \= min(1, depthAt20bps / (targetSizeUSD \* 3))

  // Faktor 3: Wir wollen mindestens 3x unsere Positionsgröße in der Tiefe

  // 3\. Symmetrie-Komponente (0-1, symmetrischer \= besser)

  bidDepth \= depthProfile\[20\].bidDepthUSD

  askDepth \= depthProfile\[20\].askDepthUSD

  symmetry \= min(bidDepth, askDepth) / max(bidDepth, askDepth)

  // 4\. Slippage-Komponente (0-1, weniger Slippage \= besser)

  sim \= simulateMarketOrder(asks, targetSizeUSD / midpoint)

  slippageScore \= sim.feasible ? max(0, 1 \- sim.slippageBps / 10\) : 0

  // Gewichteter Score

  OHI \= 0.30 \* spreadScore

      \+ 0.30 \* depthRatio

      \+ 0.15 \* symmetry

      \+ 0.25 \* slippageScore

  return { OHI, spreadBps, depthRatio, symmetry, slippageScore }

*Begründung der Gewichtung: Spread und Tiefe sind die wichtigsten Faktoren (je 30%), da sie direkt die Ausführungskosten bestimmen. Die Slippage-Simulation (25%) validiert das Gesamtbild. Symmetrie (15%) ist ein Warnsignal für einseitige Orderbooks, die auf bevorstehende Preisbewegungen hindeuten können.*

*Wichtig: Der OHI basiert auf einem einzelnen Snapshot. Für robuste Entscheidungen sollte der OHI über mehrere Snapshots gemittelt werden (siehe Abschnitt 2.5).*

## **2.5 Zeitgewichteter OHI (TOHI)**

Um die Zuverlässigkeit der Liquiditätsbewertung zu erhöhen, wird der OHI über einen gleitenden Zeitraum aggregiert:

function computeTOHI(ohiHistory: TimeSeries\<OHI\>, windowMinutes: number \= 5):

  recentOHIs \= ohiHistory.filter(t \=\> t.age \< windowMinutes \* 60 \* 1000\)

  if recentOHIs.length \< 3:

    return { tohi: 0, confidence: 'insufficient\_data' }

  // Exponentiell gewichteter Durchschnitt (neuere Werte zählen mehr)

  alpha \= 2 / (recentOHIs.length \+ 1\)

  ema \= recentOHIs\[0\].ohi

  for i \= 1 to recentOHIs.length:

    ema \= alpha \* recentOHIs\[i\].ohi \+ (1 \- alpha) \* ema

  // Stabilität: Standardabweichung als Konfidenzmaß

  stdDev \= standardDeviation(recentOHIs.map(o \=\> o.ohi))

  stability \= max(0, 1 \- stdDev / 0.3)  // Normalisiert

  // Minimum-OHI im Fenster (Worst-Case-Betrachtung)

  minOHI \= min(recentOHIs.map(o \=\> o.ohi))

  return {

    tohi: ema,

    stability: stability,

    minOHI: minOHI,

    confidence: stability \> 0.7 ? 'high' : stability \> 0.4 ? 'medium' : 'low'

  }

*Begründung: Ein einzelner Orderbook-Snapshot kann täuschen – Market Maker können Liquidität kurzfristig abziehen. Der TOHI glättet diese Schwankungen und liefert ein zuverlässigeres Bild. Die Minimum-Betrachtung schützt vor optimistischer Fehleinschätzung.*

# **3\. Funding Rate Analyse**

## **3.1 Annualisierung und Normalisierung**

Verschiedene Exchanges verwenden unterschiedliche Funding-Intervalle. Alle Rates müssen auf eine gemeinsame Basis (APR) normalisiert werden:

| Exchange-Typ | Funding-Intervall | Annualisierungs-Formel |
| :---- | :---- | :---- |
| Standard (Binance-Typ) | Alle 8 Stunden | rate\_8h × 3 × 365 |
| Stündlich (Hyperliquid) | Jede Stunde | rate\_1h × 24 × 365 |
| Kontinuierlich (Paradex) | \~5 Sekunden | rate\_5s × (86400/5) × 365 |
| Variabel | Beliebig | rate × (86400 / interval\_seconds) × 365 |

function annualizeFundingRate(rate: number, intervalSeconds: number): number {

  periodsPerDay \= 86400 / intervalSeconds

  return rate \* periodsPerDay \* 365

}

## **3.2 Historisch gewichtete Funding-Spread-Bewertung**

Der Kern der Opportunitätsbewertung: Der Funding Spread ist die Differenz der annualisierten Funding Rates zwischen zwei Venues. Ein positiver Spread bedeutet, dass die Long-Venue dem Longholder zahlt und die Short-Venue vom Shorthalter kassiert – oder umgekehrt.

function computeFundingSpread(rateVenueA: number, rateVenueB: number):

  // Convention: positiver Spread \= profitabel wenn Long A / Short B

  // Funding Rate positiv \= Longs zahlen Shorts

  // Wir wollen: Long wo Rate negativ (wir erhalten), Short wo Rate positiv (wir erhalten)

  spreadLongAShortB \= \-rateVenueA \+ rateVenueB

  spreadLongBShortA \= \-rateVenueB \+ rateVenueA

  if spreadLongAShortB \> spreadLongBShortA:

    return { spread: spreadLongAShortB, longVenue: 'A', shortVenue: 'B' }

  else:

    return { spread: spreadLongBShortA, longVenue: 'B', shortVenue: 'A' }

**Historischer Filter (Weighted Historical Spread)**

Um temporäre Spikes herauszufiltern, wird der aktuelle Spread gegen historische Daten gewichtet:

function computeWeightedSpread(currentSpread: number, history: SpreadHistory):

  // Gewichtung: Neuere Daten zählen mehr, aber Langzeittrend stabilisiert

  weights \= {

    current:  0.25,   // Aktueller Snapshot

    avg\_24h:  0.30,   // 24-Stunden-Durchschnitt

    avg\_3d:   0.20,   // 3-Tage-Durchschnitt

    avg\_7d:   0.15,   // 7-Tage-Durchschnitt

    avg\_30d:  0.10,   // 30-Tage-Durchschnitt

  }

  weightedSpread \= weights.current  \* currentSpread

                 \+ weights.avg\_24h  \* history.avg24h

                 \+ weights.avg\_3d   \* history.avg3d

                 \+ weights.avg\_7d   \* history.avg7d

                 \+ weights.avg\_30d  \* history.avg30d

  // Stabilitätscheck: Wie konsistent ist der Spread?

  consistency \= 1 \- (history.stdDev7d / abs(weightedSpread))

  consistency \= clamp(consistency, 0, 1\)

  return { weightedSpread, consistency }

*Begründung der Gewichtung: Der 24h-Durchschnitt erhält das höchste Gewicht (0.30), weil er kurzfristige Trends erfasst, ohne auf einzelne Spikes zu reagieren. Der aktuelle Wert (0.25) ermöglicht schnelle Reaktion auf echte Regime-Wechsel. Langfristige Durchschnitte (7d, 30d) bilden das Fundament und verhindern Eintritte in Pairs, die historisch instabile Spreads haben.*

## **3.3 Minimum-Profitabilitätsanalyse**

Bevor eine Position eröffnet wird, muss der erwartete Profit alle Kosten übersteigen:

function computeMinimumProfitability(

  fundingSpreadAPR: number,

  slippageCostBps: { entry: number, exit: number },

  feeCostBps: { makerEntry: number, takerEntry: number, makerExit: number, takerExit: number },

  holdingPeriodDays: number

):

  // Gesamte Entry-Kosten (beide Legs)

  entrySlippage \= slippageCostBps.entry  // Summe beider Venues

  entryCostBps \= entrySlippage \+ feeCostBps.makerEntry \+ feeCostBps.takerEntry

  // Gesamte Exit-Kosten (beide Legs)

  exitSlippage \= slippageCostBps.exit

  exitCostBps \= exitSlippage \+ feeCostBps.makerExit \+ feeCostBps.takerExit

  // Gesamtkosten als annualisierter Prozentsatz

  totalCostBps \= entryCostBps \+ exitCostBps

  totalCostAPR \= totalCostBps / holdingPeriodDays \* 365

  // Netto-APR nach Kosten

  netAPR \= fundingSpreadAPR \- totalCostAPR

  // Breakeven-Haltezeit in Tagen

  dailyEarningBps \= fundingSpreadAPR / 365

  breakevenDays \= totalCostBps / dailyEarningBps

  return {

    netAPR,

    totalCostBps,

    breakevenDays,

    profitable: netAPR \> 0

  }

*Kritisch: Viele Arbitrage-Systeme ignorieren die Exit-Kosten bei der Profitabilitätsberechnung. Dies führt zu systematischer Überschätzung des tatsächlichen Profits. Dieser Algorithmus berücksichtigt explizit Entry \+ Exit.*

# **4\. Position Sizing Algorithmus**

Die Positionsgröße wird durch multiple Constraints bestimmt. Die endgültige Größe ist immer das Minimum aller Constraints – die engste Beschränkung dominiert.

## **4.1 Constraint-basiertes Position Sizing**

function computeOptimalPosition(

  collateral: number,          // Verfügbares Kapital in USD

  config: PositionConfig,       // Risiko-Parameter

  liquidityA: LiquiditySnapshot,// Orderbook Venue A

  liquidityB: LiquiditySnapshot,// Orderbook Venue B

  existingExposure: number      // Bereits allokiertes Kapital

):

  // Constraint 1: Kapital-Allokation

  maxByCapital \= (collateral \- existingExposure) \* config.maxUtilization

  // maxUtilization z.B. 0.80 \= nie mehr als 80% des Kapitals nutzen

  // Constraint 2: Einzelpaar-Exposure

  maxByPairLimit \= collateral \* config.maxPerPairRatio

  // maxPerPairRatio z.B. 0.25 \= max 25% in ein einzelnes Pair

  // Constraint 3: Liquiditäts-basiert (Venue A)

  maxByLiquidityA \= findMaxSizeForSlippageBudget(

    liquidityA.orderbook,

    config.maxSlippageBps  // z.B. 10 bps

  )

  // Constraint 4: Liquiditäts-basiert (Venue B)

  maxByLiquidityB \= findMaxSizeForSlippageBudget(

    liquidityB.orderbook,

    config.maxSlippageBps

  )

  // Constraint 5: Relative Größe zum Markt

  avgVolumeA \= liquidityA.volume24h

  avgVolumeB \= liquidityB.volume24h

  maxByMarketImpact \= min(avgVolumeA, avgVolumeB) \* config.maxVolumeRatio

  // maxVolumeRatio z.B. 0.02 \= max 2% des 24h-Volumens

  // Finales Ergebnis: Minimum aller Constraints

  optimalSize \= min(

    maxByCapital,

    maxByPairLimit,

    maxByLiquidityA,

    maxByLiquidityB,

    maxByMarketImpact

  )

  // Bestimme den bindenden Constraint (für Logging/Debugging)

  bindingConstraint \= argmin(

    maxByCapital, maxByPairLimit, maxByLiquidityA,

    maxByLiquidityB, maxByMarketImpact

  )

  return { optimalSize, bindingConstraint, allConstraints: {...} }

**Algorithmus: findMaxSizeForSlippageBudget()**

Binäre Suche nach der maximalen Ordergröße, die innerhalb des Slippage-Budgets bleibt:

function findMaxSizeForSlippageBudget(

  orderbook: Orderbook,

  maxSlippageBps: number

):

  // Gesamte verfügbare Liquidität im Orderbook

  totalAvailable \= sum(level.size for level in orderbook.asks)

  lo \= 0

  hi \= totalAvailable

  while hi \- lo \> 0.001:  // Präzision: 0.001 Base Asset

    mid \= (lo \+ hi) / 2

    sim \= simulateMarketOrder(orderbook.asks, mid)

    if sim.feasible AND sim.slippageBps \<= maxSlippageBps:

      lo \= mid    // Kann mehr, versuche größer

    else:

      hi \= mid    // Zu viel, versuche kleiner

  return lo \* midpoint  // Konvertiere zu USD

*Begründung der binären Suche: Linear durch das Orderbook zu iterieren wäre O(n) für jeden Test. Die binäre Suche konvergiert in O(log n) Schritten zur optimalen Größe. Bei 20 Iterationen erreicht man eine Präzision von 1/1.000.000 des Gesamtvolumens.*

## **4.2 Empfohlene Default-Konfiguration**

| Parameter | Default-Wert | Begründung |
| :---- | :---- | :---- |
| maxUtilization | 0.80 (80%) | 20% Reserve für Margin Calls und Notfall-Exits |
| maxPerPairRatio | 0.25 (25%) | Diversifikation: Minimum 4 Positionen für Portfolio-Effekt |
| maxSlippageBps | 10 bps | Bei DEX-Liquidität ein realistischer Kompromiss |
| maxVolumeRatio | 0.02 (2%) | Begrenzung des Market Impact auf ein Minimum |
| minOHI | 0.5 | Unter diesem Wert ist die Venue zu illiquide |
| minTOHIStability | 0.4 | Orderbook muss über Zeit stabil sein |

# **5\. Entry-Entscheidungslogik**

Die Entry-Logik kombiniert alle vorherigen Analysen in eine Ja/Nein-Entscheidung. Der Algorithmus durchläuft eine strikte Gate-Struktur: Jede Bedingung muss erfüllt sein.

## **5.1 Entry Gate Algorithmus**

function evaluateEntry(pair: string, venueA: Exchange, venueB: Exchange):

  // ═══ Gate 1: Funding Spread Minimum ═══

  spread \= computeWeightedSpread(pair, venueA, venueB)

  if spread.weightedSpread \< config.minSpreadAPR:  // z.B. 10% APR

    return REJECT('spread\_too\_low')

  // ═══ Gate 2: Spread-Konsistenz ═══

  if spread.consistency \< config.minConsistency:   // z.B. 0.5

    return REJECT('spread\_inconsistent')

  // ═══ Gate 3: Orderbook-Gesundheit beider Venues ═══

  tohiA \= computeTOHI(venueA.ohiHistory, pair)

  tohiB \= computeTOHI(venueB.ohiHistory, pair)

  if tohiA.tohi \< config.minOHI OR tohiB.tohi \< config.minOHI:

    return REJECT('insufficient\_liquidity')

  if tohiA.confidence \== 'low' OR tohiB.confidence \== 'low':

    return REJECT('unstable\_orderbook')

  // ═══ Gate 4: Positions-Sizing möglich ═══

  sizing \= computeOptimalPosition(...)

  if sizing.optimalSize \< config.minPositionUSD:   // z.B. $100

    return REJECT('position\_too\_small')

  // ═══ Gate 5: Profitabilität nach Kosten ═══

  slippageA \= simulateMarketOrder(venueA.orderbook, sizing.optimalSize)

  slippageB \= simulateMarketOrder(venueB.orderbook, sizing.optimalSize)

  profitability \= computeMinimumProfitability(

    spread.weightedSpread,

    { entry: slippageA.slippageBps \+ slippageB.slippageBps,

      exit: slippageA.slippageBps \+ slippageB.slippageBps },  // Konservativ: Exit \= Entry

    feeStructure,

    config.expectedHoldingDays

  )

  if profitability.netAPR \< config.minNetAPR:       // z.B. 5% nach Kosten

    return REJECT('insufficient\_net\_apr')

  // ═══ Gate 6: Persistenz-Check ═══

  // Bedingungen müssen über N Scans stabil sein

  if not persistenceTracker.isStable(pair, config.minPersistenceMinutes):

    return REJECT('conditions\_not\_persistent')

  // ═══ Gate 7: Cross-Venue Price Spread ═══

  priceSpreadBps \= computePriceSpread(venueA.orderbook, venueB.orderbook, spread.longVenue)

  if priceSpreadBps \> 0:  // Positiv \= Long-Seite günstiger als Short-Seite

    sizing.bonus \= priceSpreadBps  // Zusätzlicher unrealisierter Profit

  return ACCEPT({

    pair, sizing, spread, profitability, priceSpreadBps,

    longVenue: spread.longVenue, shortVenue: spread.shortVenue

  })

**Cross-Venue Price Spread (Gate 7\)**

Dies ist der Mechanismus, der ca. 80% der Trades sofort profitabel macht. Der Algo sucht aktiv nach Preisdiskrepanzen zwischen den Venues:

function computePriceSpread(

  orderbookLong: Orderbook,  // Wo wir Long gehen

  orderbookShort: Orderbook, // Wo wir Short gehen

  longVenue: string

):

  // Für Long: Wir kaufen \= bester Ask-Preis

  longEntryPrice \= orderbookLong.asks\[0\].price

  // Für Short: Wir verkaufen \= bester Bid-Preis

  shortEntryPrice \= orderbookShort.bids\[0\].price

  // Positiv \= Short-Einstieg höher als Long-Einstieg \= sofortiger Profit

  priceSpreadBps \= (shortEntryPrice \- longEntryPrice) / longEntryPrice \* 10000

  return priceSpreadBps

*Begründung: Wenn wir Long bei $3.000 und Short bei $3.005 eingehen, haben wir sofort $5 unrealisierten Profit pro Einheit. Dieser Spread ist bei fragmentierten DEX-Märkten häufig, weil die Preisfindung nicht so effizient ist wie auf CEXes.*

# **6\. Execution Engine**

Die Ausführung ist der risikoreichste Moment der gesamten Strategie. Zwischen der Orderbook-Analyse und dem tatsächlichen Fill können sich die Bedingungen ändern. Die Engine muss beide Legs möglichst gleichzeitig und mit definiertem Fallback ausführen.

## **6.1 Execution-Modi**

| Modus | Beschreibung | Vorteile | Nachteile |
| :---- | :---- | :---- | :---- |
| Simultaneous Market | Market Orders auf beiden Venues gleichzeitig | Minimale Leg-Divergenz | Höchste Slippage \+ Fees |
| Maker-Taker Hybrid | Limit auf illiquider Venue, Market auf liquider nach Fill | Niedrigere Kosten auf Maker-Leg | Ungewisser Fill-Zeitpunkt |
| Sequential TWAP | Beide Legs in kleinen Chunks über Zeit | Minimaler Market Impact | Langsam, längeres Dir.-Exposure |

## **6.2 Simultaneous Market Execution (Empfohlen für Start)**

async function executeSimultaneous(

  entry: AcceptedEntry,

  venueA: Exchange, venueB: Exchange

):

  // Pre-Flight Check: Orderbook-Validierung direkt vor Execution

  freshBookA \= await venueA.getOrderbook(entry.pair)

  freshBookB \= await venueB.getOrderbook(entry.pair)

  // Prüfe ob sich die Bedingungen seit der Analyse verschlechtert haben

  freshSlippageA \= simulateMarketOrder(freshBookA, entry.sizing.optimalSize)

  freshSlippageB \= simulateMarketOrder(freshBookB, entry.sizing.optimalSize)

  if freshSlippageA.slippageBps \> entry.slippageA.slippageBps \* 1.5

     OR freshSlippageB.slippageBps \> entry.slippageB.slippageBps \* 1.5:

    return ABORT('orderbook\_deteriorated')

  // Gleichzeitige Ausführung

  \[resultA, resultB\] \= await Promise.allSettled(\[

    venueA.placeOrder({

      pair: entry.pair,

      side: entry.longVenue \== 'A' ? 'BUY' : 'SELL',

      size: entry.sizing.optimalSize,

      type: 'MARKET',

      maxSlippage: config.maxSlippageBps \* 1.5  // 50% Buffer

    }),

    venueB.placeOrder({

      pair: entry.pair,

      side: entry.shortVenue \== 'B' ? 'SELL' : 'BUY',

      size: entry.sizing.optimalSize,

      type: 'MARKET',

      maxSlippage: config.maxSlippageBps \* 1.5

    })

  \])

  // Fehlerbehandlung: Ein Leg failed

  if resultA.status \== 'rejected' AND resultB.status \== 'fulfilled':

    // SOFORT Leg B schließen (Unwind)

    await venueB.closePosition(entry.pair)

    return ERROR('leg\_a\_failed', { unwindResult: ... })

  if resultB.status \== 'rejected' AND resultA.status \== 'fulfilled':

    await venueA.closePosition(entry.pair)

    return ERROR('leg\_b\_failed', { unwindResult: ... })

  if resultA.status \== 'rejected' AND resultB.status \== 'rejected':

    return ERROR('both\_legs\_failed')  // Kein Unwind nötig

  // Beide erfolgreich: Verifiziere Größen-Match

  filledA \= resultA.value.filledSize

  filledB \= resultB.value.filledSize

  sizeDiffPercent \= abs(filledA \- filledB) / max(filledA, filledB) \* 100

  if sizeDiffPercent \> config.maxSizeDivergencePercent:  // z.B. 5%

    // Partial Unwind der größeren Position

    surplus \= abs(filledA \- filledB)

    surplusVenue \= filledA \> filledB ? venueA : venueB

    await surplusVenue.reducePosition(entry.pair, surplus)

  return SUCCESS({

    pair: entry.pair,

    longVenue: entry.longVenue,

    shortVenue: entry.shortVenue,

    sizeLong: filledA,

    sizeShort: filledB,

    entryPriceLong: resultA.value.avgPrice,

    entryPriceShort: resultB.value.avgPrice,

    timestamp: now()

  })

*Kritisch: Promise.allSettled() statt Promise.all(). Bei Promise.all() würde ein Fehler auf Venue A dazu führen, dass Venue B gar nicht erst versucht wird – oder schlimmer, dass wir den Status von B nicht kennen. allSettled() garantiert, dass wir den Status beider Legs kennen und entsprechend reagieren können.*

## **6.3 Maker-Taker Hybrid Execution**

Für fortgeschrittene Implementierungen bietet der Hybrid-Modus niedrigere Kosten:

async function executeMakerTaker(entry: AcceptedEntry):

  // Identifiziere die illiquidere Venue (= Maker-Leg)

  makerVenue \= entry.tohiA \< entry.tohiB ? venueA : venueB

  takerVenue \= makerVenue \== venueA ? venueB : venueA

  makerSide \= makerVenue \== entry.longVenue ? 'BUY' : 'SELL'

  // Schritt 1: Limit Order auf Maker-Venue

  limitPrice \= makerSide \== 'BUY'

    ? makerVenue.orderbook.bids\[0\].price \+ 1 tick  // Knapp vor Best Bid

    : makerVenue.orderbook.asks\[0\].price \- 1 tick  // Knapp vor Best Ask

  makerOrder \= await makerVenue.placeOrder({

    pair: entry.pair,

    side: makerSide,

    size: entry.sizing.optimalSize,

    type: 'LIMIT',

    price: limitPrice,

    timeInForce: 'GTC'

  })

  // Schritt 2: Warte auf Fill (mit Timeout)

  fillResult \= await waitForFill(makerOrder, config.makerTimeoutMs)

  // makerTimeoutMs z.B. 30000 (30 Sekunden)

  if fillResult.status \== 'timeout':

    await makerVenue.cancelOrder(makerOrder.id)

    return ABORT('maker\_fill\_timeout')

  if fillResult.status \== 'partial':

    // Nur den gefüllten Teil hedgen

    entry.sizing.optimalSize \= fillResult.filledSize

  // Schritt 3: Sofort Market Order auf Taker-Venue

  takerResult \= await takerVenue.placeOrder({

    pair: entry.pair,

    side: makerSide \== 'BUY' ? 'SELL' : 'BUY',

    size: entry.sizing.optimalSize,

    type: 'MARKET',

    maxSlippage: config.maxSlippageBps \* 2  // Breiteres Budget weil zeitkritisch

  })

  if takerResult.status \== 'failed':

    // Maker-Position sofort unwinden

    await makerVenue.closePosition(entry.pair)

    return ERROR('taker\_leg\_failed')

  return SUCCESS(...)

*Begründung Maker-Taker: Der Maker-Leg spart typischerweise 2-5 bps an Fees. Über viele Trades summiert sich das erheblich. Der Tradeoff ist die Verzögerung zwischen den Legs – daher der Timeout und das breitere Slippage-Budget für den Taker-Leg.*

# **7\. Exit- und Rotationslogik**

Die Rotation – das Schließen einer bestehenden Position und Eröffnen einer neuen – ist der risikoreichste Vorgang im System. Vier Orders müssen ausgeführt werden, und in der Zwischenzeit ist das Kapital teilweise ungehedged.

## **7.1 Exit-Trigger**

Eine bestehende Position wird geschlossen, wenn eine der folgenden Bedingungen eintritt:

| Trigger | Schwellenwert | Priorität |
| :---- | :---- | :---- |
| Funding Spread dreht sich um | weightedSpread \< 0 für \> 2h | SOFORT |
| Spread unter Kosten | netAPR \< 0 für \> 4h | HOCH |
| Orderbook trocknet aus | TOHI \< 0.3 auf einer Venue | HOCH |
| Bessere Opportunity (Rotation) | Neue Opp. \> aktuelle \+ Rotationskosten | MITTEL |
| Max. Haltezeit erreicht | Konfigurierbar, z.B. 30 Tage | NIEDRIG |

## **7.2 Rotations-Entscheidungsalgorithmus**

function evaluateRotation(

  currentPosition: ActivePosition,

  newOpportunity: AcceptedEntry

):

  // Kosten der Rotation (4 Orders: 2x Close \+ 2x Open)

  closeCosts \= estimateCloseCosts(currentPosition)

  openCosts \= estimateOpenCosts(newOpportunity)

  totalRotationCostBps \= closeCosts.totalBps \+ openCosts.totalBps

  // Annualisierte Rotationskosten

  rotationCostAPR \= totalRotationCostBps

    / newOpportunity.expectedHoldingDays \* 365

  // Netto-Vorteil der Rotation

  currentNetAPR \= currentPosition.currentNetAPR

  newNetAPR \= newOpportunity.profitability.netAPR \- rotationCostAPR

  rotationAdvantage \= newNetAPR \- currentNetAPR

  // Höherer Threshold für Rotation vs. initialen Einstieg

  minRotationAdvantage \= config.minRotationAdvantageAPR  // z.B. 5%

  minRotationMultiplier \= config.minRotationMultiplier   // z.B. 1.5x

  shouldRotate \= rotationAdvantage \> minRotationAdvantage

    AND newNetAPR \> currentNetAPR \* minRotationMultiplier

    AND newOpportunity.spread.consistency \> 0.7  // Höherer Standard

  return {

    shouldRotate,

    rotationAdvantage,

    currentNetAPR,

    newNetAPR,

    rotationCostBps: totalRotationCostBps,

    reasoning: shouldRotate

      ? 'Rotation profitabel: \+' \+ rotationAdvantage \+ '% APR'

      : 'Bestehende Position bevorzugt'

  }

*Begründung des hohen Rotation-Thresholds: Jede Rotation verursacht garantierte Kosten (Slippage \+ Fees auf 4 Legs) gegen einen unsicheren zukünftigen Vorteil. Der Multiplier von 1.5x bedeutet: Die neue Opportunity muss mindestens 50% besser sein als die aktuelle, um die Rotationsrisiken zu rechtfertigen. Dies verhindert übermäßiges »Churning«.*

## **7.3 Rotation Execution**

Die Reihenfolge der Rotation ist kritisch:

1. Neue Position eröffnen (Long-Leg der neuen Opportunity)

2. Neue Position eröffnen (Short-Leg der neuen Opportunity)

3. Alte Position schließen (beide Legs)

*Begründung der Reihenfolge: Zuerst die neue Position eröffnen statt zuerst die alte zu schließen. Warum? Wenn die alte zuerst geschlossen wird, sitzt das Kapital temporär unproduktiv – und die Bedingungen der neuen Opportunity könnten sich verschlechtern. Umgekehrt: Wenn die neue Position fehlschlägt, bleibt die alte Position bestehen und verdient weiter.*

*Achtung: Diese Reihenfolge erfordert zusätzliches Kapital (vorübergehend laufen alte \+ neue Position parallel). Der PortfolioManager muss sicherstellen, dass genügend freies Collateral vorhanden ist.*

# **8\. Risikomanagement**

## **8.1 Risiko-Kategorien und Mitigationsstrategien**

| Risiko-Kategorie | Beschreibung | Mitigation |
| :---- | :---- | :---- |
| Leg-Divergenz | Nur ein Leg wird ausgeführt → ungehedgtes Exposure | Sofortiges Unwinding des offenen Legs; maxSizeDivergence-Check |
| Funding Reversal | Spread dreht sich um, Position zahlt statt verdient | Historischer Filter, Persistenz-Check, zeitnahe Exit-Trigger |
| Liquiditäts-Evaporation | Orderbook wird plötzlich dünn → hohe Exit-Slippage | TOHI-Monitoring, konservatives Sizing (3x-Tiefe-Regel) |
| Smart Contract Risk | Bug oder Exploit auf einer DEX | Diversifikation über Venues, Position Caps pro Venue |
| Liquidation | Margin Call bei extremer Preisbewegung | maxUtilization 80%, Position Sizing Constraints |
| Oracle-Manipulation | Preisfeed-Manipulation auf einer Venue | Cross-Venue-Preisvergleich, Anomalie-Detection |
| Latenz-Risiko | Veraltete Orderbook-Daten führen zu falschen Entscheidungen | Snapshot-Alter prüfen, Pre-Flight-Validierung |

## **8.2 Portfolio-Level Limits**

interface RiskLimits {

  maxTotalExposureUSD: number,      // Absolutes Maximum aller Positionen

  maxPerVenueExposureUSD: number,   // Maximum pro Exchange

  maxPerPairExposureUSD: number,    // Maximum pro Trading-Pair

  maxCorrelatedExposure: number,    // Max für korrelierte Assets (z.B. ETH \+ L2s)

  maxDrawdownPercent: number,       // Stop-all bei Drawdown X%

  maxConcurrentPositions: number,   // Maximale Anzahl gleichzeitiger Positionen

  maxDailyRotations: number,        // Verhindert Overtrading

  emergencyExitThreshold: number,   // TOHI unter X → alle Positionen schließen

}

## **8.3 Anomalie-Detection**

Der Algorithmus überprüft laufend auf Anomalien, die auf Probleme hindeuten könnten:

function detectAnomalies(snapshot: SystemSnapshot):

  anomalies \= \[\]

  // 1\. Preis-Divergenz zwischen Venues \> 1%

  for pair in activePairs:

    priceDiff \= abs(priceA \- priceB) / avg(priceA, priceB)

    if priceDiff \> 0.01:

      anomalies.push({ type: 'price\_divergence', pair, diff: priceDiff })

  // 2\. Plötzlicher Liquiditätsabfall \> 50%

  for venue in venues:

    if currentDepth \< previousDepth \* 0.5:

      anomalies.push({ type: 'liquidity\_drop', venue })

  // 3\. Funding Rate Extremwerte (\> 3 Standardabweichungen)

  for pair in monitoredPairs:

    if abs(currentRate \- historicalMean) \> 3 \* historicalStdDev:

      anomalies.push({ type: 'funding\_spike', pair })

  // 4\. Erhöhte Latenz zu einer Venue

  for venue in venues:

    if venue.latency \> 2 \* venue.avgLatency:

      anomalies.push({ type: 'latency\_spike', venue })

  // Bei kritischen Anomalien: Pausiere neue Entries

  if anomalies.any(a \=\> a.type in \['price\_divergence', 'liquidity\_drop'\]):

    pauseNewEntries()

  return anomalies

# **9\. Haupt-Algorithmus (Main Loop)**

Der gesamte Algorithmus läuft in einem kontinuierlichen Loop, der alle Komponenten orchestriert:

async function mainLoop():

  while running:

    try:

      // ╔═══════════════════════════════════════════╗

      // ║ Phase 1: Daten sammeln                    ║

      // ╚═══════════════════════════════════════════╝

      for venue in venues:

        for pair in venue.availablePairs:

          orderbook \= await venue.getOrderbook(pair)

          fundingRate \= await venue.getFundingRate(pair)

          ohi \= computeOHI(orderbook, config.targetSize)

          store(orderbook, fundingRate, ohi)

      // ╔═══════════════════════════════════════════╗

      // ║ Phase 2: Anomalie-Check                   ║

      // ╚═══════════════════════════════════════════╝

      anomalies \= detectAnomalies(currentSnapshot)

      if anomalies.hasCritical():

        handleCriticalAnomalies(anomalies)

        continue  // Nächste Iteration, keine neuen Entries

      // ╔═══════════════════════════════════════════╗

      // ║ Phase 3: Bestehende Positionen prüfen      ║

      // ╚═══════════════════════════════════════════╝

      for position in activePositions:

        exitDecision \= evaluateExit(position)

        if exitDecision.shouldExit:

          await closePosition(position, exitDecision.reason)

      // ╔═══════════════════════════════════════════╗

      // ║ Phase 4: Neue Opportunities bewerten       ║

      // ╚═══════════════════════════════════════════╝

      opportunities \= \[\]

      for pairCombination in allPairCombinations(venues):

        result \= evaluateEntry(pairCombination)

        if result.accepted:

          opportunities.push(result)

      // Sortiere nach Netto-APR (beste zuerst)

      opportunities.sort(by: netAPR, descending)

      // ╔═══════════════════════════════════════════╗

      // ║ Phase 5: Rotationen prüfen                 ║

      // ╚═══════════════════════════════════════════╝

      for opp in opportunities:

        for position in activePositions:

          if opp.pair \== position.pair: continue  // Gleich Pair, kein Tausch

          rotation \= evaluateRotation(position, opp)

          if rotation.shouldRotate:

            await executeRotation(position, opp)

            break  // Eine Rotation pro Iteration

      // ╔═══════════════════════════════════════════╗

      // ║ Phase 6: Neue Positionen eröffnen           ║

      // ╚═══════════════════════════════════════════╝

      for opp in opportunities:

        if activePositions.count \>= config.maxConcurrentPositions:

          break

        if hasCapacityFor(opp.sizing.optimalSize):

          await executeEntry(opp)

      // Loop-Interval

      await sleep(config.scanIntervalMs)  // z.B. 50ms für 20 Scans/Sek

    catch error:

      log.error(error)

      await sleep(config.errorCooldownMs)

*Begründung der Phasen-Reihenfolge: (1) Daten zuerst, damit alle Entscheidungen auf dem gleichen Snapshot basieren. (2) Anomalien vor allem anderen, weil sie alle weiteren Aktionen blockieren können. (3) Exits vor neuen Entries, um Kapital freizumachen. (4) Opportunities bewerten bevor (5) Rotationen, weil Rotationen die Opportunity-Liste als Input brauchen. (6) Neue Entries zuletzt, weil sie das geringste Risiko tragen (kein bestehendes Exposure wird verändert).*

# **10\. Monitoring und Performance-Metriken**

## **10.1 Echtzeit-Dashboard-Metriken**

| Metrik | Berechnung | Zielwert |
| :---- | :---- | :---- |
| Portfolio Net APR | Summe aller Positions-APR gewichtet nach Größe | \> 10% |
| Avg. Slippage (Entry) | Durchschnittl. realisierte Slippage aller Entries | \< 10 bps |
| Avg. Slippage (Exit) | Durchschnittl. realisierte Slippage aller Exits | \< 15 bps |
| Fill Rate | Erfolgreiche Executions / Versuchte Executions | \> 90% |
| Leg-Divergenz-Rate | Einseitige Fills / Gesamte Fills | \< 5% |
| Rotation-Erfolgsrate | Profitable Rotationen / Gesamte Rotationen | \> 70% |
| Avg. Haltezeit | Durchschnittliche Positionsdauer in Stunden | Kontextabhängig |
| Capital Utilization | Genutztes Kapital / Verfügbares Kapital | 60-80% |
| Max Drawdown | Größter Verlust von Peak | \< 3% |
| Sharpe Ratio | (Return \- RiskFree) / StdDev | \> 2.0 |

## **10.2 Post-Trade-Analyse**

Jeder Trade wird nach Abschluss analysiert, um den Algorithmus iterativ zu verbessern:

interface TradeAnalysis {

  // Kosten-Analyse

  estimatedSlippageBps: number,    // Vorhersage vor Trade

  realizedSlippageBps: number,     // Tatsächliche Slippage

  slippagePredictionError: number, // Differenz

  // Profitabilität

  estimatedAPR: number,            // Vorhersage

  realizedAPR: number,             // Tatsächlich

  fundingReceived: number,         // Absolute Funding-Erträge

  totalCosts: number,              // Alle Kosten (Fees \+ Slippage)

  netProfit: number,               // Netto

  // Execution-Qualität

  legTimeDifferenceMs: number,     // Zeit zwischen Leg-Fills

  priceMoveDuringExecution: number,// Preisbewegung während Execution

  // Lerneffekte

  bindingConstraint: string,       // Was hat die Positionsgröße limitiert?

  entryGatesPassed: string\[\],      // Welche Gates waren knapp?

}

*Begründung: Die Differenz zwischen geschätzter und realisierter Slippage ist der wichtigste Feedback-Loop. Wenn der Algo die Slippage systematisch unterschätzt, müssen die Sicherheitsmargen (z.B. der 1.5x-Buffer im Pre-Flight-Check) erhöht werden.*

# **11\. Implementierungs-Roadmap**

## **Phase 1: Foundation (Woche 1-2)**

* Exchange Adapter für beide Ziel-DEXes implementieren

* Orderbook-Scanner mit Slippage-Berechnung und Depth Profile

* Funding Rate Collector mit historischer Datenbank (bestehenden Scanner erweitern)

* Alle Berechnungen unit-testen mit synthetischen Orderbooks

## **Phase 2: Analyse-Engine (Woche 3-4)**

* OHI und TOHI implementieren und gegen Live-Daten validieren

* Historischer Funding-Spread-Filter mit allen Zeitfenstern

* Position Sizing mit allen 5 Constraints

* Backtesting-Framework: Alle Berechnungen auf historischen Daten laufen lassen

## **Phase 3: Execution (Woche 5-6)**

* Simultaneous Market Execution mit Fallback-Logik

* Paper Trading: Echte Orderbooks, simulierte Fills

* Monitoring-Dashboard mit allen Metriken aus Abschnitt 10

## **Phase 4: Live (Woche 7-8)**

* Start mit minimalem Kapital und konservativen Parametern

* Schrittweise Erhöhung nach Validierung der Slippage-Vorhersagen

* Maker-Taker Hybrid implementieren nach genügend Daten

* Rotationslogik erst aktivieren wenn Entry/Exit stabil läuft

*Hinweis: Dieses Framework ist bewusst exchange-agnostisch gehalten. Die gesamte mathematische Logik bleibt identisch – nur die Exchange-Adapter müssen für jede neue Venue implementiert werden. Die Algorithmen, Schwellenwerte und Formeln sind universell anwendbar auf jedes Orderbook-basierte Perpetual-Futures-System.*