export interface ApiResponse<T = unknown> {
  data: T | null
  error: string | null
  ok: boolean
}

export interface HealthResponse {
  status: string
  grvt_env: string
}

export interface BotsListResponse {
  bots: import('./bot').BotSummary[]
}

export interface ExchangesResponse {
  exchanges: string[]
}

export interface MarketsResponse {
  instruments: string[]
}
