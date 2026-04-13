export interface OrderRecord {
  id: number
  exchange_order_id: string
  exchange: string
  instrument: string
  token: string
  side: 'BUY' | 'SELL'
  order_type: string
  status: string
  price: number
  average_price: number
  qty: number
  filled_qty: number
  fee: number
  reduce_only: number
  post_only: number
  created_at: number
  updated_at: number
  bot_id: string | null
}

export interface FillRecord {
  id: number
  exchange_fill_id: string
  exchange_order_id: string
  exchange: string
  instrument: string
  token: string
  side: 'BUY' | 'SELL'
  price: number
  qty: number
  value: number
  fee: number
  is_taker: number
  trade_type: string
  created_at: number
  bot_id: string | null
}

export interface FundingPayment {
  id: number
  exchange_payment_id: string
  exchange: string
  instrument: string
  token: string
  side: string
  size: number
  funding_fee: number
  funding_rate: number
  mark_price: number
  paid_at: number
  bot_id: string | null
}

export interface PointsRecord {
  id: number
  exchange: string
  season_id: number
  epoch_id: number
  start_date: string
  end_date: string
  points: number
  fetched_at: number
}

export interface JournalSummary {
  fills: Array<{
    exchange?: string
    token?: string
    bot_id?: string
    side: string
    fill_count: number
    total_qty: number
    total_value: number
    total_fee: number
    taker_fills: number
    maker_fills: number
  }>
  funding: Array<{
    exchange?: string
    token?: string
    total_funding: number
    payment_count: number
  }>
  orders: Array<{
    exchange?: string
    token?: string
    bot_id?: string
    order_count: number
    filled_count: number
    cancelled_count: number
  }>
  period: { from: number; to: number }
  group_by: string
}

export interface Position {
  id: string
  exchange: string
  instrument: string
  token: string
  side: 'LONG' | 'SHORT'
  status: 'CLOSED' | 'OPEN'
  entry_qty: number
  exit_qty: number
  remaining_qty: number
  entry_price: number
  exit_price: number
  realized_pnl: number
  total_fees: number
  total_funding: number
  net_pnl: number
  opened_at: number
  closed_at: number | null
  duration_ms: number
  fill_count: number
  bot_id: string | null
}

export interface PositionStats {
  total_positions: number
  open_positions: number
  closed_positions: number
  total_realized_pnl: number
  total_fees: number
  total_funding: number
  total_net_pnl: number
  win_rate: number
  wins: number
  losses: number
}

export interface PositionsResponse {
  positions: Position[]
  stats: PositionStats
}

export interface PairedTradeCombined {
  entry_spread: number
  exit_spread: number
  realized_pnl: number
  total_fees: number
  total_funding: number
  net_pnl: number
  size: number
  opened_at: number
  closed_at: number | null
  duration_ms: number
  fill_count: number
}

export interface PairedTrade {
  id: string
  token: string
  status: 'OPEN' | 'CLOSED'
  long: Position | null
  short: Position | null
  combined: PairedTradeCombined
}

export interface PairedTradeStats {
  total_trades: number
  open_trades: number
  closed_trades: number
  total_realized_pnl: number
  total_fees: number
  total_funding: number
  total_net_pnl: number
  win_rate: number
  wins: number
  losses: number
}

export interface PairedTradesResponse {
  trades: PairedTrade[]
  stats: PairedTradeStats
}

export interface JournalResponse<T> {
  data: T[]
  count: number
}
