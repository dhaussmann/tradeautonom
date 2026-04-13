# Funding Rate API V4 — Web Developer Guide

**Base URL:** `https://api.fundingrate.de`
**Version:** V4
**Last updated:** April 2026

---

## Table of Contents

1. [Quick Start](#1-quick-start)
2. [General Information](#2-general-information)
3. [Endpoints Overview](#3-endpoints-overview)
4. [Markets](#4-markets)
5. [History](#5-history)
6. [Moving Averages](#6-moving-averages)
7. [Arbitrage](#7-arbitrage)
8. [Single Coin Analysis](#8-single-coin-analysis)
9. [Exchanges](#9-exchanges)
10. [Spreads](#10-spreads)
11. [Data Types & Enums](#11-data-types--enums)
12. [Confidence Score](#12-confidence-score)
13. [Error Handling](#13-error-handling)
14. [Usage Examples](#14-usage-examples)
15. [TypeScript Interfaces](#15-typescript-interfaces)

---

## 1. Quick Start

No API key required. All read endpoints are public GET requests with CORS enabled.

```bash
# Get all live market data
curl https://api.fundingrate.de/api/v4/markets

# Get BTC across all exchanges
curl https://api.fundingrate.de/api/v4/markets/BTC

# Get best arbitrage opportunities
curl https://api.fundingrate.de/api/v4/arbitrage

# Get 7-day moving averages
curl "https://api.fundingrate.de/api/v4/ma/latest?period=7d"

# Get full analysis for ETH
curl https://api.fundingrate.de/api/v4/analysis/ETH
```

```typescript
// TypeScript / JavaScript
const res = await fetch('https://api.fundingrate.de/api/v4/markets?symbol=BTC');
const json = await res.json();
// json.success === true
// json.data === [ { normalized_symbol: 'BTC', exchange: 'hyperliquid', ... }, ... ]
// json.count === 25
```

---

## 2. General Information

### Base URL

```
https://api.fundingrate.de
```

### Authentication

No authentication required for all read (GET) endpoints.

### CORS

All endpoints return the following CORS headers:

```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, POST, OPTIONS
Access-Control-Allow-Headers: Content-Type
```

### Rate Limits

No enforced rate limits. Recommended: max 1 request per second per endpoint. Data updates every 5 minutes — polling faster yields no new data.

### Caching

Responses are server-side cached. Cache headers indicate freshness:

| Header | Description |
|--------|-------------|
| `X-Cache` | `HIT` or `MISS` — whether response came from cache |
| `X-Cache-Key` | The cache key used |

Cache TTLs:

| Endpoint pattern | TTL |
|------------------|-----|
| `/api/v4/markets` | 60s |
| `/api/v4/markets/latest` | 60s |
| `/api/v4/ma/latest` | 300s |
| `/api/v4/arbitrage` | 300s |
| `/api/v4/exchanges` | 300s |

### Response Wrapper

All responses follow this structure:

**Success:**
```json
{
  "success": true,
  "data": [...],
  "count": 123
}
```

**Error:**
```json
{
  "success": false,
  "error": "Error message"
}
```

### Funding Rate Format

All funding rates are returned as **annualized APR in decimal format**:

| Value | Meaning |
|-------|---------|
| `0.15` | 15% APR |
| `1.0` | 100% APR |
| `-0.05` | -5% APR |
| `0.001` | 0.1% APR |

To display as percentage: multiply by 100.

```typescript
const displayPercent = (funding_rate_apr * 100).toFixed(2) + '%';
// 0.15 → "15.00%"
```

### Data Freshness

- **Live data** (`/api/v4/markets`): Updated every 5 minutes
- **Moving averages** (`/api/v4/ma/*`): Recalculated every 5 minutes
- **Arbitrage** (`/api/v4/arbitrage`): Based on live or MA data
- **Historical** (`/api/v4/history/*`): 3-month retention

---

## 3. Endpoints Overview

### Public Read Endpoints (GET)

| Endpoint | Description |
|----------|-------------|
| [`/api/v4/markets`](#get-apiv4markets) | All live market snapshots |
| [`/api/v4/markets/latest`](#get-apiv4marketslatest) | Best APR per symbol (deduplicated) |
| [`/api/v4/markets/{symbol}`](#get-apiv4marketssymbol) | All exchanges for one symbol |
| [`/api/v4/history/{symbol}`](#get-apiv4historysymbol) | Historical time-series data |
| [`/api/v4/ma/latest`](#get-apiv4malatest) | Latest moving averages |
| [`/api/v4/ma/latest/{symbol}`](#get-apiv4malatestsymbol) | MA data for one symbol |
| [`/api/v4/ma/history/{symbol}`](#get-apiv4mahistorysymbol) | Historical MA values |
| [`/api/v4/arbitrage`](#get-apiv4arbitrage) | Arbitrage opportunities |
| [`/api/v4/analysis/{symbol}`](#get-apiv4analysissymbol) | Comprehensive single-coin analysis |
| [`/api/v4/exchanges`](#get-apiv4exchanges) | List of supported exchanges |
| [`/api/v4/exchanges/{key}/logo`](#get-apiv4exchangeskeylogo) | Exchange logo image |
| [`/api/v4/spreads`](#get-apiv4spreads) | Spread data query |

---

## 4. Markets

### `GET /api/v4/markets`

Returns all live market data snapshots. Each row represents one symbol on one exchange.

**Query Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `exchange` | string | No | all | Filter by exchange (e.g. `hyperliquid`) |
| `symbol` | string | No | all | Filter by ticker symbol (e.g. `BTC`) |
| `type` | string | No | all | Filter by market type: `crypto`, `stock`, `forex`, `etf`, `index`, `commodity` |
| `limit` | number | No | 5000 | Max results (max 10000) |

**Example Request:**

```bash
curl "https://api.fundingrate.de/api/v4/markets?exchange=hyperliquid&type=crypto&limit=10"
```

**Example Response:**

```json
{
  "success": true,
  "data": [
    {
      "normalized_symbol": "BTC",
      "exchange": "hyperliquid",
      "collected_at": 1773224551,
      "funding_rate_apr": 0.0688,
      "market_price": 73261.00,
      "open_interest": 1933163139.92,
      "max_leverage": 40,
      "volume_24h": 2050817569.09,
      "spread_bid_ask": 0.0014,
      "price_change_24h": 2.10,
      "market_type": "crypto"
    },
    {
      "normalized_symbol": "ETH",
      "exchange": "hyperliquid",
      "collected_at": 1773224551,
      "funding_rate_apr": 0.0412,
      "market_price": 3850.50,
      "open_interest": 850000000.00,
      "max_leverage": 50,
      "volume_24h": 780000000.00,
      "spread_bid_ask": 0.0012,
      "price_change_24h": 1.35,
      "market_type": "crypto"
    }
  ],
  "count": 2
}
```

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `normalized_symbol` | string | Ticker symbol (uppercase, e.g. `BTC`, `ETH`, `TSLA`) |
| `exchange` | string | Exchange identifier (lowercase, e.g. `hyperliquid`) |
| `collected_at` | number | Unix timestamp (seconds) when data was collected |
| `funding_rate_apr` | number | Annualized funding rate (decimal). `0.15` = 15% APR |
| `market_price` | number \| null | Current mark price in USD |
| `open_interest` | number \| null | Open interest in USD |
| `max_leverage` | number \| null | Maximum leverage available |
| `volume_24h` | number \| null | 24-hour trading volume in USD |
| `spread_bid_ask` | number \| null | Bid-ask spread as percentage |
| `price_change_24h` | number \| null | 24-hour price change in percent |
| `market_type` | string | Market classification: `crypto`, `stock`, `forex`, `etf`, `index`, `commodity` |

---

### `GET /api/v4/markets/latest`

Returns the **best APR per symbol** — one row per ticker, showing the exchange with the highest funding rate.

**Query Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `type` | string | No | all | Filter by market type |
| `limit` | number | No | 5000 | Max results (max 10000) |

**Example Request:**

```bash
curl "https://api.fundingrate.de/api/v4/markets/latest?type=crypto&limit=5"
```

**Example Response:**

```json
{
  "success": true,
  "data": [
    {
      "normalized_symbol": "POPCAT",
      "exchange": "drift",
      "collected_at": 1773224551,
      "funding_rate_apr": 1.8523,
      "market_price": 0.42,
      "open_interest": 5200000,
      "max_leverage": 20,
      "volume_24h": 12000000,
      "spread_bid_ask": 0.15,
      "price_change_24h": 8.5,
      "market_type": "crypto"
    }
  ],
  "count": 1
}
```

---

### `GET /api/v4/markets/{symbol}`

Returns all exchange data for a specific symbol.

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `symbol` | string | Yes | Ticker symbol (case-insensitive, e.g. `BTC`, `btc`, `ETH`) |

**Query Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `exchange` | string | No | all | Filter to a specific exchange |

**Example Request:**

```bash
curl https://api.fundingrate.de/api/v4/markets/BTC
```

**Example Response:**

```json
{
  "success": true,
  "data": [
    {
      "normalized_symbol": "BTC",
      "exchange": "paradex",
      "collected_at": 1773224551,
      "funding_rate_apr": 0.1250,
      "market_price": 73250.00,
      "open_interest": 450000000,
      "max_leverage": 20,
      "volume_24h": 320000000,
      "spread_bid_ask": 0.003,
      "price_change_24h": 2.10,
      "market_type": "crypto"
    },
    {
      "normalized_symbol": "BTC",
      "exchange": "hyperliquid",
      "collected_at": 1773224551,
      "funding_rate_apr": 0.0688,
      "market_price": 73261.00,
      "open_interest": 1933163139.92,
      "max_leverage": 40,
      "volume_24h": 2050817569.09,
      "spread_bid_ask": 0.0014,
      "price_change_24h": 2.10,
      "market_type": "crypto"
    }
  ],
  "count": 2,
  "symbol": "BTC"
}
```

---

## 5. History

### `GET /api/v4/history/{symbol}`

Returns historical time-series data from Cloudflare Analytics Engine. Data retention is **3 months**.

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `symbol` | string | Yes | Ticker symbol (case-insensitive) |

**Query Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `exchange` | string | No | all | Filter by exchange |
| `from` | number | No | now - 7 days | Start of time range (Unix seconds) |
| `to` | number | No | now | End of time range (Unix seconds) |
| `limit` | number | No | 1000 | Max rows (max 10000) |

**Example Request:**

```bash
# BTC history for last 24 hours on Hyperliquid
curl "https://api.fundingrate.de/api/v4/history/BTC?exchange=hyperliquid&from=1773138000&to=1773224400&limit=100"
```

**Example Response:**

```json
{
  "success": true,
  "data": [
    {
      "ticker": "BTC",
      "exchange": "hyperliquid",
      "market_type": "crypto",
      "collected_at": 1773224400,
      "funding_rate_apr": 0.0688,
      "market_price": 73261.00,
      "open_interest": 1933163139.92,
      "max_leverage": 40,
      "volume_24h": 2050817569.09,
      "spread_bid_ask": 0.0014,
      "price_change_24h": 2.10
    },
    {
      "ticker": "BTC",
      "exchange": "hyperliquid",
      "market_type": "crypto",
      "collected_at": 1773224100,
      "funding_rate_apr": 0.0695,
      "market_price": 73100.00,
      "open_interest": 1930000000.00,
      "max_leverage": 40,
      "volume_24h": 2040000000.00,
      "spread_bid_ask": 0.0015,
      "price_change_24h": 1.98
    }
  ],
  "count": 2,
  "symbol": "BTC",
  "from": 1773138000,
  "to": 1773224400
}
```

**Notes:**
- Data points are collected every 5 minutes (288 per day per exchange)
- Results are ordered by `collected_at` descending (newest first)
- Fields `market_price`, `open_interest`, `max_leverage`, `volume_24h`, `spread_bid_ask`, `price_change_24h` can be `null` for older migrated data

---

## 6. Moving Averages

### `GET /api/v4/ma/latest`

Returns the latest calculated moving averages for all symbols.

**Query Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `symbol` | string | No | all | Filter by ticker symbol |
| `period` | string | No | all | Filter by MA period (see periods below) |
| `exchange` | string | No | all | Filter by exchange. Use `_all` for cross-exchange aggregate |

**Available MA Periods:**

| Period | Description |
|--------|-------------|
| `1h` | 1-hour moving average |
| `4h` | 4-hour moving average |
| `8h` | 8-hour moving average |
| `12h` | 12-hour moving average |
| `1d` | 1-day moving average |
| `3d` | 3-day moving average |
| `7d` | 7-day moving average |
| `30d` | 30-day moving average |

**Example Request:**

```bash
# All 7-day MAs
curl "https://api.fundingrate.de/api/v4/ma/latest?period=7d"

# BTC MAs across all periods and exchanges
curl "https://api.fundingrate.de/api/v4/ma/latest?symbol=BTC"

# Cross-exchange aggregate for BTC
curl "https://api.fundingrate.de/api/v4/ma/latest?symbol=BTC&exchange=_all"
```

**Example Response:**

```json
{
  "success": true,
  "data": [
    {
      "normalized_symbol": "BTC",
      "exchange": "hyperliquid",
      "period": "7d",
      "ma_apr": 0.0523,
      "data_points": 2016,
      "period_start": 1772619600,
      "calculated_at": 1773224400
    },
    {
      "normalized_symbol": "BTC",
      "exchange": "_all",
      "period": "7d",
      "ma_apr": 0.0612,
      "data_points": 48384,
      "period_start": 1772619600,
      "calculated_at": 1773224400
    }
  ],
  "count": 2,
  "symbol": "BTC"
}
```

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `normalized_symbol` | string | Ticker symbol |
| `exchange` | string | Exchange name, or `_all` for cross-exchange average |
| `period` | string | MA period (`1h`, `4h`, `8h`, `12h`, `1d`, `3d`, `7d`, `30d`) |
| `ma_apr` | number | Moving average of funding_rate_apr (decimal) |
| `data_points` | number | Number of data points used for this average |
| `period_start` | number | Timestamp of the oldest data point included (Unix seconds) |
| `calculated_at` | number | When this MA was calculated (Unix seconds) |

---

### `GET /api/v4/ma/latest/{symbol}`

Returns all MA periods and exchanges for a single symbol.

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `symbol` | string | Yes | Ticker symbol (case-insensitive) |

**Query Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `exchange` | string | No | all | Filter by exchange |

**Example Request:**

```bash
curl https://api.fundingrate.de/api/v4/ma/latest/ETH
```

---

### `GET /api/v4/ma/history/{symbol}`

Returns historical MA values from Analytics Engine. Useful for charting MA trends over time.

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `symbol` | string | Yes | Ticker symbol (case-insensitive) |

**Query Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `exchange` | string | No | all | Filter by exchange |
| `period` | string | No | all | Filter by MA period |
| `from` | number | No | now - 30 days | Start of time range (Unix seconds) |
| `to` | number | No | now | End of time range (Unix seconds) |
| `limit` | number | No | 500 | Max rows (max 5000) |

**Example Request:**

```bash
# BTC 7d MA on Hyperliquid over the last 14 days
curl "https://api.fundingrate.de/api/v4/ma/history/BTC?exchange=hyperliquid&period=7d&limit=200"
```

**Example Response:**

```json
{
  "success": true,
  "data": [
    {
      "exchange": "hyperliquid",
      "period": "7d",
      "calculated_at": 1773224400,
      "ma_apr": 0.0523,
      "data_points": 2016,
      "period_start": 1772619600
    },
    {
      "exchange": "hyperliquid",
      "period": "7d",
      "calculated_at": 1773224100,
      "ma_apr": 0.0519,
      "data_points": 2015,
      "period_start": 1772619300
    }
  ],
  "count": 2,
  "symbol": "BTC",
  "from": 1772360400,
  "to": 1773224400
}
```

---

## 7. Arbitrage

### `GET /api/v4/arbitrage`

Finds the best perp-vs-perp funding rate spread pairs across exchanges. A positive spread means the short side pays a higher funding rate than the long side — the arbitrageur collects the difference.

**Query Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `period` | string | No | `live` | Data source: `live` = current snapshot, or `1h`, `4h`, `8h`, `12h`, `1d`, `3d`, `7d`, `30d` = MA-based |
| `exchange` | string | No | all | Only include pairs that feature this exchange |
| `exchanges` | string | No | — | Comma-separated list (e.g. `hyperliquid,paradex`). Only pairs between these exchanges |
| `allPairs` | boolean | No | `false` | If `true`, return all pair combinations (not just best per symbol) |
| `type` | string | No | all | Filter by market type (live period only) |
| `minSpread` | number | No | `0` (`0.05` when allPairs) | Minimum spread APR (decimal, e.g. `0.1` = 10%) |
| `minScore` | number | No | `1` (`0` when allPairs) | Minimum confidence score (0–4) |
| `includeAll` | boolean | No | `false` | Include all pairs regardless of score |
| `limit` | number | No | `100` (`500` when allPairs) | Max results (max 2000) |

**Example Request:**

```bash
# Top 10 live arbitrage opportunities
curl "https://api.fundingrate.de/api/v4/arbitrage?limit=10"

# MA-based 7-day arbitrage (more stable)
curl "https://api.fundingrate.de/api/v4/arbitrage?period=7d&limit=20"

# Only pairs involving Hyperliquid, min 20% spread
curl "https://api.fundingrate.de/api/v4/arbitrage?exchange=hyperliquid&minSpread=0.2"

# All pairs between two specific exchanges
curl "https://api.fundingrate.de/api/v4/arbitrage?exchanges=hyperliquid,paradex&allPairs=true"
```

**Example Response:**

```json
{
  "success": true,
  "data": [
    {
      "ticker": "POPCAT",
      "spread_apr": 1.4520,
      "short_exchange": "drift",
      "short_apr": 1.8523,
      "short_volume": 12000000,
      "long_exchange": "hyperliquid",
      "long_apr": 0.4003,
      "long_volume": 45000000,
      "confidence_score": 3,
      "confidence": {
        "spread_consistency": 0.75,
        "volume_depth": 0.62,
        "rate_stability": 0.88,
        "historical_edge": 0.75
      },
      "market_price": 0.42,
      "open_interest": 5200000,
      "volume_24h": 45000000,
      "market_type": "crypto"
    }
  ],
  "count": 1,
  "period": "live",
  "total_pairs": 856
}
```

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `ticker` | string | Symbol (e.g. `BTC`) |
| `spread_apr` | number | APR spread between short and long (decimal) |
| `short_exchange` | string | Exchange with higher funding rate (collect) |
| `short_apr` | number | Funding rate on short side (decimal) |
| `short_volume` | number \| null | 24h volume on short exchange (USD) |
| `long_exchange` | string | Exchange with lower funding rate (pay) |
| `long_apr` | number | Funding rate on long side (decimal) |
| `long_volume` | number \| null | 24h volume on long exchange (USD) |
| `confidence_score` | number | Quality score 0–4 (see [Confidence Score](#12-confidence-score)) |
| `confidence` | object | Breakdown of the 4 sub-scores (each 0.0–1.0) |
| `market_price` | number \| null | Current price in USD |
| `open_interest` | number \| null | Open interest in USD |
| `volume_24h` | number \| null | Highest volume of the two sides (USD) |
| `market_type` | string | Market type classification |

**Usage Tips:**
- Use `period=7d` or `period=30d` for more stable, reliable arbitrage signals
- `confidence_score >= 2` filters out most noisy/unreliable pairs
- `minSpread=0.1` (10% APR) is a good starting threshold for meaningful opportunities

---

## 8. Single Coin Analysis

### `GET /api/v4/analysis/{symbol}`

Returns a comprehensive analysis for a single token: live rates per exchange (with nested MA data), all arbitrage pairs with confidence scores, and summary statistics.

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `symbol` | string | Yes | Ticker symbol (case-insensitive) |

**Query Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `minSpread` | number | No | `0` | Minimum spread APR for arbitrage pairs |

**Example Request:**

```bash
curl https://api.fundingrate.de/api/v4/analysis/BTC
```

**Example Response:**

```json
{
  "success": true,
  "symbol": "BTC",
  "market_type": "crypto",
  "exchanges": [
    {
      "exchange": "paradex",
      "funding_rate_apr": 0.1250,
      "market_price": 73250.00,
      "open_interest": 450000000,
      "volume_24h": 320000000,
      "spread_bid_ask": 0.003,
      "price_change_24h": 2.10,
      "collected_at": 1773224551,
      "ma": {
        "1h": { "ma_apr": 0.1180, "data_points": 12 },
        "4h": { "ma_apr": 0.1105, "data_points": 48 },
        "1d": { "ma_apr": 0.0980, "data_points": 288 },
        "7d": { "ma_apr": 0.0850, "data_points": 2016 },
        "30d": { "ma_apr": 0.0720, "data_points": 8640 }
      }
    },
    {
      "exchange": "hyperliquid",
      "funding_rate_apr": 0.0688,
      "market_price": 73261.00,
      "open_interest": 1933163139.92,
      "volume_24h": 2050817569.09,
      "spread_bid_ask": 0.0014,
      "price_change_24h": 2.10,
      "collected_at": 1773224551,
      "ma": {
        "1h": { "ma_apr": 0.0700, "data_points": 12 },
        "4h": { "ma_apr": 0.0680, "data_points": 48 },
        "1d": { "ma_apr": 0.0650, "data_points": 288 },
        "7d": { "ma_apr": 0.0523, "data_points": 2016 },
        "30d": { "ma_apr": 0.0490, "data_points": 8640 }
      }
    }
  ],
  "arbitrage_pairs": [
    {
      "short_exchange": "paradex",
      "short_apr": 0.1250,
      "long_exchange": "hyperliquid",
      "long_apr": 0.0688,
      "spread_apr": 0.0562,
      "short_volume": 320000000,
      "long_volume": 2050817569.09,
      "confidence_score": 3,
      "confidence": {
        "spread_consistency": 1.0,
        "volume_depth": 0.85,
        "rate_stability": 0.90,
        "historical_edge": 0.50
      }
    }
  ],
  "summary": {
    "exchange_count": 2,
    "avg_apr": 0.0969,
    "min_apr": 0.0688,
    "max_apr": 0.1250,
    "total_open_interest": 2383163139.92,
    "total_volume_24h": 2370817569.09,
    "best_arbitrage_spread": 0.0562,
    "arbitrage_pair_count": 1
  }
}
```

**Response Structure:**

| Field | Type | Description |
|-------|------|-------------|
| `symbol` | string | The requested ticker symbol |
| `market_type` | string | Market type classification |
| `exchanges` | array | Live data per exchange, each with nested `ma` object |
| `exchanges[].ma` | object | MA data keyed by period (`1h`, `4h`, ..., `30d`), each with `ma_apr` and `data_points` |
| `arbitrage_pairs` | array | All exchange pair combinations sorted by spread descending |
| `summary` | object | Aggregate statistics |

**Summary Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `exchange_count` | number | Number of exchanges listing this symbol |
| `avg_apr` | number | Average funding rate across all exchanges |
| `min_apr` | number | Lowest funding rate |
| `max_apr` | number | Highest funding rate |
| `total_open_interest` | number \| null | Sum of OI across all exchanges |
| `total_volume_24h` | number \| null | Sum of 24h volume across all exchanges |
| `best_arbitrage_spread` | number | Highest spread among all pairs |
| `arbitrage_pair_count` | number | Total number of arbitrage pairs found |

---

## 9. Exchanges

### `GET /api/v4/exchanges`

Returns the list of all supported exchanges with metadata and current statistics.

**Example Request:**

```bash
curl https://api.fundingrate.de/api/v4/exchanges
```

**Example Response:**

```json
{
  "success": true,
  "data": [
    {
      "key": "hyperliquid",
      "displayName": "Hyperliquid",
      "logoUrl": "https://defiapi.cloudflareone-demo-account.workers.dev/api/v4/exchanges/hyperliquid/logo",
      "website": "https://hyperliquid.xyz",
      "marketCount": 198,
      "symbolCount": 198,
      "lastCollected": 1773224551
    },
    {
      "key": "paradex",
      "displayName": "Paradex",
      "logoUrl": "https://defiapi.cloudflareone-demo-account.workers.dev/api/v4/exchanges/paradex/logo",
      "website": "https://paradex.trade",
      "marketCount": 85,
      "symbolCount": 85,
      "lastCollected": 1773224551
    }
  ],
  "count": 2
}
```

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `key` | string | Unique exchange identifier (used as filter in other endpoints) |
| `displayName` | string | Human-readable exchange name |
| `logoUrl` | string | URL to the exchange's logo image |
| `website` | string | Exchange website URL |
| `marketCount` | number | Number of markets currently tracked |
| `symbolCount` | number | Number of unique symbols tracked |
| `lastCollected` | number | Unix timestamp of last data collection |

**Supported Exchanges (29):**

| Key | Display Name |
|-----|-------------|
| `hyperliquid` | Hyperliquid |
| `paradex` | Paradex |
| `lighter` | Lighter |
| `edgex` | EdgeX |
| `ethereal` | Ethereal |
| `extended` | Extended |
| `asterdex` | AsterDEX |
| `variational` | Variational |
| `reya` | Reya |
| `pacifica` | Pacifica |
| `backpack` | Backpack |
| `vest` | Vest |
| `tradexyz` | TradeXYZ |
| `drift` | Drift |
| `evedex` | Evedex |
| `apex` | ApeX |
| `arkm` | Arkham |
| `dydx` | dYdX |
| `aevo` | Aevo |
| `01` | 01 Exchange |
| `nado` | Nado |
| `grvt` | GRVT |
| `astros` | Astros |
| `standx` | StandX |
| `hibachi` | Hibachi |
| `felix` | Felix |
| `hyena` | HyENA |
| `ventuals` | Ventuals |

---

### `GET /api/v4/exchanges/{key}/logo`

Returns the exchange logo as an image (PNG, SVG, or WebP).

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `key` | string | Yes | Exchange key (e.g. `hyperliquid`) |

**Response:** Binary image with appropriate `Content-Type` header. Cached for 24 hours.

**Example (in HTML):**

```html
<img src="https://api.fundingrate.de/api/v4/exchanges/hyperliquid/logo" alt="Hyperliquid" />
```

---

## 10. Spreads

### `GET /api/v4/spreads`

Query historical spread data from Analytics Engine.

**Query Parameters:**

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `pair` | string | No | all | Filter by pair (e.g. `PAXG_XAUT`) |
| `exchange` | string | No | all | Filter by exchange |
| `order` | string | No | `DESC` | Sort order: `ASC` or `DESC` |
| `limit` | number | No | 100 | Max rows |

**Example Request:**

```bash
curl "https://api.fundingrate.de/api/v4/spreads?pair=PAXG_XAUT&limit=10"
```

**Example Response:**

```json
{
  "success": true,
  "count": 1,
  "data": [
    {
      "pair": "PAXG_XAUT",
      "exchange": "variational",
      "token_a": "PAXG",
      "token_b": "XAUT",
      "fetched_at": "2026-04-10T16:00:00Z",
      "mark_a": 3215.50,
      "funding_rate_a": 0.0001,
      "bid_a": 3214.20,
      "ask_a": 3216.80,
      "mid_a": 3215.50,
      "mark_b": 3218.30,
      "funding_rate_b": 0.0002,
      "bid_b": 3217.10,
      "ask_b": 3219.50,
      "mid_b": 3218.30,
      "diff_usd": 2.80,
      "diff_pct": 0.087,
      "diff_bps": 8.7,
      "a_bid_vs_b_ask": -5.30,
      "b_bid_vs_a_ask": 0.30
    }
  ]
}
```

---

## 11. Data Types & Enums

### Market Types

| Value | Description | Example Tickers |
|-------|-------------|-----------------|
| `crypto` | Cryptocurrencies (default) | BTC, ETH, SOL, DOGE |
| `stock` | Equities / Stocks | TSLA, AAPL, NVDA, MSTR |
| `forex` | Foreign exchange pairs | EUR, GBP, JPY, AUD |
| `etf` | Exchange-traded funds | SPY, QQQ, GLD |
| `index` | Market indices | SPX, NDX |
| `commodity` | Commodities | XAU, XAG |

### MA Periods

| Value | Duration | Description |
|-------|----------|-------------|
| `1h` | 1 hour | Very short-term trend |
| `4h` | 4 hours | Short-term trend |
| `8h` | 8 hours | Intraday trend |
| `12h` | 12 hours | Half-day trend |
| `1d` | 1 day | Daily average |
| `3d` | 3 days | Short-term average |
| `7d` | 7 days | Weekly average |
| `30d` | 30 days | Monthly average |

### Arbitrage Periods

| Value | Source | Description |
|-------|--------|-------------|
| `live` | `unified_v4` | Current market snapshot (volatile) |
| `1h` – `30d` | `funding_ma_v4` | MA-based (smoother, more reliable) |

---

## 12. Confidence Score

Each arbitrage opportunity includes a **confidence score** (0–4) composed of four sub-scores (each 0.0–1.0):

### Sub-Scores

| Component | Description | How it's calculated |
|-----------|-------------|---------------------|
| `spread_consistency` | Is the spread direction stable across timeframes? | Checks if short > long at 1d, 7d, 30d MA periods. `1.0` = consistent across all available periods |
| `volume_depth` | Is there enough liquidity to execute? | Based on min(short_volume, long_volume). `<10k → 0`, `500k → 0.5`, `5M → 0.85`, `≥50M → 1.0` |
| `rate_stability` | Are live rates close to MA averages? | Compares live APR to 1d and 7d MA. Low deviation = high score |
| `historical_edge` | Does the spread hold over longer periods? | Checks if 7d and 30d MA spreads are positive and ≥50% of live spread |

### Score Interpretation

| Score | Meaning | Recommendation |
|-------|---------|----------------|
| **4** | Excellent — spread is consistent, liquid, stable, and historically proven | High confidence trade |
| **3** | Good — most quality signals positive | Worth monitoring |
| **2** | Fair — some positive signals, some missing data | Proceed with caution |
| **1** | Low — limited data or mixed signals | Research further |
| **0** | Minimal — insufficient data for evaluation | Not recommended |

### Filtering by Confidence

```bash
# Only show high-quality opportunities (score >= 3)
curl "https://api.fundingrate.de/api/v4/arbitrage?minScore=3"

# Show everything including low-quality (default filters score < 1)
curl "https://api.fundingrate.de/api/v4/arbitrage?includeAll=true"
```

---

## 13. Error Handling

### HTTP Status Codes

| Code | Meaning |
|------|---------|
| `200` | Success |
| `400` | Bad request — invalid parameters |
| `404` | Symbol or endpoint not found |
| `405` | Method not allowed (e.g. POST to a GET endpoint) |
| `500` | Internal server error |
| `502` | Analytics Engine query failed |
| `503` | Service unavailable (credentials not configured) |

### Error Response Format

```json
{
  "success": false,
  "error": "Symbol 'INVALID' not found"
}
```

### Common Errors

| Scenario | Status | Error message |
|----------|--------|---------------|
| Invalid period | 400 | `"Invalid period. Use 'live' or one of: 1h, 4h, 8h, 12h, 1d, 3d, 7d, 30d"` |
| Unknown symbol | 404 | `"Symbol 'XYZ' not found"` |
| Exchange logo missing | 404 | `"Exchange not found"` or `"Logo not found"` |
| AE query failure | 502 | `"Analytics Engine query failed"` |

---

## 14. Usage Examples

### TypeScript / JavaScript

```typescript
const BASE_URL = 'https://api.fundingrate.de';

// Fetch all BTC data across exchanges
async function getBTCData() {
  const res = await fetch(`${BASE_URL}/api/v4/markets/BTC`);
  const json = await res.json();

  if (!json.success) {
    throw new Error(json.error);
  }

  return json.data; // Array of market snapshots
}

// Fetch top arbitrage opportunities
async function getArbitrage(period = 'live', minSpread = 0.1) {
  const params = new URLSearchParams({ period, minSpread: String(minSpread) });
  const res = await fetch(`${BASE_URL}/api/v4/arbitrage?${params}`);
  const json = await res.json();
  return json.data;
}

// Fetch MA for a symbol
async function getMA(symbol: string, period?: string) {
  const params = period ? `?period=${period}` : '';
  const res = await fetch(`${BASE_URL}/api/v4/ma/latest/${symbol}${params}`);
  const json = await res.json();
  return json.data;
}

// Fetch full analysis
async function getAnalysis(symbol: string) {
  const res = await fetch(`${BASE_URL}/api/v4/analysis/${symbol}`);
  const json = await res.json();
  return json; // { symbol, exchanges, arbitrage_pairs, summary }
}

// Build a funding rate chart from historical data
async function getHistoricalChart(symbol: string, exchange: string, days = 7) {
  const to = Math.floor(Date.now() / 1000);
  const from = to - days * 86400;
  const params = new URLSearchParams({
    exchange,
    from: String(from),
    to: String(to),
    limit: '2000',
  });
  const res = await fetch(`${BASE_URL}/api/v4/history/${symbol}?${params}`);
  const json = await res.json();
  return json.data.map((d: any) => ({
    time: new Date(d.collected_at * 1000),
    apr: d.funding_rate_apr,
    price: d.market_price,
  }));
}
```

### Python

```python
import requests

BASE_URL = "https://api.fundingrate.de"

# Get all markets
response = requests.get(f"{BASE_URL}/api/v4/markets")
data = response.json()
print(f"Total markets: {data['count']}")

# Get BTC on all exchanges
response = requests.get(f"{BASE_URL}/api/v4/markets/BTC")
for market in response.json()["data"]:
    apr_pct = market["funding_rate_apr"] * 100
    print(f"  {market['exchange']}: {apr_pct:.2f}% APR")

# Get top arbitrage opportunities
response = requests.get(f"{BASE_URL}/api/v4/arbitrage", params={
    "period": "7d",
    "minSpread": "0.1",
    "minScore": "2",
    "limit": "20"
})
for arb in response.json()["data"]:
    spread_pct = arb["spread_apr"] * 100
    print(f"  {arb['ticker']}: {spread_pct:.1f}% spread "
          f"(short {arb['short_exchange']}, long {arb['long_exchange']}, "
          f"score: {arb['confidence_score']})")

# Full single-coin analysis
response = requests.get(f"{BASE_URL}/api/v4/analysis/ETH")
analysis = response.json()
summary = analysis["summary"]
print(f"ETH on {summary['exchange_count']} exchanges, "
      f"avg APR: {summary['avg_apr']*100:.2f}%, "
      f"best arb spread: {summary['best_arbitrage_spread']*100:.2f}%")
```

### cURL

```bash
# List all exchanges
curl -s https://api.fundingrate.de/api/v4/exchanges | jq '.data[] | {key, displayName, marketCount}'

# Get BTC across all exchanges
curl -s https://api.fundingrate.de/api/v4/markets/BTC | jq '.data[] | {exchange, funding_rate_apr, market_price}'

# Top 5 live arbitrage opportunities
curl -s "https://api.fundingrate.de/api/v4/arbitrage?limit=5" | jq '.data[] | {ticker, spread_apr, short_exchange, long_exchange, confidence_score}'

# BTC 7-day MA per exchange
curl -s "https://api.fundingrate.de/api/v4/ma/latest/BTC?period=7d" | jq '.data[] | {exchange, ma_apr, data_points}'

# Historical BTC data (last 24h)
FROM=$(($(date +%s) - 86400))
curl -s "https://api.fundingrate.de/api/v4/history/BTC?from=$FROM&limit=100" | jq '.count'
```

### React Component Example

```tsx
import { useEffect, useState } from 'react';

interface MarketData {
  normalized_symbol: string;
  exchange: string;
  funding_rate_apr: number;
  market_price: number | null;
  open_interest: number | null;
  volume_24h: number | null;
  market_type: string;
}

function FundingRateTable({ symbol }: { symbol: string }) {
  const [markets, setMarkets] = useState<MarketData[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetch(`https://api.fundingrate.de/api/v4/markets/${symbol}`)
      .then(res => res.json())
      .then(json => {
        if (json.success) setMarkets(json.data);
        setLoading(false);
      });
  }, [symbol]);

  if (loading) return <div>Loading...</div>;

  return (
    <table>
      <thead>
        <tr>
          <th>Exchange</th>
          <th>APR</th>
          <th>Price</th>
          <th>Volume 24h</th>
        </tr>
      </thead>
      <tbody>
        {markets.map(m => (
          <tr key={m.exchange}>
            <td>{m.exchange}</td>
            <td>{(m.funding_rate_apr * 100).toFixed(2)}%</td>
            <td>${m.market_price?.toLocaleString() ?? '—'}</td>
            <td>${m.volume_24h ? (m.volume_24h / 1e6).toFixed(1) + 'M' : '—'}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

---

## 15. TypeScript Interfaces

```typescript
// === Response wrapper ===

interface ApiResponse<T> {
  success: boolean;
  error?: string;
  data?: T;
  count?: number;
}

// === Market data ===

interface MarketSnapshot {
  normalized_symbol: string;
  exchange: string;
  collected_at: number;
  funding_rate_apr: number;
  market_price: number | null;
  open_interest: number | null;
  max_leverage: number | null;
  volume_24h: number | null;
  spread_bid_ask: number | null;
  price_change_24h: number | null;
  market_type: MarketType;
}

type MarketType = 'crypto' | 'stock' | 'forex' | 'etf' | 'index' | 'commodity';

// === History ===

interface HistoryDataPoint {
  ticker: string;
  exchange: string;
  market_type: string;
  collected_at: number;
  funding_rate_apr: number;
  market_price: number | null;
  open_interest: number | null;
  max_leverage: number | null;
  volume_24h: number | null;
  spread_bid_ask: number | null;
  price_change_24h: number | null;
}

interface HistoryResponse extends ApiResponse<HistoryDataPoint[]> {
  symbol: string;
  from: number;
  to: number;
}

// === Moving Averages ===

interface MAEntry {
  normalized_symbol: string;
  exchange: string;       // exchange name or '_all' for cross-exchange
  period: MAPeriod;
  ma_apr: number;
  data_points: number;
  period_start: number;
  calculated_at: number;
}

type MAPeriod = '1h' | '4h' | '8h' | '12h' | '1d' | '3d' | '7d' | '30d';

// === MA History ===

interface MAHistoryEntry {
  exchange: string;
  period: string;
  calculated_at: number;
  ma_apr: number;
  data_points: number;
  period_start: number;
}

// === Arbitrage ===

interface ArbitrageOpportunity {
  ticker: string;
  spread_apr: number;
  short_exchange: string;
  short_apr: number;
  short_volume: number | null;
  long_exchange: string;
  long_apr: number;
  long_volume: number | null;
  confidence_score: number;     // 0–4 integer
  confidence: ConfidenceDetails;
  market_price: number | null;
  open_interest: number | null;
  volume_24h: number | null;
  market_type: string;
}

interface ConfidenceDetails {
  spread_consistency: number;   // 0.0–1.0
  volume_depth: number;         // 0.0–1.0
  rate_stability: number;       // 0.0–1.0
  historical_edge: number;      // 0.0–1.0
}

interface ArbitrageResponse extends ApiResponse<ArbitrageOpportunity[]> {
  period: string;
  total_pairs: number;
}

type ArbitragePeriod = 'live' | MAPeriod;

// === Analysis ===

interface AnalysisExchange {
  exchange: string;
  funding_rate_apr: number;
  market_price: number | null;
  open_interest: number | null;
  volume_24h: number | null;
  spread_bid_ask: number | null;
  price_change_24h: number | null;
  collected_at: number;
  ma: Record<MAPeriod, { ma_apr: number; data_points: number }>;
}

interface AnalysisArbitragePair {
  short_exchange: string;
  short_apr: number;
  long_exchange: string;
  long_apr: number;
  spread_apr: number;
  short_volume: number | null;
  long_volume: number | null;
  confidence_score: number;
  confidence: ConfidenceDetails;
}

interface AnalysisSummary {
  exchange_count: number;
  avg_apr: number;
  min_apr: number;
  max_apr: number;
  total_open_interest: number | null;
  total_volume_24h: number | null;
  best_arbitrage_spread: number;
  arbitrage_pair_count: number;
}

interface AnalysisResponse {
  success: boolean;
  symbol: string;
  market_type: string;
  exchanges: AnalysisExchange[];
  arbitrage_pairs: AnalysisArbitragePair[];
  summary: AnalysisSummary;
}

// === Exchanges ===

interface ExchangeInfo {
  key: string;
  displayName: string;
  logoUrl: string;
  website: string;
  marketCount: number;
  symbolCount: number;
  lastCollected: number;
}

// === Spreads ===

interface SpreadDataPoint {
  pair: string;
  exchange: string;
  token_a: string;
  token_b: string;
  fetched_at: string;
  mark_a: number;
  funding_rate_a: number;
  bid_a: number;
  ask_a: number;
  mid_a: number;
  mark_b: number;
  funding_rate_b: number;
  bid_b: number;
  ask_b: number;
  mid_b: number;
  diff_usd: number;
  diff_pct: number;
  diff_bps: number;
  a_bid_vs_b_ask: number;
  b_bid_vs_a_ask: number;
}
```

---

## Changelog

| Date | Change |
|------|--------|
| April 2026 | Initial V4 Web Developer Guide |
| March 2026 | V4 API launched with 28 exchanges, 8 MA periods, confidence scoring |
