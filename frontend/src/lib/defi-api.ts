const DEFI_BASE = 'https://api.fundingrate.de'

async function defiRequest<T>(path: string): Promise<T> {
  const res = await fetch(`${DEFI_BASE}${path}`)
  if (!res.ok) {
    throw new Error(`DeFi API error: ${res.status}`)
  }
  const json = await res.json() as { success: boolean; data: T }
  if (!json.success) throw new Error('DeFi API returned error')
  return json.data
}

// ── Types ────────────────────────────────────────────

export interface MarketEntry {
  normalized_symbol: string
  exchange: string
  collected_at: number
  funding_rate_apr: number
  market_price: number | null
  open_interest: number | null
  max_leverage: number | null
  volume_24h: number | null
  spread_bid_ask: number | null
  price_change_24h: number | null
  market_type: string
}

export interface ExchangeInfo {
  key: string
  displayName: string
  logoUrl: string
  website: string
  marketCount: number
  symbolCount: number
  lastCollected: number
}

export interface AnalysisExchange {
  exchange: string
  funding_rate_apr: number
  market_price: number | null
  open_interest: number | null
  volume_24h: number | null
  spread_bid_ask: number | null
  price_change_24h: number | null
  collected_at: number
  ma?: Record<string, { ma_apr: number; data_points: number }>
}

export interface AnalysisResult {
  symbol: string
  market_type: string
  exchanges: AnalysisExchange[]
  arbitrage?: unknown[]
  summary?: Record<string, unknown>
}

export interface ArbitrageEntry {
  ticker: string
  spread_apr: number
  short_exchange: string
  short_apr: number
  short_volume: number | null
  long_exchange: string
  long_apr: number
  long_volume: number | null
  confidence_score: number
  confidence: {
    spread_consistency: number
    volume_depth: number
    rate_stability: number
    historical_edge: number
  }
  market_price: number | null
  open_interest: number | null
  volume_24h: number | null
  market_type: string
}

// ── API functions ────────────────────────────────────

/** All latest snapshots — one row per symbol, best APR */
export async function fetchMarketsLatest(): Promise<MarketEntry[]> {
  return defiRequest('/api/v4/markets/latest')
}

/** All snapshots for a specific symbol across all exchanges */
export async function fetchMarketsBySymbol(symbol: string): Promise<MarketEntry[]> {
  return defiRequest(`/api/v4/markets?symbol=${encodeURIComponent(symbol)}`)
}

/** Comprehensive analysis for a single token (includes MA data) */
export async function fetchAnalysis(symbol: string): Promise<AnalysisResult> {
  // Analysis endpoint wraps differently: { success, symbol, exchanges, ... }
  const res = await fetch(`${DEFI_BASE}/api/v4/analysis/${encodeURIComponent(symbol)}`)
  if (!res.ok) throw new Error(`DeFi API error: ${res.status}`)
  const json = await res.json() as AnalysisResult & { success: boolean }
  if (!json.success) throw new Error('DeFi API returned error')
  return json
}

/** List all exchanges */
export async function fetchExchangeList(): Promise<ExchangeInfo[]> {
  return defiRequest('/api/v4/exchanges')
}

/** All snapshots for a specific exchange */
export async function fetchMarketsByExchange(exchange: string): Promise<MarketEntry[]> {
  return defiRequest(`/api/v4/markets?exchange=${encodeURIComponent(exchange)}`)
}

/** Tokens available on ALL of the given exchanges (normalized symbols) */
export async function fetchCommonTokens(
  exchanges: string[] = ['extended', 'variational', 'grvt'],
): Promise<string[]> {
  const results = await Promise.all(exchanges.map(fetchMarketsByExchange))
  const sets = results.map(entries => new Set(entries.map(e => e.normalized_symbol)))
  const common = [...sets[0]].filter(sym => sets.every(s => s.has(sym)))
  return common.sort()
}

export interface TokenTableRow {
  symbol: string
  volume24h: number
  /**
   * Funding-arb spread: |max(APR) − min(APR)| across the selected exchanges.
   *
   * This is what the bot actually earns — collecting positive funding on the
   * high-APR exchange while paying the low-APR exchange on the opposite leg.
   * Always >= 0; sign is irrelevant in Step 1 (long/short assignment is
   * decided in Step 2 from the per-exchange APRs).
   */
  aprSpread: number
  perExchange: Record<string, { apr: number; price: number | null; volume: number | null }>
}

/** Common tokens with per-exchange data for the table view */
export async function fetchCommonTokensWithData(
  exchanges: string[] = ['extended', 'variational', 'grvt'],
): Promise<TokenTableRow[]> {
  const results = await Promise.all(exchanges.map(fetchMarketsByExchange))
  // Build per-exchange maps
  const maps = exchanges.map((_ex, i) => {
    const m = new Map<string, MarketEntry>()
    for (const e of results[i]) m.set(e.normalized_symbol, e)
    return m
  })
  // Intersection
  const common = [...maps[0].keys()].filter(sym => maps.every(m => m.has(sym)))
  return common.map(sym => {
    const perExchange: TokenTableRow['perExchange'] = {}
    let totalVol = 0
    let maxApr = -Infinity
    let minApr = Infinity
    for (let i = 0; i < exchanges.length; i++) {
      const entry = maps[i].get(sym)!
      perExchange[exchanges[i]] = {
        apr: entry.funding_rate_apr,
        price: entry.market_price,
        volume: entry.volume_24h,
      }
      totalVol += entry.volume_24h ?? 0
      if (entry.funding_rate_apr > maxApr) maxApr = entry.funding_rate_apr
      if (entry.funding_rate_apr < minApr) minApr = entry.funding_rate_apr
    }
    // Funding-arb spread is the gap between the highest- and lowest-APR
    // exchange — that's what the bot captures by going long the cheap leg
    // and short the expensive leg. Always non-negative.
    const aprSpread =
      Number.isFinite(maxApr) && Number.isFinite(minApr) ? maxApr - minApr : 0
    return { symbol: sym, volume24h: totalVol, aprSpread, perExchange }
  }).sort((a, b) => a.symbol.localeCompare(b.symbol))
}

/** Fetch arbitrage opportunities filtered to specific exchanges */
export async function fetchArbitrage(
  period: string = '7d',
  exchanges: string[] = ['extended', 'variational', 'grvt'],
): Promise<ArbitrageEntry[]> {
  const params = new URLSearchParams({
    exchanges: exchanges.join(','),
    period,
    allPairs: 'true',
    minScore: '0',
    includeAll: 'true',
    limit: '500',
  })
  return defiRequest(`/api/v4/arbitrage?${params}`)
}
