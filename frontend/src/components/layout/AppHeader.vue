<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAppStore } from '@/stores/app'
import { useAuthStore } from '@/stores/auth'
import Typography from '@/components/ui/Typography.vue'
import StatusDot from '@/components/ui/StatusDot.vue'

const route = useRoute()
const router = useRouter()
const appStore = useAppStore()
const authStore = useAuthStore()

async function handleLogout() {
  await authStore.logout()
  router.push('/login')
}

const baseLinks = [
  { to: '/', label: 'Dashboard', name: 'dashboard' },
  { to: '/strategies', label: 'Strategies', name: 'strategies' },
  { to: '/arbitrage', label: 'Arbitrage', name: 'arbitrage' },
  { to: '/dna', label: 'DNA Bot', name: 'dna' },
  { to: '/positions', label: 'Positions', name: 'positions' },
  { to: '/history', label: 'History', name: 'history' },
  { to: '/account', label: 'Account', name: 'account' },
  { to: '/markets', label: 'Markets', name: 'markets' },
  { to: '/settings', label: 'Settings', name: 'settings' },
]

const navLinks = computed(() => {
  if (authStore.isAdmin) {
    return [...baseLinks, { to: '/admin', label: 'Admin', name: 'admin' }, { to: '/admin/activity', label: 'Activity', name: 'admin-activity' }]
  }
  return baseLinks
})
</script>

<template>
  <header :class="$style.header">
    <nav :class="$style.nav">
      <div :class="$style.brand">
        <Typography size="text-lg" weight="semibold" font="bricolage">TradeAutonom</Typography>
      </div>
      <div :class="$style.links">
        <RouterLink
          v-for="link in navLinks"
          :key="link.name"
          :to="link.to"
          :class="[$style.link, route.name === link.name && $style['link--active']]"
        >
          {{ link.label }}
        </RouterLink>
      </div>
      <div :class="$style.right">
        <div :class="$style.status">
          <StatusDot :active="appStore.connected" :color="appStore.connected ? 'success' : 'error'" />
          <Typography size="text-sm" :color="appStore.connected ? 'secondary' : 'error'">
            {{ appStore.connected ? 'Connected' : 'Disconnected' }}
          </Typography>
        </div>
        <div v-if="authStore.user" :class="$style.user">
          <Typography size="text-sm" color="tertiary">{{ authStore.user.email }}</Typography>
          <button :class="$style.logoutBtn" @click="handleLogout">
            <Typography size="text-xs" color="secondary">Logout</Typography>
          </button>
        </div>
      </div>
    </nav>
  </header>
</template>

<style module>
.header {
  padding: 0 2.5rem;
  height: 56px;
  position: relative;
  display: flex;
  align-items: center;
  border-bottom: 1px solid var(--color-stroke-divider);
}

.nav {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
  padding: 8px 0;
}

.brand {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.links {
  display: flex;
  gap: 2rem;
}

.link {
  font-size: var(--text-md);
  color: var(--color-text-secondary);
  transition: color var(--duration-md);
  padding: 0.5rem 0;
}

.link:hover {
  color: var(--color-text-primary);
}

.link--active {
  color: var(--color-text-primary);
  border-bottom: 1px solid var(--color-text-primary);
}

.right {
  display: flex;
  align-items: center;
  gap: var(--space-5);
}

.status {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.user {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.logoutBtn {
  background: none;
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-md);
  padding: var(--space-1) var(--space-3);
  cursor: pointer;
  transition: all 0.15s;
}

.logoutBtn:hover {
  border-color: var(--color-text-tertiary);
}
</style>
