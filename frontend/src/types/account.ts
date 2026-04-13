export interface Position {
  instrument: string
  size: number
  side: string
  entry_price: number
  mark_price: number
  unrealized_pnl: number
  leverage: number
  exchange: string
  realized_pnl?: number
  total_pnl?: number
  roi?: number
  est_liquidation_price?: number
  margin_type?: string
}

export interface AccountSummary {
  exchange: string
  equity: number
  unrealized_pnl: number
  positions: Position[]
}
