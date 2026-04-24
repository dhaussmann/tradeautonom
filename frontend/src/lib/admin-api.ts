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
    throw new Error((body as any).detail || (body as any).error || `HTTP ${res.status}`)
  }
  return res.json()
}

export interface AdminUser {
  id: string
  name: string
  email: string
  createdAt: string
  /** 'photon' (V1) | 'cf' (V2 Cloudflare Container). Null for users created before migration 0006. */
  backend: 'photon' | 'cf' | null
  container_name: string | null
  port: number | null
  container_status: string | null
}

export async function fetchAdminCheck(): Promise<{ is_admin: boolean }> {
  return request('/admin/check')
}

export async function fetchAdminUsers(): Promise<AdminUser[]> {
  const data = await request<{ users: AdminUser[] }>('/admin/users')
  return data.users
}

export async function deleteAdminUser(userId: string): Promise<{ status: string }> {
  return request(`/admin/users/${userId}`, { method: 'DELETE' })
}

/**
 * Flip a user between the V1 (Photon) and V2 (Cloudflare Container) backend.
 * If the user's bots on the current backend aren't IDLE, the server will
 * return 409 unless `force=true`.
 */
export async function setUserBackend(
  userId: string,
  backend: 'photon' | 'cf',
  force = false,
): Promise<{ status: string; backend: string; forced: boolean }> {
  return request(`/admin/user/${userId}/backend`, {
    method: 'POST',
    body: JSON.stringify({ backend, force }),
  })
}

// ── Activity Log ─────────────────────────────────────────────

export interface ActivityLogEntry {
  container: string
  port: string
  bot_type: string
  bot_id: string
  event: string
  message: string
  user_id: string
  timestamp: number
  datetime: string
}

export interface ActivityLogFilters {
  container?: string
  bot_type?: string
  event?: string
  search?: string
  from?: string
  to?: string
  limit?: number
}

export async function fetchActivityLogs(filters: ActivityLogFilters = {}): Promise<{ rows: ActivityLogEntry[]; count: number }> {
  const params = new URLSearchParams()
  if (filters.container) params.set('container', filters.container)
  if (filters.bot_type) params.set('bot_type', filters.bot_type)
  if (filters.event) params.set('event', filters.event)
  if (filters.search) params.set('search', filters.search)
  if (filters.from) params.set('from', filters.from)
  if (filters.to) params.set('to', filters.to)
  if (filters.limit) params.set('limit', String(filters.limit))
  const qs = params.toString()
  return request(`/admin/activity${qs ? '?' + qs : ''}`)
}
