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
  exit_min_spread_pct: number
  exit_max_spread_pct: number
  fn_opt_depth_spread?: boolean
  fn_opt_max_slippage_bps?: number
  fn_opt_ohi_monitoring?: boolean
  fn_opt_min_ohi?: number
  fn_opt_funding_history?: boolean
  fn_opt_min_funding_consistency?: number
  fn_opt_dynamic_sizing?: boolean
  fn_opt_max_utilization?: number
  fn_opt_max_per_pair_ratio?: number
  fn_opt_shared_monitor_url?: string
  fn_opt_taker_drift_guard?: boolean
  fn_opt_max_taker_drift_bps?: number
  [key: string]: unknown
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

/**
 * One TWAP-chunk fill, flattened from engine._trade_log.
 *
 * Pre-mapped long/short legs come straight from the backend (computed
 * in engine.py:_log_trade at execution time using the maker_exchange ↔
 * config.long_exchange identity match — robust across ENTRY/EXIT
 * regardless of which side is buying or selling).
 *
 * `spread_usd` is the per-unit price difference (long_price −
 * short_price). Negative = long leg cheaper than short at fill =
 * favourable carry on entry.
 *
 * `ts` is end_ts of the chunk (or trade timestamp as fallback).
 * `error` is non-null only for failed/aborted chunks the user should
 * notice; successful chunks have `error: null`.
 */
export interface FillEntry {
  action: 'ENTRY' | 'EXIT' | string
  chunk_index: number
  long_exchange: string
  long_qty: number
  long_price: number
  short_exchange: string
  short_qty: number
  short_price: number
  spread_usd: number | null
  ts: number
  error: string | null
}

export interface OhiEntry {
  exchange?: string
  ohi: number
  volume_24h?: number
  spread_bps?: number
  depth_usd?: number
  symmetry?: number
  spread_score?: number
  depth_score?: number
  symmetry_score?: number
}

export interface OhiInfo {
  long?: OhiEntry | null
  short?: OhiEntry | null
}

export interface DepthAnalysis {
  bbo_spread_pct: number
  exec_spread_pct: number
  slippage_bps: number
  is_acceptable: boolean
  long_fill_price: number
  short_fill_price: number
  long_bbo: number
  short_bbo: number
}

export interface FundingV4Info {
  long?: { exchange: string; rate: number; annualised: number } | null
  short?: { exchange: string; rate: number; annualised: number } | null
  net_annualised?: number
  consistency?: number
  confidence_score?: number | null
  spread_consistency?: number | null
  pair_found?: boolean
  spread_apr?: number
  volume_depth?: number
  rate_stability?: number
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
  funding_v4?: FundingV4Info | null
  ohi?: OhiInfo | null
  depth_analysis?: DepthAnalysis | null
  risk: RiskInfo
  feeds_ready: boolean
  data: Record<string, unknown> & { oms_active?: boolean; oms_url?: string | null }
  config: BotConfig
  trade_count: number
  // Latest 50 fills (newest first) — live tail from SSE. Initial mount
  // also calls fetchBotFills() to load the full history; subsequent SSE
  // frames keep the tail in sync. May be undefined on V2 containers
  // that pre-date the field; callers must handle that gracefully.
  fills?: FillEntry[]
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
  exit_min_spread_pct?: number
  exit_max_spread_pct?: number
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

/**
 * OMS V2 /meta/{exchange}/{symbol} response.
 * Matches deploy/cf-containers/oms-v2/src/types.ts::SymbolMeta
 */
export interface SymbolMeta {
  exchange: string
  symbol: string
  base_token: string
  tick_size: number
  /** Base-qty floor. For Nado this equals size_increment. */
  min_order_size: number
  /** USD-notional floor, or null if the exchange does not publish one. */
  min_notional_usd: number | null
  qty_step: number
  max_leverage: number
  taker_fee_pct: number
  maker_fee_pct: number | null
  funding_interval_s: number | null
}
