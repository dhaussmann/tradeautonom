export type BotState = 'IDLE' | 'ENTERING' | 'HOLDING' | 'EXITING' | 'PAUSED_ENTERING' | 'PAUSED_EXITING'

export interface BotSummary {
  bot_id: string
  state: BotState
  is_running: boolean
  long_exchange: string
  short_exchange: string
  instrument_a: string
  instrument_b: string
  quantity: number
}

export interface BotConfig {
  long_exchange: string
  short_exchange: string
  maker_exchange: string
  instrument_a: string
  instrument_b: string
  quantity: number
  twap_num_chunks: number
  twap_interval_s: number
  simulation: boolean
  max_chunk_spread_usd: number
  min_spread_pct: number
  max_spread_pct: number
}

export interface BotPosition {
  long_exchange: string
  short_exchange: string
  long_symbol: string
  short_symbol: string
  long_qty: number
  short_qty: number
  net_delta: number
  long_entry_price: number
  short_entry_price: number
}

export interface BotExecution {
  state: string
  chunk_index: number
  chunk_state: string | null
  chunks_completed: number
  total_chunks: number
  last_result: {
    success: boolean | null
    error: string | null
    total_maker_qty: number
    total_taker_qty: number
  }
}

export interface BotTimer {
  started_at: number | null
  expires_at: number | null
  remaining_s: number | null
  duration_h: number
  duration_m: number
  stop_reason: string | null
}

export interface PriceInfo {
  symbol: string
  best_bid: number
  best_ask: number
  mid: number
  synced: boolean
}

export interface FundingInfo {
  extended?: { symbol: string; rate: number }
  grvt?: { symbol: string; rate: number }
  variational?: { symbol: string; rate: number }
  nado?: { symbol: string; rate: number }
  spread: number
  spread_annualised: number
  recommended_long: string
  recommended_short: string
  reason: string
  age_ms: number
}

export interface RiskInfo {
  halted: boolean
  cumulative_pnl: number
  circuit_breaker_threshold: number
  delta_max_usd: number
  min_spread_pct: number
  max_spread_pct: number
  recent_alerts: number
}

export interface ActivityEntry {
  seq: number
  ts: number
  cat: string
  msg: string
  extra?: { level?: string }
}

export interface BotStatus {
  state: BotState
  is_running: boolean
  is_paused: boolean
  timer: BotTimer
  leverage: { long: number; short: number }
  prices: Record<string, PriceInfo>
  pnl: { long_pnl: number; short_pnl: number; total_pnl: number }
  position: BotPosition
  execution: BotExecution
  funding: FundingInfo
  risk: RiskInfo
  feeds_ready: boolean
  data: Record<string, unknown>
  config: BotConfig
  trade_count: number
  activity_log: ActivityEntry[]
}

export interface BotCreateRequest {
  bot_id: string
  long_exchange: string
  short_exchange: string
  instrument_a: string
  instrument_b: string
  quantity: number
  twap_num_chunks?: number
  twap_interval_s?: number
  maker_exchange?: string
  simulation?: boolean
  leverage_long?: number
  leverage_short?: number
  min_spread_pct?: number
  max_spread_pct?: number
}

export interface BotStartRequest {
  duration_h?: number
  duration_m?: number
  leverage_long?: number
  leverage_short?: number
  quantity?: number
  long_exchange?: string
  short_exchange?: string
  instrument_a?: string
  instrument_b?: string
}
