import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import {
  fetchHealth,
  fetchVaultStatus,
  unlockVault as apiUnlockVault,
  setupVault as apiSetupVault,
  updateSecretsKeys,
} from '@/lib/api'

export const useAppStore = defineStore('app', () => {
  const connected = ref(false)
  const backendEnv = ref('')
  const error = ref<string | null>(null)

  // Vault state
  const vaultLocked = ref(false)
  const vaultSetupRequired = ref(false)
  const vaultUnlocked = ref(false)
  const vaultChecked = ref(false)
  const vaultError = ref<string | null>(null)
  const d1HasKeys = ref(false)
  const autoInjected = ref(false)

  // needsVaultAction: true when checked but not unlocked
  // (user must either enter vault password or configure API keys)
  const needsVaultAction = computed(() => vaultChecked.value && !vaultUnlocked.value)

  // needsKeySetup: new user with no keys anywhere
  const needsKeySetup = computed(() =>
    vaultChecked.value && !vaultUnlocked.value && !d1HasKeys.value && vaultSetupRequired.value
  )

  async function checkHealth() {
    try {
      const data = await fetchHealth()
      connected.value = data.status === 'ok'
      backendEnv.value = data.grvt_env
      error.value = null
    } catch (e) {
      connected.value = false
      error.value = e instanceof Error ? e.message : 'Unknown error'
    }
  }

  async function checkVault() {
    try {
      const data = await fetchVaultStatus() as any
      vaultSetupRequired.value = data.setup_required ?? false
      vaultLocked.value = data.locked ?? false
      vaultUnlocked.value = data.unlocked ?? false
      d1HasKeys.value = data.d1_has_keys ?? false
      autoInjected.value = data.auto_injected ?? false
      vaultError.value = null
    } catch (e) {
      vaultError.value = e instanceof Error ? e.message : 'Unknown error'
    } finally {
      vaultChecked.value = true
    }
  }

  async function unlockVault(password: string): Promise<boolean> {
    vaultError.value = null
    try {
      await apiUnlockVault(password)
      vaultLocked.value = false
      vaultUnlocked.value = true
      return true
    } catch (e) {
      vaultError.value = e instanceof Error ? e.message : 'Unlock failed'
      return false
    }
  }

  async function setupVault(password: string): Promise<boolean> {
    vaultError.value = null
    try {
      await apiSetupVault(password)
      vaultSetupRequired.value = false
      vaultLocked.value = false
      vaultUnlocked.value = true
      return true
    } catch (e) {
      vaultError.value = e instanceof Error ? e.message : 'Setup failed'
      return false
    }
  }

  async function saveKeys(keys: Record<string, string>): Promise<boolean> {
    vaultError.value = null
    try {
      const result = await updateSecretsKeys(keys)
      if (result.container_updated) {
        vaultUnlocked.value = true
        vaultLocked.value = false
        vaultSetupRequired.value = false
        d1HasKeys.value = true
      }
      return result.container_updated
    } catch (e) {
      vaultError.value = e instanceof Error ? e.message : 'Failed to save keys'
      return false
    }
  }

  /**
   * Reset all session-scoped state. Called by /logout so the next user's
   * session starts clean — no leftover vault flags, no stale connected
   * status, no error messages from a previous failed unlock.
   *
   * `connected` and `backendEnv` are also reset because they reflect the
   * /health endpoint of the previous user's container; the next session
   * will repopulate them via App.vue's onMounted hook.
   */
  function resetSession() {
    connected.value = false
    backendEnv.value = ''
    error.value = null
    vaultLocked.value = false
    vaultSetupRequired.value = false
    vaultUnlocked.value = false
    vaultChecked.value = false
    vaultError.value = null
    d1HasKeys.value = false
    autoInjected.value = false
  }

  return {
    connected, backendEnv, error, checkHealth,
    vaultLocked, vaultSetupRequired, vaultUnlocked, vaultChecked, vaultError,
    d1HasKeys, autoInjected, needsVaultAction, needsKeySetup,
    checkVault, unlockVault, setupVault, saveKeys, resetSession,
  }
})
