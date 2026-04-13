export interface PortfolioPosition {
  instrument: string
  token: string
  side: 'LONG' | 'SHORT'
  size: number
  entry_price: number
  mark_price: number
  unrealized_pnl: number
  realized_pnl: number
  total_pnl: number
  cumulative_funding: number
  leverage: number
  funding_rate: number
}

export interface PortfolioExchange {
  exchange: string
  equity: number
  unrealized_pnl: number
  positions: PortfolioPosition[]
  error: string | null
}

export interface PortfolioSnapshot {
  exchanges: Record<string, PortfolioExchange>
  timestamp: number
}

export interface PairPosition extends PortfolioPosition {
  exchange: string
}

export interface CombinedPnl {
  unrealized: number
  realized: number
  total: number
  funding_net: number
}

export interface DeltaNeutralPair {
  token: string
  source: string
  long: PairPosition | null
  short: PairPosition | null
  combined_pnl: CombinedPnl
}

export interface PairsResponse {
  pairs: DeltaNeutralPair[]
  timestamp: number
}
