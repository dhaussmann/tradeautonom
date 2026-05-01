/**
 * Gold-Spread Bot — API client.
 *
 * Endpoints split between two origins (both reverse-proxied under /api):
 *   - Bot control (status/start/stop/reset/config/stream): forwarded by the
 *     main CF Worker to the user's container — see deploy/cloudflare/src/index.ts
 *     catch-all /api/* proxy.
 *   - History query (/api/gold-spread/history): served directly by the main
 *     CF Worker via deploy/cloudflare/src/gold_spread.ts.
 */

import type {
  GoldSpreadStatus,
  GoldSpreadConfigUpdate,
  GoldSpreadHistoryResponse,
  GoldSpreadRange,
  GoldSpreadResolution,
  OmsGoldSpreadLatest,
} from '@/types/gold-spread'

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
    throw new Error(body.detail || body.error || `HTTP ${res.status}`)
  }
  return res.json()
}

// ── Bot lifecycle (proxied to backend container) ──────────────────

export function fetchGoldSpreadStatus(): Promise<GoldSpreadStatus> {
  return request<GoldSpreadStatus>('/gold-spread/status')
}

export function startGoldSpread(): Promise<{ status: string }> {
  return request('/gold-spread/start', { method: 'POST' })
}

export function stopGoldSpread(): Promise<{ status: string }> {
  return request('/gold-spread/stop', { method: 'POST' })
}

export function resetGoldSpread(): Promise<{ reset: boolean; state: string }> {
  return request('/gold-spread/reset', { method: 'POST' })
}

export function updateGoldSpreadConfig(
  updates: GoldSpreadConfigUpdate,
): Promise<{ applied: Record<string, unknown>; rejected: Record<string, string>; config: unknown }> {
  return request('/gold-spread/config', {
    method: 'POST',
    body: JSON.stringify(updates),
  })
}

// ── Historical chart data (served from CF Analytics Engine) ───────

export function fetchGoldSpreadHistory(
  range: GoldSpreadRange = '24h',
  resolution?: GoldSpreadResolution,
): Promise<GoldSpreadHistoryResponse> {
  const params = new URLSearchParams({ range })
  if (resolution) params.set('resolution', resolution)
  return request<GoldSpreadHistoryResponse>(
    `/gold-spread/history?${params.toString()}`,
  )
}

// ── Live spread from OMS (works even when bot is stopped) ─────────

/**
 * Fetch the latest in-memory PAXG/XAUT spread snapshot from the OMS-v2
 * worker. Independent of the bot's lifecycle — the OMS computes a fresh
 * point on every Variational poll (~1.2 s), so this stays live even when
 * the user has not started the bot. The Main Worker proxies
 * `/api/oms/*` to `https://oms-v2.defitool.de/*` (see deploy/cloudflare/
 * src/index.ts::handleOmsProxy).
 */
export function fetchOmsSpreadLatest(): Promise<OmsGoldSpreadLatest> {
  return request<OmsGoldSpreadLatest>('/oms/gold-spread/latest')
}
