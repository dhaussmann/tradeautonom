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
