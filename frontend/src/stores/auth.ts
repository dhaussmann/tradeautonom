import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { authClient } from '@/lib/auth'
import { fetchAdminCheck } from '@/lib/admin-api'

export const useAuthStore = defineStore('auth', () => {
  const user = ref<{ id: string; email: string; name: string } | null>(null)
  const loading = ref(true)
  const error = ref<string | null>(null)

  const isAuthenticated = computed(() => !!user.value)
  const isAdmin = ref(false)

  async function checkSession() {
    loading.value = true
    try {
      const session = await authClient.getSession()
      if (session.data?.user) {
        user.value = session.data.user as { id: string; email: string; name: string }
        // Check admin status
        try {
          const adminResp = await fetchAdminCheck()
          isAdmin.value = adminResp.is_admin
        } catch {
          isAdmin.value = false
        }
      } else {
        user.value = null
        isAdmin.value = false
      }
      error.value = null
    } catch {
      user.value = null
      isAdmin.value = false
    } finally {
      loading.value = false
    }
  }

  async function login(email: string, password: string) {
    error.value = null
    const result = await authClient.signIn.email({ email, password })
    if (result.error) {
      error.value = result.error.message || 'Login failed'
      return false
    }
    await checkSession()
    return true
  }

  async function register(email: string, password: string, name: string) {
    error.value = null
    const result = await authClient.signUp.email({ email, password, name })
    if (result.error) {
      error.value = result.error.message || 'Registration failed'
      return false
    }
    await checkSession()
    return true
  }

  async function logout() {
    await authClient.signOut()
    user.value = null
  }

  return { user, loading, error, isAuthenticated, isAdmin, checkSession, login, register, logout }
})
