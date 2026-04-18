import type { BotSummary, BotStatus, BotCreateRequest, BotStartRequest, BotPosition, FundingInfo, ActivityEntry } from '@/types/bot'
import type { Position, AccountSummary } from '@/types/account'
import type { PairsResponse } from '@/types/portfolio'
import type { EquitySnapshot, Trade, HistoryResponse } from '@/types/history'
import type { OrderRecord, FillRecord, FundingPayment, PointsRecord, JournalSummary, JournalResponse, PositionsResponse, PairedTradesResponse } from '@/types/journal'

const API_BASE = '/api'

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${url}`, {
    credentials: 'include',
    cache: 'no-store',
    ...options,
    headers: { 'Content-Type': 'application/json', ...options?.headers },
  })
  if (!res.ok) {
    const body = await res.json().catch(() => ({}))
    throw new Error(body.detail || `HTTP ${res.status}`)
  }
  return res.json()
}

// ── Bots ──────────────────────────────────────────────
export async function fetchBots(): Promise<BotSummary[]> {
  const data = await request<{ bots: BotSummary[] }>('/fn/bots')
  return data.bots
}

export async function fetchBotStatus(botId: string): Promise<BotStatus> {
  return request<BotStatus>(`/fn/bots/${botId}/status`)
}

export async function createBot(req: BotCreateRequest): Promise<{ success: boolean; bot_id: string }> {
  return request('/fn/bots', { method: 'POST', body: JSON.stringify(req) })
}

export async function deleteBot(botId: string): Promise<{ success: boolean }> {
  return request(`/fn/bots/${botId}`, { method: 'DELETE' })
}

export async function startBot(botId: string, req?: BotStartRequest): Promise<{ success: boolean; message: string }> {
  return request(`/fn/bots/${botId}/start`, { method: 'POST', body: JSON.stringify(req || {}) })
}

export async function stopBot(botId: string): Promise<{ success: boolean; message: string }> {
  return request(`/fn/bots/${botId}/stop`, { method: 'POST' })
}

export async function killBot(botId: string): Promise<{ success: boolean; message: string }> {
  return request(`/fn/bots/${botId}/kill`, { method: 'POST' })
}

export async function pauseBot(botId: string): Promise<{ status: string; paused: boolean }> {
  return request(`/fn/bots/${botId}/pause`, { method: 'POST' })
}

export async function resumeBot(botId: string): Promise<{ status: string; paused: boolean }> {
  return request(`/fn/bots/${botId}/resume`, { method: 'POST' })
}

export async function resetBot(botId: string): Promise<{ status: string }> {
  return request(`/fn/bots/${botId}/reset`, { method: 'POST' })
}

export async function updateBotConfig(botId: string, updates: Record<string, unknown>): Promise<{ status: string }> {
  return request(`/fn/bots/${botId}/config`, { method: 'POST', body: JSON.stringify(updates) })
}

export async function adjustBotTimer(botId: string, durationH: number, durationM: number): Promise<{ status: string }> {
  return request(`/fn/bots/${botId}/timer`, { method: 'POST', body: JSON.stringify({ duration_h: durationH, duration_m: durationM }) })
}

export async function fetchBotPosition(botId: string): Promise<BotPosition> {
  return request<BotPosition>(`/fn/bots/${botId}/position`)
}

export async function fetchBotFunding(botId: string): Promise<FundingInfo> {
  return request<FundingInfo>(`/fn/bots/${botId}/funding`)
}

export async function fetchBotRisk(botId: string) {
  return request<{ status: Record<string, unknown>; alerts: unknown[] }>(`/fn/bots/${botId}/risk`)
}

export async function fetchBotTrades(botId: string, limit = 50) {
  return request<Record<string, unknown>>(`/fn/bots/${botId}/trades?limit=${limit}`)
}

export async function fetchBotLog(botId: string, sinceSeq = 0, limit = 100): Promise<{ entries: ActivityEntry[] }> {
  return request(`/fn/bots/${botId}/log?since_seq=${sinceSeq}&limit=${limit}`)
}

export async function fetchBotSuggestion(botId: string) {
  return request<Record<string, unknown>>(`/fn/bots/${botId}/suggestion`)
}

// ── Account ───────────────────────────────────────────
export async function fetchAccountAll(): Promise<AccountSummary[]> {
  return request<AccountSummary[]>('/account/all')
}

export async function fetchPositions(): Promise<Position[]> {
  return request<Position[]>('/account/positions')
}

// ── Exchanges ─────────────────────────────────────────
export async function fetchExchanges(): Promise<string[]> {
  const data = await request<{ exchanges: string[] }>('/exchanges')
  return data.exchanges
}

export interface MarketInfo {
  symbol: string
  name: string
  asset?: string
}

export async function fetchMarkets(exchange: string): Promise<MarketInfo[]> {
  const data = await request<{ markets: MarketInfo[] }>(`/exchanges/markets?exchange=${exchange}`)
  return data.markets
}

// ── Portfolio ─────────────────────────────────────────
export async function fetchPortfolioPairs(): Promise<PairsResponse> {
  return request<PairsResponse>('/portfolio/pairs')
}

// ── Health ────────────────────────────────────────────
export async function fetchHealth() {
  return request<{ status: string; grvt_env: string }>('/health')
}

// ── Vault ─────────────────────────────────────────────
export async function fetchVaultStatus(): Promise<{ setup_required: boolean; locked: boolean; unlocked: boolean }> {
  return request('/auth/status')
}

export async function unlockVault(password: string): Promise<{ status: string }> {
  return request('/auth/unlock', { method: 'POST', body: JSON.stringify({ password }) })
}

export async function setupVault(password: string): Promise<{ status: string }> {
  return request('/auth/setup', { method: 'POST', body: JSON.stringify({ password }) })
}

// ── Secrets (D1-backed key management) ───────────────
export async function fetchSecretsKeys(): Promise<{ keys: Record<string, string> }> {
  return request('/secrets/keys')
}

export async function updateSecretsKeys(keys: Record<string, string>): Promise<{ status: string; changed: string[]; container_updated: boolean }> {
  return request('/secrets/keys', { method: 'POST', body: JSON.stringify(keys) })
}

// ── NADO linked signer (wallet-connect auth flow) ────
export async function nadoPrepareLink(walletAddress: string, subaccountName?: string): Promise<{
  typed_data: Record<string, unknown>
  trading_address: string
  sender_hex: string
  signer_hex: string
}> {
  return request('/nado/prepare-link', {
    method: 'POST',
    body: JSON.stringify({ wallet_address: walletAddress, subaccount_name: subaccountName || 'default' }),
  })
}

export async function nadoSubmitLink(signature: string): Promise<{
  status: string
  trading_key: string
  trading_address: string
  wallet_address: string
  subaccount_name: string
}> {
  return request('/nado/submit-link', { method: 'POST', body: JSON.stringify({ signature }) })
}

export async function nadoLinkStatus(): Promise<{
  has_trading_key: boolean
  wallet_address: string
  subaccount_name: string
  remote_linked_signer: string | null
}> {
  return request('/nado/link-status')
}

// ── History ──────────────────────────────────────────
export async function fetchEquityHistory(params?: {
  exchange?: string; from?: number; to?: number; limit?: number
}): Promise<HistoryResponse<EquitySnapshot>> {
  const q = new URLSearchParams()
  if (params?.exchange) q.set('exchange', params.exchange)
  if (params?.from) q.set('from', String(params.from))
  if (params?.to) q.set('to', String(params.to))
  if (params?.limit) q.set('limit', String(params.limit))
  return request(`/history/equity?${q}`)
}

export async function fetchTradesHistory(params?: {
  token?: string; exchange?: string; from?: number; to?: number; limit?: number
}): Promise<HistoryResponse<Trade>> {
  const q = new URLSearchParams()
  if (params?.token) q.set('token', params.token)
  if (params?.exchange) q.set('exchange', params.exchange)
  if (params?.from) q.set('from', String(params.from))
  if (params?.to) q.set('to', String(params.to))
  if (params?.limit) q.set('limit', String(params.limit))
  return request(`/history/trades?${q}`)
}

// ── Journal ─────────────────────────────────────────────
export async function fetchJournalOrders(params?: {
  exchange?: string; token?: string; bot_id?: string; from?: number; to?: number; limit?: number
}): Promise<JournalResponse<OrderRecord>> {
  const q = new URLSearchParams()
  if (params?.exchange) q.set('exchange', params.exchange)
  if (params?.token) q.set('token', params.token)
  if (params?.bot_id) q.set('bot_id', params.bot_id)
  if (params?.from) q.set('from', String(params.from))
  if (params?.to) q.set('to', String(params.to))
  if (params?.limit) q.set('limit', String(params.limit))
  return request(`/journal/orders?${q}`)
}

export async function fetchJournalFills(params?: {
  exchange?: string; token?: string; bot_id?: string; from?: number; to?: number; limit?: number
}): Promise<JournalResponse<FillRecord>> {
  const q = new URLSearchParams()
  if (params?.exchange) q.set('exchange', params.exchange)
  if (params?.token) q.set('token', params.token)
  if (params?.bot_id) q.set('bot_id', params.bot_id)
  if (params?.from) q.set('from', String(params.from))
  if (params?.to) q.set('to', String(params.to))
  if (params?.limit) q.set('limit', String(params.limit))
  return request(`/journal/fills?${q}`)
}

export async function fetchJournalFunding(params?: {
  exchange?: string; token?: string; from?: number; to?: number; limit?: number
}): Promise<JournalResponse<FundingPayment>> {
  const q = new URLSearchParams()
  if (params?.exchange) q.set('exchange', params.exchange)
  if (params?.token) q.set('token', params.token)
  if (params?.from) q.set('from', String(params.from))
  if (params?.to) q.set('to', String(params.to))
  if (params?.limit) q.set('limit', String(params.limit))
  return request(`/journal/funding?${q}`)
}

export async function fetchJournalPoints(params?: {
  exchange?: string
}): Promise<JournalResponse<PointsRecord>> {
  const q = new URLSearchParams()
  if (params?.exchange) q.set('exchange', params.exchange)
  return request(`/journal/points?${q}`)
}

export async function fetchJournalPositions(params?: {
  exchange?: string; token?: string; from?: number; to?: number; status?: string
}): Promise<PositionsResponse> {
  const q = new URLSearchParams()
  if (params?.exchange) q.set('exchange', params.exchange)
  if (params?.token) q.set('token', params.token)
  if (params?.from) q.set('from', String(params.from))
  if (params?.to) q.set('to', String(params.to))
  if (params?.status) q.set('status', params.status)
  return request(`/journal/positions?${q}`)
}

export async function fetchJournalPairedTrades(params?: {
  token?: string; from?: number; to?: number; status?: string
}): Promise<PairedTradesResponse> {
  const q = new URLSearchParams()
  if (params?.token) q.set('token', params.token)
  if (params?.from) q.set('from', String(params.from))
  if (params?.to) q.set('to', String(params.to))
  if (params?.status) q.set('status', params.status)
  return request(`/journal/paired-trades?${q}`)
}

export async function fetchJournalSummary(params?: {
  from?: number; to?: number; group_by?: string
}): Promise<JournalSummary> {
  const q = new URLSearchParams()
  if (params?.from) q.set('from', String(params.from))
  if (params?.to) q.set('to', String(params.to))
  if (params?.group_by) q.set('group_by', params.group_by)
  return request(`/journal/summary?${q}`)
}

// ── OMS Arbitrage ────────────────────────────────────────

export interface ArbOpportunity {
  token: string
  buy_exchange: string
  buy_symbol: string
  sell_exchange: string
  sell_symbol: string
  buy_price_bbo: number
  sell_price_bbo: number
  bbo_spread_bps: number
  buy_fill_vwap: number
  sell_fill_vwap: number
  net_profit_bps: number
  max_qty: number
  max_notional_usd: number
  timestamp_ms: number
}

export interface ArbConfig {
  scan_interval_s: number
  max_notional_usd: number
  exchanges: string[]
  taker_fees_pct: Record<string, number>
  fee_buffer_bps: number
  min_profit_bps: Record<string, number>
  tokens_tracked: number
  active_opportunities: number
}

// ── DNA Bot ──────────────────────────────────────────────

export interface DNAPosition {
  position_id: string
  token: string
  buy_exchange: string
  buy_symbol: string
  sell_exchange: string
  sell_symbol: string
  quantity: number
  buy_fill_price: number
  sell_fill_price: number
  entry_spread_bps: number
  notional_usd: number
  opened_at: number
  status: string
  exit_mode: string
  exit_min_hold_s: number
  exit_threshold_bps: number
  closed_at: number | null
  close_spread_bps: number | null
  close_reason: string
  close_buy_fill_price: number
  close_sell_fill_price: number
  buy_fill_qty: number
  sell_fill_qty: number
  simulation: boolean
}

export interface DNAConfig {
  position_size_usd: number
  max_positions: number
  spread_mode: string
  custom_min_spread_bps: number
  exchanges: string[]
  simulation: boolean
  exit_mode: string
  exit_min_hold_minutes: number
  exit_min_hold_hours: number
  exit_min_hold_days: number
  exit_threshold_bps: number
}

export interface DNAActivityEntry {
  timestamp: number
  event: string
  message: string
}

export interface DNAStatus {
  bot_id: string
  running: boolean
  config: DNAConfig
  positions: {
    open: number
    max: number
    total_notional_usd: number
    details: DNAPosition[]
  }
  all_positions: DNAPosition[]
  trade_history: DNAPosition[]
  activity_log: DNAActivityEntry[]
}

export interface PreflightExchangeCheck {
  positions: boolean
  balance: boolean | null
  signer_ok?: boolean | null
  error: string | null
}

export interface PreflightOmsCheck {
  health: boolean
  feeds?: number
  books: Record<string, boolean>
  error: string | null
}

export interface PreflightResult {
  ok: boolean
  can_start: boolean
  checks: Record<string, PreflightExchangeCheck | PreflightOmsCheck>
}

export async function fetchDnaStatus(): Promise<DNAStatus> {
  return request<DNAStatus>('/dna/status')
}

export async function fetchDnaPreflight(): Promise<PreflightResult> {
  return request<PreflightResult>('/dna/preflight')
}

export async function startDna(): Promise<{ status: string }> {
  return request('/dna/start', { method: 'POST' })
}

export async function stopDna(): Promise<{ status: string }> {
  return request('/dna/stop', { method: 'POST' })
}

export async function updateDnaConfig(updates: Partial<DNAConfig>): Promise<{ status: string }> {
  return request('/dna/config', { method: 'POST', body: JSON.stringify(updates) })
}

export async function resetDna(): Promise<{ status: string }> {
  return request('/dna/reset', { method: 'POST' })
}

export async function closeDnaPosition(positionId: string): Promise<{ status: string; position_id: string }> {
  return request(`/dna/close/${positionId}`, { method: 'POST' })
}

export async function deleteDnaPosition(positionId: string): Promise<{ status: string; position_id: string }> {
  return request(`/dna/position/${positionId}/delete`, { method: 'POST' })
}

export async function fetchDnaPositions(): Promise<{ positions: DNAPosition[]; open: number; max: number }> {
  return request('/dna/positions')
}

export async function fetchArbOpportunities(token?: string): Promise<ArbOpportunity[]> {
  const q = token ? `?token=${encodeURIComponent(token)}` : ''
  return request<ArbOpportunity[]>(`/oms/arb/opportunities${q}`)
}

export async function fetchArbConfig(): Promise<ArbConfig> {
  return request<ArbConfig>('/oms/arb/config')
}
