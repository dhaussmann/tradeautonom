<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { useAppStore } from '@/stores/app'
import { useAuthStore } from '@/stores/auth'
import AppHeader from '@/components/layout/AppHeader.vue'
import VaultScreen from '@/components/VaultScreen.vue'
import MobileBottomNav from '@/components/mobile/MobileBottomNav.vue'

const appStore = useAppStore()
const authStore = useAuthStore()
const ready = ref(false)

onMounted(async () => {
  await authStore.checkSession()
  if (authStore.isAuthenticated) {
    await appStore.checkVault()
    if (appStore.vaultUnlocked) {
      appStore.checkHealth()
      setInterval(() => appStore.checkHealth(), 15000)
    }
  }
  ready.value = true
})

// Re-check vault when user authenticates after initial mount (login/register)
watch(() => authStore.isAuthenticated, async (isAuth) => {
  if (isAuth && !appStore.vaultChecked) {
    await appStore.checkVault()
    if (appStore.vaultUnlocked) {
      appStore.checkHealth()
      setInterval(() => appStore.checkHealth(), 15000)
    }
  }
})
</script>

<template>
  <div class="container">
    <!-- Loading while session + vault check in progress -->
    <template v-if="!ready">
      <div class="app-loading">
        <span class="spinner" />
      </div>
    </template>
    <!-- Vault locked/setup required -->
    <template v-else-if="authStore.isAuthenticated && appStore.needsVaultAction">
      <VaultScreen />
    </template>
    <!-- Normal app -->
    <template v-else>
      <AppHeader v-if="authStore.isAuthenticated" />
      <main class="app-main">
        <RouterView />
      </main>
      <!-- Mobile bottom navigation (only on mobile) -->
      <MobileBottomNav v-if="authStore.isAuthenticated" />
    </template>
  </div>
</template>

<style>
.container {
  display: flex;
  flex-direction: column;
  min-height: 100vh;
}

.app-main {
  flex: 1;
  overflow: auto;
}

/* Add padding for mobile bottom nav */
@media (max-width: 767px) {
  .app-main {
    padding-bottom: calc(64px + env(safe-area-inset-bottom, 0px));
  }
}

.app-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
}

.spinner {
  width: 32px;
  height: 32px;
  border: 3px solid var(--color-stroke-divider, #333);
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}
</style>
