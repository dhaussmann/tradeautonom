<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const authStore = useAuthStore()

// Core navigation items (showing 5 max on mobile bottom nav)
const mainNavItems = [
  { to: '/', label: 'Dashboard', icon: '⊞', name: 'dashboard' },
  { to: '/bots', label: 'Bots', icon: '⚡', name: 'bots' },
  { to: '/positions', label: 'Positions', icon: '◎', name: 'positions' },
  { to: '/arbitrage', label: 'Arbitrage', icon: '⇄', name: 'arbitrage' },
  { to: '/account', label: 'Account', icon: '👤', name: 'account' },
]

// Additional items in "More" menu
const moreNavItems = [
  { to: '/strategies', label: 'Strategies', icon: '📋', name: 'strategies' },
  { to: '/dna', label: 'DNA Bot', icon: '🧬', name: 'dna' },
  { to: '/history', label: 'History', icon: '📊', name: 'history' },
  { to: '/markets', label: 'Markets', icon: '📈', name: 'markets' },
  { to: '/settings', label: 'Settings', icon: '⚙', name: 'settings' },
]

const adminNavItems = [
  { to: '/admin', label: 'Admin', icon: '🔒', name: 'admin' },
  { to: '/admin/activity', label: 'Activity', icon: '📋', name: 'admin-activity' },
]

const showMoreMenu = computed(() => {
  const moreNames = moreNavItems.map(i => i.name)
  const adminNames = adminNavItems.map(i => i.name)
  return moreNames.includes(route.name as string) || adminNames.includes(route.name as string)
})

function navigate(to: string) {
  router.push(to)
}

const allMoreItems = computed(() => [
  ...moreNavItems,
  ...(authStore.isAdmin ? adminNavItems : [])
])
</script>

<template>
  <nav :class="$style.bottomNav">
    <!-- Main navigation items -->
    <button
      v-for="item in mainNavItems"
      :key="item.name"
      :class="[$style.navItem, route.name === item.name && $style.navItemActive]"
      @click="navigate(item.to)"
    >
      <span :class="$style.icon">{{ item.icon }}</span>
      <span :class="$style.label">{{ item.label }}</span>
    </button>

    <!-- More menu button -->
    <button
      :class="[$style.navItem, showMoreMenu && $style.navItemActive]"
      @click="navigate('/settings')"
    >
      <span :class="$style.icon">⋯</span>
      <span :class="$style.label">More</span>
    </button>
  </nav>

  <!-- More menu overlay (shown when on a "More" page) -->
  <Transition name="slide-up">
    <div v-if="showMoreMenu" :class="$style.moreMenu">
      <div :class="$style.moreMenuHeader">
        <span>More Options</span>
        <button :class="$style.closeBtn" @click="navigate('/')">✕</button>
      </div>
      <div :class="$style.moreMenuItems">
        <button
          v-for="item in allMoreItems"
          :key="item.name"
          :class="[$style.moreItem, route.name === item.name && $style.moreItemActive]"
          @click="navigate(item.to)"
        >
          <span :class="$style.moreIcon">{{ item.icon }}</span>
          <span :class="$style.moreLabel">{{ item.label }}</span>
        </button>
      </div>
    </div>
  </Transition>
</template>

<style module>
.bottomNav {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  display: flex;
  justify-content: space-around;
  align-items: center;
  height: calc(64px + var(--safe-area-inset-bottom));
  padding-bottom: var(--safe-area-inset-bottom);
  background: var(--color-bg-secondary);
  border-top: 1px solid var(--color-stroke-divider);
  z-index: 100;
}

.navItem {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  flex: 1;
  height: 100%;
  background: none;
  border: none;
  cursor: pointer;
  color: var(--color-text-tertiary);
  transition: color 0.15s ease;
  min-width: var(--touch-target-min);
}

.navItem:hover,
.navItem:active {
  color: var(--color-text-secondary);
}

.navItemActive {
  color: var(--color-brand);
}

.icon {
  font-size: 20px;
  line-height: 1;
}

.label {
  font-size: 10px;
  font-weight: 500;
  white-space: nowrap;
}

/* More menu overlay */
.moreMenu {
  position: fixed;
  bottom: calc(64px + var(--safe-area-inset-bottom));
  left: 0;
  right: 0;
  background: var(--color-bg-secondary);
  border-top: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-xl) var(--radius-xl) 0 0;
  z-index: 99;
  max-height: 60vh;
  overflow-y: auto;
}

.moreMenuHeader {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-stroke-divider);
  font-size: var(--text-md);
  font-weight: 600;
  color: var(--color-text-primary);
}

.closeBtn {
  background: none;
  border: none;
  color: var(--color-text-secondary);
  font-size: 18px;
  cursor: pointer;
  padding: var(--space-2);
  min-width: var(--touch-target-min);
  min-height: var(--touch-target-min);
  display: flex;
  align-items: center;
  justify-content: center;
}

.moreMenuItems {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--space-2);
  padding: var(--space-4);
}

.moreItem {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-4) var(--space-2);
  background: var(--color-white-4);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-lg);
  cursor: pointer;
  color: var(--color-text-secondary);
  transition: all 0.15s ease;
  min-height: var(--touch-target-comfortable);
}

.moreItem:hover,
.moreItem:active {
  background: var(--color-white-10);
  border-color: var(--color-stroke-primary);
}

.moreItemActive {
  background: var(--color-brand-bg);
  border-color: var(--color-brand-stroke);
  color: var(--color-brand);
}

.moreIcon {
  font-size: 24px;
  line-height: 1;
}

.moreLabel {
  font-size: var(--text-xs);
  font-weight: 500;
}

/* Slide animation */
:global(.slide-up-enter-active),
:global(.slide-up-leave-active) {
  transition: transform 0.2s ease-out;
}

:global(.slide-up-enter-from),
:global(.slide-up-leave-to) {
  transform: translateY(100%);
}

/* Hide on desktop */
@media (min-width: 768px) {
  .bottomNav,
  .moreMenu {
    display: none;
  }
}
</style>
