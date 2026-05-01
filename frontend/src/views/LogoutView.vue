<script setup lang="ts">
/**
 * Always-reachable logout escape hatch.
 *
 * Routed at /logout with `meta: { public: true }` so the auth guard in
 * router/index.ts lets it through unconditionally. Reachable from:
 *   - The address bar (bookmark / direct nav)
 *   - The Sign-out button in AppHeader (desktop + mobile)
 *   - The Sign-out link on VaultScreen (when stuck on vault locked/setup)
 *
 * Behaviour:
 *   1. Call authStore.logout() — better-auth signOut() + local state reset.
 *      Wrapped in try/catch so a network failure does not strand us.
 *   2. Reset every session-scoped Pinia store explicitly. We do not blanket
 *      $reset all stores — global UI prefs (none today, but planned) should
 *      survive logout.
 *   3. Best-effort clear of the non-HttpOnly better-auth cookie cache.
 *      The HttpOnly session_token cookie can only be cleared by the server
 *      response from a successful signOut(); if that failed, the cookie may
 *      still exist but the session it points to was either invalidated or
 *      will be on the next signOut retry.
 *   4. Redirect to /login. The router guard will keep us there because
 *      authStore.user is null (set by step 1's finally block).
 *
 * On signOut() failure we surface the error and offer a Try-again button.
 * The local state (Pinia stores, user.value) is already cleaned at that
 * point so the next /login attempt starts from a clean slate regardless.
 */
import { onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useAppStore } from '@/stores/app'
import { useAccountStore } from '@/stores/account'
import { useBotsStore } from '@/stores/bots'

const router = useRouter()
const authStore = useAuthStore()
const appStore = useAppStore()
const accountStore = useAccountStore()
const botsStore = useBotsStore()

const error = ref<string | null>(null)
const inFlight = ref(false)

function resetSessionStores() {
  appStore.resetSession()
  accountStore.resetSession()
  botsStore.resetSession()
}

async function performLogout() {
  if (inFlight.value) return
  inFlight.value = true
  error.value = null
  try {
    await authStore.logout()
  } catch (e) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    // Always clean local state so the next session starts fresh, even if
    // the server signOut failed.
    resetSessionStores()
    // Best-effort: clear the non-HttpOnly cookie cache. HttpOnly cookies
    // (better-auth.session_token) cannot be cleared from JS — they will
    // be cleared by a successful signOut() response or expire on their own.
    try {
      document.cookie = 'better-auth.session_data=; Max-Age=0; path=/'
    } catch {
      /* cookie API unavailable in some embedded contexts — ignore */
    }
    inFlight.value = false
  }
  if (!error.value) {
    router.replace({ name: 'login' })
  }
}

onMounted(performLogout)
</script>

<template>
  <div :class="$style.page">
    <div :class="$style.card">
      <template v-if="!error">
        <span :class="$style.spinner" />
        <p>Signing out…</p>
      </template>
      <template v-else>
        <p :class="$style.errorText">Logout had a hiccup: {{ error }}</p>
        <p :class="$style.hint">
          Local session data has been cleared. You can try again or just go
          to the login page.
        </p>
        <div :class="$style.actions">
          <button :class="$style.btn" :disabled="inFlight" @click="performLogout">
            {{ inFlight ? 'Working…' : 'Try again' }}
          </button>
          <button :class="$style.btnSecondary" @click="router.replace({ name: 'login' })">
            Go to login
          </button>
        </div>
      </template>
    </div>
  </div>
</template>

<style module>
.page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: var(--space-6);
}

.card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-6);
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  max-width: 420px;
  width: 100%;
  text-align: center;
}

.spinner {
  width: 28px;
  height: 28px;
  border: 3px solid var(--color-stroke-divider, #333);
  border-top-color: transparent;
  border-radius: 50%;
  animation: lv-spin 0.8s linear infinite;
}

@keyframes lv-spin {
  to { transform: rotate(360deg); }
}

.errorText {
  color: var(--color-error, #dc3545);
  margin: 0;
}

.hint {
  color: var(--color-text-tertiary);
  font-size: var(--text-sm);
  margin: 0;
}

.actions {
  display: flex;
  gap: var(--space-3);
  margin-top: var(--space-3);
}

.btn,
.btnSecondary {
  padding: var(--space-2) var(--space-4);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  color: var(--color-text-primary);
  cursor: pointer;
  transition: all 0.15s;
}

.btn:hover:not(:disabled),
.btnSecondary:hover {
  border-color: var(--color-text-tertiary);
  background: var(--color-white-2);
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btnSecondary {
  background: transparent;
}
</style>
