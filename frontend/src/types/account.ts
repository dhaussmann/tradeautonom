export interface Position {
  instrument: string
  /**
   * Underlying token name (e.g. "XRP", "DOGE"). Currently only populated by
   * the Variational client; lets the UI match positions by token when the
   * full instrument string has drifted (Variational rotates funding intervals
   * and stale position objects still carry the old `funding_interval_s`).
   */
  underlying?: string
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
  /** Variational-specific: USD notional of the position (negative for shorts). */
  value?: number
  /** Variational-specific: cumulative funding payment received/paid. */
  cumulative_funding?: number
}

export interface AccountSummary {
  exchange: string
  equity: number
  unrealized_pnl: number
  positions: Position[]
}
