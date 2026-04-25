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
 *
 * NOTE: This is the bare flip — it does NOT copy state. Prefer
 * migrateUserToCf / migrateUserToPhoton which orchestrate state copy + flip.
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

export interface MigrateResult {
  status: string
  user_id: string
  email: string
  backend: 'photon' | 'cf'
  tar_bytes: number
  r2_verify_bytes?: number
  photon_stopped?: boolean
  photon_started?: boolean
  cf_recycled?: boolean
  forced: boolean
  trace: string[]
}

/**
 * Phase F.4 M5: One-click V1 → V2 migration. Orchestrates everything:
 * IDLE pre-flight, /app/data/ tar export from Photon, R2 upload via the
 * user-v2 Worker, R2 round-trip verification, D1 backend flip, Photon
 * container stop, CF container recycle.
 *
 * Returns a `trace` array with step-by-step progress for debug.
 */
export async function migrateUserToCf(
  userId: string,
  force = false,
): Promise<MigrateResult> {
  return request(`/admin/migrate-to-cf/${userId}`, {
    method: 'POST',
    body: JSON.stringify({ force }),
  })
}

/**
 * Phase F.4 M5: V2 → V1 rollback. Force-flushes V2, downloads the R2
 * tarball, has the orchestrator extract it into the Photon container,
 * flips D1 back to 'photon', restarts Photon container.
 *
 * Photon container must already exist; refuses with a clear message
 * otherwise.
 */
export async function migrateUserToPhoton(
  userId: string,
  force = false,
): Promise<MigrateResult> {
  return request(`/admin/migrate-to-photon/${userId}`, {
    method: 'POST',
    body: JSON.stringify({ force }),
  })
}

// ── Migration history (Phase F.4 M7) ────────────────────────

export interface MigrationAuditRow {
  id: number
  user_id: string
  direction: 'to_cf' | 'to_photon'
  started_at: string
  finished_at: string | null
  status: 'in_progress' | 'success' | 'failed'
  error: string | null
  tar_bytes: number | null
  forced: number
  trace: string[]
}

export async function fetchMigrationHistory(
  userId?: string,
  limit = 20,
): Promise<{ rows: MigrationAuditRow[] }> {
  const qs = new URLSearchParams()
  if (userId) qs.set('user_id', userId)
  qs.set('limit', String(limit))
  return request<{ rows: MigrationAuditRow[] }>(`/admin/migration-history?${qs.toString()}`)
}

// ── V2 Persistence Status (Phase F.4 M6) ────────────────────

export interface PersistenceRow {
  user_id: string
  email: string
  backend: 'photon' | 'cf'
  r2_size_bytes: number | null
  r2_uploaded_at: string | null
  r2_age_s: number | null
  last_flush_ts: string | null
  last_flush_status: string | null
  last_flush_size: number | null
  last_restore_ts: string | null
  last_restore_status: string | null
  flushes_24h: number
  flush_errors_24h: number
  health: 'green' | 'yellow' | 'red' | 'idle'
  health_reason: string
}

export interface PersistenceStatusResponse {
  generated_at: string
  summary: {
    total_users: number
    on_v2: number
    green: number
    yellow: number
    red: number
  }
  rows: PersistenceRow[]
}

export async function fetchPersistenceStatus(): Promise<PersistenceStatusResponse> {
  return request<PersistenceStatusResponse>('/admin/persistence-status')
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
