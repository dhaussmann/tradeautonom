# V5: Orderbook Management Service (OMS) & Advanced Features

## Inhaltsverzeichnis

1. [Architektur-Überblick](#1-architektur-überblick)
2. [Orderbook Management Service (OMS)](#2-orderbook-management-service-oms)
3. [Auto-Discovery](#3-auto-discovery)
4. [Orderbook-Datenfluss: Vorher vs. Nachher](#4-orderbook-datenfluss-vorher-vs-nachher)
5. [Opt-in Features (fn_opt_*)](#5-opt-in-features)
6. [Position Opening Logic (Entry Flow)](#6-position-opening-logic-entry-flow)
7. [UI-Integration](#7-ui-integration)
8. [Deployment](#8-deployment)
9. [API-Referenz](#9-api-referenz)

---

## 1. Architektur-Überblick

```
                  ┌─────────────────────────────┐
                  │   OMS (Port 8099)            │
                  │   monitor_service.py         │
                  │                              │
                  │   ┌──── Extended WS ────┐    │
                  │   ├──── GRVT WS ────────┤    │
                  │   ├──── Nado WS ────────┤    │
                  │   └──── Variational ────┘    │
                  │          (REST Poll)         │
                  │                              │
                  │   317 Feeds, 114 Tokens      │
                  │   GET /book/{exch}/{sym}     │
                  └──────────────┬────────────────┘
                                 │ HTTP Poll (500ms)
                  ┌──────────────┴────────────────┐
                  │   Bot Container (Port 8005)   │
                  │   DataLayer → OMS Poll Mode   │
                  │                              │
                  │   Engine → StateMachine       │
                  │   FundingMonitor              │
                  │   RiskManager                 │
                  └──────────────┬────────────────┘
                                 │ SSE Stream
                  ┌──────────────┴────────────────┐
                  │   Frontend (Cloudflare)       │
                  │   bot.defitool.de             │
                  └───────────────────────────────┘
```

**Kernidee**: Statt dass jeder Bot-Container eigene WebSocket-Verbindungen zu allen Exchanges unterhält (was Rate-Limits und Ressourcen verbraucht), gibt es einen zentralen OMS-Container, der **alle** Orderbook-Feeds sammelt. Bot-Container lesen per HTTP-Poll die aktuellen Snapshots.

---

## 2. Orderbook Management Service (OMS)

### Was ist der OMS?

Ein standalone FastAPI-Service (`deploy/monitor/monitor_service.py`), der:

- **WebSocket-Verbindungen** zu Extended, GRVT und Nado aufrechterhält
- **REST-Polling** für Variational durchführt (da Variational kein WS-Orderbook hat)
- **Orderbook-Snapshots** im RAM cached und per REST-API bereitstellt
- **Auto-Discovery** aller handelbaren Tokens über alle 4 Exchanges durchführt

### Datenmodell

Jeder Feed wird als `BookSnapshot` gespeichert:

```python
@dataclass
class BookSnapshot:
    bids: list[list]       # [[price, qty], ...] sortiert absteigend
    asks: list[list]       # [[price, qty], ...] sortiert aufsteigend
    timestamp_ms: float    # Zeitpunkt des letzten Updates
    connected: bool        # WS-Verbindung steht
    update_count: int      # Anzahl empfangener Updates
```

### Exchange-spezifische Feeds

| Exchange     | Protokoll      | Besonderheit |
|-------------|----------------|--------------|
| **Extended** | WebSocket       | Ein WS pro Symbol, URL-Template mit Symbol-Pfad |
| **GRVT**     | WebSocket       | Shared WS, Subscribe per JSON-Message, L2 Snapshots |
| **Nado**     | WebSocket       | Shared WS, `book_depth` Stream mit x18-Format Preisen |
| **Variational** | REST Poll (5s) | Kein WS-Orderbook. Synthetisches Book aus Stats-API Quotes |

### Variational: Einzelner Shared Poll

Variational hat kein echtes Orderbook (RFQ-basiert). Der OMS nutzt einen **einzigen** API-Call zur Stats-API (`/metadata/stats`), der alle Listings zurückgibt. Daraus werden synthetische Orderbooks für alle Variational-Symbole gebaut:

```
Stats-API Response:
  listings: [
    { ticker: "BTC", quotes: { size_1k: {bid, ask}, size_100k: {bid, ask}, size_1m: {bid, ask} } },
    { ticker: "ETH", quotes: { ... } },
    ...
  ]

→ Pro Symbol: 3 Bid/Ask Levels (1k, 100k, 1m Tiers)
→ Ein API-Call für alle ~95 Variational-Symbole
```

Vorher: Ein API-Call **pro Symbol** pro Poll-Intervall → 95 Calls/5s = 19 Calls/s.
Nachher: **1 Call/5s** für alle 95 Symbole.

---

## 3. Auto-Discovery

### Ablauf beim OMS-Start

1. **Marktlisten abrufen** von allen 4 Exchanges parallel:
   - Extended: `GET /api/v1/info/markets` → aktive Märkte
   - GRVT: `POST /full/v1/all_instruments` → aktive Instrumente
   - Nado: `GET /symbols` → Perpetual-Märkte
   - Variational: `GET /metadata/stats` → Listings

2. **Token-Normalisierung** — jede Exchange hat eigene Symbolformate:

   | Exchange     | Rohformat            | Normalisierter Base-Token |
   |-------------|----------------------|--------------------------|
   | Extended     | `BTC-USDC`           | `BTC`                    |
   | Extended     | `1000PEPE-USDC`      | `PEPE`                   |
   | GRVT         | `BTC_USDT_Perp`      | `BTC`                    |
   | Nado         | `kBONK-PERP`         | `BONK`                   |
   | Nado         | `ETH-PERP`           | `ETH`                    |
   | Variational  | `P-SOL-USDC-28800`   | `SOL`                    |

   Regeln:
   - Extended: `name.split("-")[0]`, `1000`-Prefix entfernen, Equity-Tokens (`AAPL_24_5`) überspringen
   - GRVT: `base`-Feld direkt verwenden
   - Nado: `-PERP` entfernen, `k`-Prefix entfernen (kBONK → BONK)
   - Variational: `ticker`-Feld direkt verwenden

3. **Overlap berechnen** — Token gilt als "trackbar" wenn er auf `>= MIN_EXCHANGES` Exchanges existiert:

   ```
   Ergebnis (Stand Deployment):
   - 31 Tokens auf allen 4 Exchanges
   - 27 Tokens auf 3 Exchanges
   - 56 Tokens auf 2 Exchanges
   ────────────────────────────────
   - 114 Base-Tokens total
   - 317 Feeds (82 Extended + 89 GRVT + 51 Nado + 95 Variational)
   ```

4. **Feeds starten** — für jeden gefundenen (exchange, symbol) Paar wird ein WS-/Poll-Task gestartet.

### Konfiguration

| Env-Variable         | Default    | Beschreibung |
|---------------------|-----------|--------------|
| `OMS_TRACKED_PAIRS` | `auto`     | `auto` = Auto-Discovery, oder `extended:BTC-USDC,grvt:BTC_USDT_Perp,...` |
| `OMS_MIN_EXCHANGES` | `2`        | Mindestanzahl Exchanges für Auto-Tracking |
| `OMS_GRVT_ENV`      | `prod`     | GRVT-Umgebung (dev/staging/testnet/prod) |
| `OMS_NADO_ENV`      | `mainnet`  | Nado-Umgebung (mainnet/testnet) |

---

## 4. Orderbook-Datenfluss: Vorher vs. Nachher

### Vorher (Ohne OMS)

```
Bot Container A ──WS──→ Extended
Bot Container A ──WS──→ GRVT
Bot Container A ──REST─→ Variational

Bot Container B ──WS──→ Extended    (Duplikat!)
Bot Container B ──WS──→ GRVT       (Duplikat!)
Bot Container B ──REST─→ Variational (Duplikat!)
```

Jeder Container unterhält eigene Verbindungen. Bei N Bots × M Symbole = N×M WebSocket-Verbindungen.

### Nachher (Mit OMS)

```
OMS ←─WS──→ Extended     (317 Feeds, 1× pro Symbol)
OMS ←─WS──→ GRVT
OMS ←─WS──→ Nado
OMS ←─REST─→ Variational (1 API-Call für alle)

Bot A ←─WS──→ OMS /ws  (Echtzeit, <10ms Latenz)
Bot B ←─WS──→ OMS /ws  (selbe Verbindung, subscribe nur eigene Symbole)
```

### OMS WebSocket-Protokoll

Der OMS stellt unter `/ws` einen WebSocket-Endpoint bereit. Bots subscriben nur die Symbole die sie brauchen und erhalten Updates in Echtzeit:

```json
// Bot → OMS: Subscribe
{"action": "subscribe", "exchange": "extended", "symbol": "SOL-USD"}

// OMS → Bot: Sofortiger Initial-Snapshot + danach jedes Update
{"type": "book", "exchange": "extended", "symbol": "SOL-USD",
 "bids": [[price, qty], ...], "asks": [[price, qty], ...],
 "timestamp_ms": 1234567890.123}

// Bot → OMS: Unsubscribe
{"action": "unsubscribe", "exchange": "extended", "symbol": "SOL-USD"}
```

### DataLayer Routing-Logik (`data_layer.py`)

```python
# In start(): Eine shared WS-Verbindung statt N×M Polls
if self._shared_monitor_url:
    # Primär: WS-Verbindung zum OMS (alle Symbole über 1 Connection)
    await self._run_oms_ws()
    # Fallback bei WS-Fehler: HTTP Poll pro Symbol
else:
    # Kein OMS: direkte WS-Verbindungen zu Exchanges
    if exch_name == "extended": ...
    elif exch_name == "grvt": ...
    elif exch_name == "nado": ...
    elif exch_name == "variational": ...
```

### Dreistufiger Fallback

```
1. OMS WebSocket  →  Echtzeit-Updates (<10ms Latenz)
       ↓ (5× Fehler)
2. OMS HTTP Poll  →  500ms Polling als Backup
       ↓ (10× Fehler)
3. Direkte WS     →  Eigene Exchange-Verbindungen
```

Dies garantiert, dass der Bot auch bei OMS-Ausfall weiter funktioniert.

---

## 5. Opt-in Features

Alle neuen Features sind **standardmäßig deaktiviert** und können per UI oder Env-Variable aktiviert werden.

### Opt 1: Depth-Aware Spread (`fn_opt_depth_spread`)

**Problem**: BBO-Spread (Best Bid vs. Best Ask) ignoriert die tatsächliche Orderbook-Tiefe. Bei dünnen DEX-Orderbooks kann der reale Ausführungspreis deutlich schlechter sein als der BBO.

**Lösung**: VWAP-basierte Fill-Price-Simulation statt BBO-Vergleich.

```
BBO-Spread:   long_ask[0] vs short_bid[0]  → oberflächlich
Exec-Spread:  VWAP_buy(qty) vs VWAP_sell(qty) → realistisch
Slippage:     exec_spread - bbo_spread → zusätzliche Kosten durch Tiefe
```

**Modul**: `app/spread_analyzer.py`

1. `estimate_fill_price(book, "buy", qty)` — simuliert einen Market Buy über die Ask-Seite
2. `estimate_fill_price(book, "sell", qty)` — simuliert einen Market Sell über die Bid-Seite
3. Differenz = erwarteter Slippage in Basis Points
4. Wenn `slippage_bps > max_slippage_bps` → wartet 2s und prüft erneut

**Konfiguration**:

| Parameter | Default | Beschreibung |
|-----------|---------|--------------|
| `fn_opt_depth_spread` | `false` | Feature aktivieren |
| `fn_opt_max_slippage_bps` | `10.0` | Max. erlaubter Slippage (Basis Points) |

### Opt 2: OHI Monitoring (`fn_opt_ohi_monitoring`)

**Orderbook Health Index** — ein gewichteter Score (0-1) der die Qualität eines Orderbooks bewertet.

**Berechnung** (`data_layer.py → get_orderbook_health()`):

```
OHI = 0.4 × Spread-Score + 0.3 × Depth-Score + 0.3 × Symmetry-Score

Spread-Score:    1.0 - (spread_bps / 50)       → 0 bps = 1.0, 50+ bps = 0.0
Depth-Score:     log1p(depth_usd) / log1p(100k) → $100k+ = 1.0, $0 = 0.0
Symmetry-Score:  min(bid_depth, ask_depth) / max(bid_depth, ask_depth)
                                                 → 1:1 = 1.0, stark einseitig = 0.0
```

**Depth-Berechnung**: Summiert `price × qty` aller Levels innerhalb ±0.5% des Mid-Prices.

**Interpretation**:

| OHI-Wert | Bedeutung | Farbe (UI) |
|----------|-----------|------------|
| ≥ 0.70   | Gesund — tiefes, symmetrisches Book | Grün |
| 0.40-0.69 | Mittel — akzeptabel für kleine Orders | Indigo |
| < 0.40   | Dünn — hohes Slippage-Risiko | Orange |

**Konfiguration**:

| Parameter | Default | Beschreibung |
|-----------|---------|--------------|
| `fn_opt_ohi_monitoring` | `false` | OHI im Dashboard anzeigen |
| `fn_opt_min_ohi` | `0.4` | Min OHI für Entry (0 = nur anzeigen) |

### Opt 3: V4 Funding History (`fn_opt_funding_history`)

**Problem**: Aktuelle Funding-Rates sind nur eine Momentaufnahme. Ein Pair kann gerade attraktiv aussehen, aber historisch instabil sein.

**Lösung**: Abruf historischer Funding-Daten von einer externen API (`fundingrate.de`), um die **Spread-Konsistenz** über Zeit zu bewerten.

**Daten** (`funding_monitor.py → _fetch_v4_data()`):

| Feld | Beschreibung |
|------|-------------|
| `pair_found` | Ob das Exchange-Pair in der V4-DB existiert |
| `spread_apr` | Annualisierter Spread (positiv = profitabel) |
| `spread_consistency` | Wie stabil der Spread über Zeit ist (0-1) |
| `confidence_score` | Gesamtbewertung des Pairs (0-100) |
| `volume_depth` | Volumen-Tiefe-Score |
| `rate_stability` | Stabilität der Funding-Rate |

**Entry-Guard**: Wenn aktiv und `spread_consistency < fn_opt_min_funding_consistency`, wird dies im Dashboard angezeigt (aktuell kein harter Block, nur Information).

**Konfiguration**:

| Parameter | Default | Beschreibung |
|-----------|---------|--------------|
| `fn_opt_funding_history` | `false` | Feature aktivieren |
| `fn_opt_min_funding_consistency` | `0.3` | Min Consistency Score (0-1) |
| `fn_opt_funding_api_url` | `https://api.fundingrate.de` | V4 API Base URL |

### Opt 4: Dynamic Sizing (`fn_opt_dynamic_sizing`)

**Problem**: Feste Position-Größe ignoriert verfügbares Kapital und Orderbook-Liquidität.

**Lösung**: Automatische Berechnung der optimalen Positionsgröße basierend auf drei Constraints:

```
recommended_qty = min(capital_limit, per_pair_limit, liquidity_limit)
```

**Constraint 1 — Capital**:
```
max_notional = collateral_usd × leverage × max_utilization
capital_qty = max_notional / mark_price
```

**Constraint 2 — Per-Pair Ratio**:
```
per_pair_notional = collateral_usd × leverage × max_per_pair_ratio
per_pair_qty = per_pair_notional / mark_price
```

**Constraint 3 — Liquidity** (Binary Search):
```
Für qty in [min_qty, min(capital_qty, per_pair_qty)]:
    buy_fill  = VWAP_buy(long_book, qty)
    sell_fill = VWAP_sell(short_book, qty)
    buy_slip  = (buy_fill - mid) / mid × 10000 bps
    sell_slip = (mid - sell_fill) / mid × 10000 bps
    → Beide Seiten müssen innerhalb max_slippage_bps liegen

→ Binary Search (10 Iterationen) findet maximale Qty
```

**Modul**: `app/position_sizer.py`

**Konfiguration**:

| Parameter | Default | Beschreibung |
|-----------|---------|--------------|
| `fn_opt_dynamic_sizing` | `false` | Feature aktivieren |
| `fn_opt_max_utilization` | `0.80` | Max. Kapitalnutzung (0-1) |
| `fn_opt_max_per_pair_ratio` | `0.25` | Max. Anteil pro Pair (0-1) |
| `fn_opt_max_slippage_bps` | `10.0` | Slippage-Budget (geteilt mit Opt 1) |

### Opt 5: Shared OMS (`fn_opt_shared_monitor_url`)

Verbindet den Bot mit dem OMS statt eigene WS-Verbindungen aufzubauen.

| Parameter | Default | Beschreibung |
|-----------|---------|--------------|
| `fn_opt_shared_monitor_url` | `""` (leer) | OMS URL, z.B. `http://192.168.133.253:8099` |

### Opt 6: Taker Drift Guard (`fn_opt_taker_drift_guard`)

**Problem**: Während der Maker-Order auf Fill wartet (bis zu 15s), wird das Taker-Orderbook nicht überwacht. Wenn der Taker-Preis wegdriftet, wird der anschließende Hedge zu teuer oder misslingt.

**Lösung**: Ein paralleler Monitor-Task prüft während des Maker-Waits alle ~1s den Taker-Mid-Price. Driftet er zu weit vom Baseline ab, wird die Maker-Order proaktiv gecancelt und die Spread-Gates werden neu evaluiert.

**Ablauf**:
1. Beim Start des Maker-Waits: Taker-Mid-Price als Baseline speichern
2. Alle ~1s: Taker-Orderbook lesen → aktuellen Mid berechnen
3. `drift_bps = |current_mid - baseline_mid| / baseline_mid × 10000`
4. Wenn `drift_bps > max_taker_drift_bps`: Maker-Order canceln → zurück zu Spread-Gates

**Konfiguration**:

| Parameter | Default | Beschreibung |
|-----------|---------|--------------|
| `fn_opt_taker_drift_guard` | `false` | Feature aktivieren |
| `fn_opt_max_taker_drift_bps` | `3.0` | Max. erlaubter Taker-Drift (Basis Points) |

---

## 6. Position Opening Logic (Entry Flow)

Der vollständige Entry-Ablauf mit allen neuen Features:

```
User klickt "Start" in der UI
        │
        ▼
1. Engine.manual_entry()
   ├── Exchange-Clients validieren
   ├── Symbole auflösen (instrument_a, instrument_b)
   ├── Maker/Taker-Rollen zuweisen
   │   (Maker = Limit Orders, Taker = IOC Market Hedge)
   │
   ├── Pre-Trade Risk Check
   │   └── RiskManager.pre_trade_check() für beide Seiten
   │
   ├── BBO Spread Check (Log only, kein Block)
   │   └── RiskManager.check_spread()
   │
   ├── [OPT 4] Dynamic Sizing
   │   ├── Orderbooks von DataLayer holen
   │   │   └── DataLayer liest aus OMS (Opt 5) oder direkte WS
   │   ├── Balance von beiden Exchanges abrufen
   │   ├── compute_position_size():
   │   │   ├── Capital Constraint
   │   │   ├── Per-Pair Constraint
   │   │   └── Liquidity Constraint (Binary Search)
   │   └── qty = min(user_qty, recommended_qty)
   │
   ├── MakerTakerConfig erstellen
   │   ├── use_depth_spread = fn_opt_depth_spread
   │   └── max_slippage_bps = fn_opt_max_slippage_bps
   │
   └── StateMachine.execute_entry(config)
        │
        ▼
2. TWAP-Ausführung (Chunk für Chunk)
   │
   FOR each chunk (1..num_chunks):
   │
   ├── Pre-Chunk Spread Check:
   │   │
   │   ├── [OPT 1] Depth-Aware Spread Guard
   │   │   ├── Beide Orderbooks laden (aus OMS oder WS)
   │   │   ├── analyze_cross_venue_spread():
   │   │   │   ├── VWAP Fill Price Long Side berechnen
   │   │   │   ├── VWAP Fill Price Short Side berechnen
   │   │   │   ├── Execution Spread = (long_fill - short_fill) / short_fill
   │   │   │   └── Slippage = exec_spread - bbo_spread
   │   │   ├── slippage_bps <= max_slippage_bps? → weiter
   │   │   └── Sonst: 2s warten, erneut prüfen (Loop)
   │   │
   │   ├── BBO Spread Guard (min_spread_pct / max_spread_pct)
   │   │   ├── long_ask vs short_bid berechnen
   │   │   ├── Spread innerhalb Grenzen? → weiter
   │   │   └── Sonst: 2s warten, erneut prüfen (Loop)
   │   │
   │   └── Beide Guards bestanden → Chunk ausführen
   │
   ├── Maker Order (Limit) platzieren
   │   ├── Berechne Preis: BBO ± offset_ticks
   │   ├── Order absetzen
   │   ├── Warten auf Fill (WS Account Stream + REST Fallback)
   │   ├── Repricing bei Timeout (bis max_chase_rounds)
   │   └── Maker gefüllt → Taker Hedge triggern
   │
   ├── Taker Hedge (IOC Market Order)
   │   ├── Berechne Preis: BBO ± slippage_buffer
   │   ├── IOC Order absetzen
   │   ├── Fill-Detection: WS first (500ms), REST fallback
   │   └── Repair bei Teilfüllung
   │
   ├── Position-State aktualisieren
   │   ├── long_qty, short_qty aktualisieren
   │   └── Chunk-Result loggen
   │
   └── Inter-Chunk Pause (twap_interval_s)

        │
        ▼
3. Entry Complete
   ├── VWAP Entry Prices berechnen (über alle Chunks)
   ├── Position auf Disk persistieren
   ├── State → HOLDING
   └── Activity Log: "Entry COMPLETE"
```

### Orderbook-Datenquellen im Entry Flow

An jedem Punkt wo Orderbook-Daten gelesen werden, ist der Pfad:

```
StateMachine._get_book()
    └── DataLayer.get_orderbook()
        ├── [OMS Modus] → HTTP GET /book/{exch}/{sym} an OMS
        │                  (alle 500ms gepollt, Snapshot aus RAM)
        └── [Direkt Modus] → Eigene WS-Verbindung
                              (Extended/GRVT/Nado: WS, Variational: REST)
```

---

## 7. UI-Integration

### Advanced Settings Panel

Auf der Bot-Detailseite unter **"▸ Advanced Settings"** (aufklappbar):

```
┌─────────────────────────────────────────────────┐
│ ● OMS Connected                                 │
│   Polling from http://192.168.133.253:8099      │
│─────────────────────────────────────────────────│
│ [x] Depth Spread     [x] OHI Monitoring         │
│ [x] V4 Funding       [x] Dynamic Sizing         │
│ Max Slippage: [10] bps                          │
│ Min Consistency: [0.30]                         │
└─────────────────────────────────────────────────┘
```

- **OMS Status**: Grüner Dot + URL wenn verbunden, grauer Dot wenn deaktiviert
- **Toggle-Switches**: Sofortige Speicherung per `PATCH /config`
- **Numerische Inputs**: Gespeichert bei Enter oder Blur (SSE-Stream überschreibt nicht während Fokus)

### OHI-Anzeige

Im Exchange-Panel (je Long/Short Side):

```
[0.82] ████████░░  ETH-USDC
       3.2bps  $145k depth
```

Farbcodierung: Grün ≥ 0.7 | Indigo ≥ 0.4 | Orange < 0.4

### V4 Funding Panel

```
Spread APR (V4):  +12.45%
Score:            78
Consistency:      0.67
Vol. Depth:       0.83
Stability:        0.91
```

---

## 8. Deployment

### OMS

```bash
# Auf dem NAS deployen
./deploy/monitor/deploy.sh sync build up

# Status prüfen
./deploy/monitor/deploy.sh status
./deploy/monitor/deploy.sh logs

# Neustart
./deploy/monitor/deploy.sh restart
```

**Port**: 8099
**Pfad auf NAS**: `/volume1/docker/tradeautonom/oms`

### Bot Container (V3)

```bash
./deploy/v3/deploy.sh
```

**Port**: 8005
**OMS-Verbindung**: `FN_OPT_SHARED_MONITOR_URL=http://192.168.133.253:8099` in `.env.container`

> **Wichtig**: Synology NAS unterstützt kein `host.docker.internal`. Die NAS-IP (`192.168.133.253`) muss direkt verwendet werden.

### Frontend

```bash
./deploy/cloudflare/deploy.sh
```

**URL**: https://bot.defitool.de

---

## 9. API-Referenz

### OMS Endpoints

| Methode | Pfad | Beschreibung |
|---------|------|-------------|
| `GET` | `/health` | `{status, feeds, timestamp}` |
| `GET` | `/status` | Alle Feeds mit connected/age_ms/updates |
| `GET` | `/book/{exchange}/{symbol}` | Orderbook Snapshot (20 Levels) |
| `GET` | `/tracked` | Auto-discovered Pairs nach Base-Token gruppiert |

### Bot Config Felder

| Feld | Typ | Default | Beschreibung |
|------|-----|---------|-------------|
| `fn_opt_depth_spread` | bool | `false` | VWAP Spread Guard |
| `fn_opt_max_slippage_bps` | float | `10.0` | Max Slippage (bps) |
| `fn_opt_ohi_monitoring` | bool | `false` | OHI Dashboard |
| `fn_opt_min_ohi` | float | `0.4` | Min OHI für Entry |
| `fn_opt_funding_history` | bool | `false` | V4 Funding API |
| `fn_opt_min_funding_consistency` | float | `0.3` | Min Consistency |
| `fn_opt_dynamic_sizing` | bool | `false` | Auto Position Sizing |
| `fn_opt_max_utilization` | float | `0.80` | Max Kapitalnutzung |
| `fn_opt_max_per_pair_ratio` | float | `0.25` | Max pro Pair |
| `fn_opt_shared_monitor_url` | string | `""` | OMS URL |
| `fn_opt_taker_drift_guard` | bool | `false` | Taker Drift Guard |
| `fn_opt_max_taker_drift_bps` | float | `3.0` | Max Taker-Drift (bps) |

### Relevante Dateien

| Datei | Funktion |
|-------|----------|
| `deploy/monitor/monitor_service.py` | OMS: Auto-Discovery, WS-Feeds, REST-API |
| `deploy/monitor/deploy.sh` | OMS Deployment Script |
| `app/data_layer.py` | Orderbook-Routing (OMS vs. Direkt), OHI-Berechnung |
| `app/spread_analyzer.py` | VWAP Cross-Venue Spread Analyse |
| `app/position_sizer.py` | Dynamic Position Sizing (Binary Search) |
| `app/funding_monitor.py` | Funding Rates + V4 History API |
| `app/safety.py` | Fill-Price-Schätzung, Depth-Checks |
| `app/engine.py` | Entry/Exit Flow, Feature Flag Integration |
| `app/state_machine.py` | TWAP-Ausführung mit Spread Guards |
| `app/config.py` | Alle `fn_opt_*` Konfigurationsfelder |
| `frontend/src/types/bot.ts` | TypeScript-Typen für OHI, FundingV4, Config |
| `frontend/src/views/BotDetailView.vue` | Advanced Settings Panel, OMS Status |
