export interface EquitySnapshot {
  ts: number
  exchange: string
  equity: number
  unrealized_pnl: number
}

export interface PositionSnapshot {
  ts: number
  exchange: string
  token: string
  instrument: string
  side: 'LONG' | 'SHORT'
  size: number
  entry_price: number
  mark_price: number
  unrealized_pnl: number
  realized_pnl: number
  cumulative_funding: number
  funding_rate: number
  leverage: number
}

export interface Trade {
  id: number
  exchange: string
  token: string
  instrument: string
  side: 'LONG' | 'SHORT'
  size: number
  entry_price: number
  exit_price: number
  opened_at: number
  closed_at: number
  realized_pnl: number
  cumulative_funding: number
  total_pnl: number
  pair_token: string | null
}

export interface HistoryResponse<T> {
  data: T[]
  count: number
}
