import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { Position, AccountSummary } from '@/types/account'
import { fetchAccountAll, fetchPositions, fetchAccountHealth } from '@/lib/api'
import type { ExchangeHealth } from '@/lib/api'

export const useAccountStore = defineStore('account', () => {
  const accounts = ref<AccountSummary[]>([])
  const positions = ref<Position[]>([])
  // Per-exchange auth/connectivity health. Populated by loadHealth();
  // consumed by views to render a "token expired/revoked" banner when
  // an exchange (currently only Variational) reports ok=false.
  const health = ref<Record<string, ExchangeHealth>>({})
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function loadAccounts() {
    loading.value = true
    error.value = null
    try {
      accounts.value = await fetchAccountAll()
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to load accounts'
    } finally {
      loading.value = false
    }
  }

  async function loadPositions() {
    loading.value = true
    error.value = null
    try {
      positions.value = await fetchPositions()
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to load positions'
    } finally {
      loading.value = false
    }
  }

  async function loadHealth() {
    try {
      health.value = await fetchAccountHealth()
    } catch (e) {
      // Health is best-effort — never block UI on it
      console.warn('loadHealth failed:', e)
    }
  }

  /** Reset all session-scoped account state (called by /logout). */
  function resetSession() {
    accounts.value = []
    positions.value = []
    health.value = {}
    loading.value = false
    error.value = null
  }

  return { accounts, positions, health, loading, error, loadAccounts, loadPositions, loadHealth, resetSession }
})
