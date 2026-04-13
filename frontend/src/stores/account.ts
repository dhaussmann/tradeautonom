import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { Position, AccountSummary } from '@/types/account'
import { fetchAccountAll, fetchPositions } from '@/lib/api'

export const useAccountStore = defineStore('account', () => {
  const accounts = ref<AccountSummary[]>([])
  const positions = ref<Position[]>([])
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

  return { accounts, positions, loading, error, loadAccounts, loadPositions }
})
