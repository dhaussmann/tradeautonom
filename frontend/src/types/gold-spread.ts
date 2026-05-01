/**
 * Gold-Spread Bot — frontend type contracts.
 *
 * Mirrors the dataclasses in app/gold_spread_bot.py and the data point
 * schema written by deploy/cf-containers/oms-v2/src/lib/gold-spread.ts.
 */

export type GoldSpreadState =
  | 'IDLE'
  | 'MONITORING'
  | 'ENTERING'
  | 'HOLDING'
  | 'EXITING'
  | 'ERROR'

export type GoldSpreadSignal = 'NONE' | 'HOLD' | 'ENTRY' | 'EXIT'

/** Which token currently trades at a premium (mid-based). */
export type GoldSpreadDirection = 'paxg_premium' | 'xaut_premium'

/** One spread evaluation tick (server-side `GoldSpreadSnapshot`). */
export interface GoldSpreadSnapshot {
  ts: number // epoch seconds (server) — multiply by 1000 for chart ts
  paxg_mid: number
  paxg_bid: number
  paxg_ask: number
  xaut_mid: number
  xaut_bid: number
  xaut_ask: number
  /** abs(paxg_mid - xaut_mid) — always positive. */
  spread: number
  spread_pct: number
  /** Direction-aware entry exec spread (short premium @ bid, long discount @ ask). */
  exec_spread: number
  /** Direction-aware exit exec spread (reverse of entry). */
  exit_exec_spread: number
  /** Which token currently trades at a premium. */
  direction: GoldSpreadDirection
  signal: GoldSpreadSignal
}

/** Open paper or live position (server-side `GoldSpreadPosition`).
 *
 * The leg layout is direction-aware: ``short_token`` is whichever token
 * was the premium one at entry (the one we sold), ``long_token`` the
 * discount side (the one we bought). */
export interface GoldSpreadPosition {
  opened_at: number
  direction: GoldSpreadDirection
  short_token: string  // "PAXG" | "XAUT"
  short_symbol: string
  short_qty: number
  short_entry_price: number
  long_token: string   // "PAXG" | "XAUT"
  long_symbol: string
  long_qty: number
  long_entry_price: number
  entry_spread: number
  simulation: boolean
  short_order_id?: string | null
  long_order_id?: string | null
}

/** All hot-updateable settings. Mirrors app/gold_spread_bot.py::GoldSpreadConfig. */
export interface GoldSpreadConfig {
  paxg_symbol: string
  xaut_symbol: string
  exchange: string
  entry_spread: number
  exit_spread: number
  threshold_in_pct: boolean
  quantity: number
  max_slippage_pct: number
  signal_confirmations: number
  tick_interval_s: number
  simulation: boolean
  // Phase 2: execution-safety knobs
  max_position_value_usd: number
  execution_timeout_s: number
  unwind_slippage_pct: number
  fill_verify_delay_s: number
  min_actual_spread_ratio: number
  max_spread_volatility_ratio: number
  oms_url: string
  bot_id: string
}

/** Subset of config keys the UI is allowed to mutate. */
export type GoldSpreadConfigUpdate = Partial<
  Pick<
    GoldSpreadConfig,
    | 'entry_spread'
    | 'exit_spread'
    | 'threshold_in_pct'
    | 'quantity'
    | 'max_slippage_pct'
    | 'signal_confirmations'
    | 'tick_interval_s'
    | 'simulation'
    | 'max_position_value_usd'
    | 'execution_timeout_s'
    | 'unwind_slippage_pct'
    | 'fill_verify_delay_s'
    | 'min_actual_spread_ratio'
    | 'max_spread_volatility_ratio'
  >
>

export interface ActivityEntry {
  timestamp: number
  event: string
  message: string
}

/** Response of GET /gold-spread/status. */
export interface GoldSpreadStatus {
  state: GoldSpreadState
  running: boolean
  error: string | null
  config: GoldSpreadConfig
  spread: GoldSpreadSnapshot | null
  position: GoldSpreadPosition | null
  signal_count: number
  last_signal: GoldSpreadSignal
  /** Recent in-memory snapshots (≤ ~720 ticks). Use for live overlay. */
  live_history: GoldSpreadSnapshot[]
  activity: ActivityEntry[]
}

/** One historical data point — matches the Worker's gold_spread.ts shape. */
export interface GoldSpreadHistoryPoint {
  ts: number // epoch ms
  paxg_mid: number
  xaut_mid: number
  /** abs(paxg_mid - xaut_mid). May be signed for very old data. */
  spread: number
  spread_pct: number
  paxg_bid?: number
  paxg_ask?: number
  xaut_bid?: number
  xaut_ask?: number
  /** Direction-aware entry exec spread. May be missing for old data. */
  exec_spread?: number
  /** Direction-aware exit exec spread. May be missing for old data. */
  exit_exec_spread?: number
  /** Which token was the premium one at this tick. Raw rows only. */
  direction?: GoldSpreadDirection
}

export type GoldSpreadRange = '1h' | '24h' | '7d' | '30d' | 'all'
export type GoldSpreadResolution = 'raw' | '1m' | '5m' | '1h'

/**
 * Live spread snapshot from the OMS-v2 worker
 * (`GET /api/oms/gold-spread/latest`). Used by the UI as a fallback when
 * the backend bot is stopped — the OMS keeps emitting fresh data points
 * roughly every 1.2 s regardless of bot state, so the chart and KPIs
 * stay live even when no monitor loop is running.
 */
export interface OmsGoldSpreadLatest {
  ts_ms: number
  paxg_mid: number
  xaut_mid: number
  spread: number
  spread_pct: number
  paxg_bid: number
  paxg_ask: number
  xaut_bid: number
  xaut_ask: number
  exec_spread: number
  exit_exec_spread: number
  direction: GoldSpreadDirection
}

/** Response of GET /api/gold-spread/history. */
export interface GoldSpreadHistoryResponse {
  points: GoldSpreadHistoryPoint[]
  count: number
  range: GoldSpreadRange
  resolution: GoldSpreadResolution
}
