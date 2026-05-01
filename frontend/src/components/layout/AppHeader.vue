<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAppStore } from '@/stores/app'
import { useAuthStore } from '@/stores/auth'
import Typography from '@/components/ui/Typography.vue'
import StatusDot from '@/components/ui/StatusDot.vue'

const route = useRoute()
const router = useRouter()
const appStore = useAppStore()
const authStore = useAuthStore()

const mobileMenuOpen = ref(false)

// Route through /logout instead of calling authStore.logout() directly so
// every logout path (header desktop button, mobile menu, vault-screen
// link, address-bar nav) shares the same Pinia-store reset and cookie
// cleanup logic in LogoutView.vue.
function handleLogout() {
  mobileMenuOpen.value = false
  router.push({ name: 'logout' })
}

const baseLinks = [
  { to: '/', label: 'Dashboard', name: 'dashboard' },
  { to: '/bots', label: 'Bots', name: 'bots' },
  { to: '/strategies', label: 'Strategies', name: 'strategies' },
  { to: '/arbitrage', label: 'Arbitrage', name: 'arbitrage' },
  { to: '/dna', label: 'DNA Bot', name: 'dna' },
  { to: '/gold-spread', label: 'Gold Spread', name: 'gold-spread' },
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

function navigate(to: string) {
  mobileMenuOpen.value = false
  router.push(to)
}
</script>

<template>
  <header :class="$style.header">
    <nav :class="$style.nav">
      <!-- Brand -->
      <div :class="$style.brand">
        <Typography size="text-lg" weight="semibold" font="bricolage">TradeAutonom</Typography>
      </div>

      <!-- Desktop Navigation -->
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

      <!-- Desktop Right Section -->
      <div :class="$style.right">
        <div :class="$style.status">
          <StatusDot :active="appStore.connected" :color="appStore.connected ? 'success' : 'error'" />
          <Typography size="text-sm" :color="appStore.connected ? 'secondary' : 'error'">
            {{ appStore.connected ? 'Connected' : 'Disconnected' }}
          </Typography>
        </div>
        <div v-if="authStore.user" :class="$style.user">
          <Typography size="text-sm" color="tertiary" :class="$style.userEmail">{{ authStore.user.email }}</Typography>
          <button :class="$style.logoutBtn" @click="handleLogout">
            <Typography size="text-xs" color="secondary">Logout</Typography>
          </button>
        </div>
      </div>

      <!-- Mobile Menu Button -->
      <button
        :class="[$style.menuBtn, mobileMenuOpen && $style.menuBtnOpen]"
        @click="mobileMenuOpen = !mobileMenuOpen"
        aria-label="Toggle menu"
      >
        <span :class="$style.menuIcon" />
      </button>
    </nav>

    <!-- Mobile Menu Overlay -->
    <Transition name="mobile-menu">
      <div v-if="mobileMenuOpen" :class="$style.mobileMenu">
        <div :class="$style.mobileMenuContent">
          <!-- Mobile Status -->
          <div :class="$style.mobileStatus">
            <StatusDot :active="appStore.connected" :color="appStore.connected ? 'success' : 'error'" />
            <Typography size="text-sm" :color="appStore.connected ? 'secondary' : 'error'">
              {{ appStore.connected ? 'Connected' : 'Disconnected' }}
            </Typography>
          </div>

          <!-- Mobile Nav Links -->
          <div :class="$style.mobileLinks">
            <button
              v-for="link in navLinks"
              :key="link.name"
              :class="[$style.mobileLink, route.name === link.name && $style.mobileLinkActive]"
              @click="navigate(link.to)"
            >
              <Typography size="text-md" :weight="route.name === link.name ? 'semibold' : 'normal'">
                {{ link.label }}
              </Typography>
            </button>
          </div>

          <!-- Mobile User Section -->
          <div v-if="authStore.user" :class="$style.mobileUser">
            <Typography size="text-sm" color="tertiary">{{ authStore.user.email }}</Typography>
            <button :class="$style.mobileLogoutBtn" @click="handleLogout">
              <Typography size="text-sm" color="error">Logout</Typography>
            </button>
          </div>
        </div>
      </div>
    </Transition>
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

/* Mobile Menu Button */
.menuBtn {
  display: none;
  width: var(--touch-target-min);
  height: var(--touch-target-min);
  background: none;
  border: none;
  cursor: pointer;
  position: relative;
  padding: 0;
}

.menuIcon {
  display: block;
  width: 24px;
  height: 2px;
  background: var(--color-text-primary);
  position: absolute;
  top: 50%;
  left: 50%;
  transform: translate(-50%, -50%);
  transition: background 0.2s ease;
}

.menuIcon::before,
.menuIcon::after {
  content: '';
  position: absolute;
  width: 24px;
  height: 2px;
  background: var(--color-text-primary);
  transition: transform 0.2s ease;
}

.menuIcon::before {
  top: -7px;
}

.menuIcon::after {
  top: 7px;
}

.menuBtnOpen .menuIcon {
  background: transparent;
}

.menuBtnOpen .menuIcon::before {
  transform: rotate(45deg);
  top: 0;
}

.menuBtnOpen .menuIcon::after {
  transform: rotate(-45deg);
  top: 0;
}

/* Mobile Menu Overlay */
.mobileMenu {
  display: none;
  position: fixed;
  top: 56px;
  left: 0;
  right: 0;
  bottom: 0;
  background: var(--color-bg-primary);
  z-index: 90;
  overflow-y: auto;
}

.mobileMenuContent {
  display: flex;
  flex-direction: column;
  padding: var(--space-4);
  gap: var(--space-6);
}

.mobileStatus {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-3);
  background: var(--color-white-4);
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-stroke-divider);
}

.mobileLinks {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.mobileLink {
  display: flex;
  align-items: center;
  padding: var(--space-3) var(--space-4);
  background: none;
  border: none;
  border-radius: var(--radius-md);
  cursor: pointer;
  color: var(--color-text-secondary);
  text-align: left;
  min-height: var(--touch-target-comfortable);
  transition: all 0.15s ease;
}

.mobileLink:hover,
.mobileLink:active {
  background: var(--color-white-4);
  color: var(--color-text-primary);
}

.mobileLinkActive {
  background: var(--color-brand-bg);
  color: var(--color-brand);
}

.mobileLinkActive:hover,
.mobileLinkActive:active {
  background: var(--color-brand-bg);
}

.mobileUser {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-4);
  background: var(--color-white-4);
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-stroke-divider);
  margin-top: auto;
}

.mobileLogoutBtn {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: var(--space-3);
  background: var(--color-error-bg);
  border: 1px solid var(--color-error-stroke);
  border-radius: var(--radius-md);
  cursor: pointer;
  min-height: var(--touch-target-comfortable);
  transition: all 0.15s ease;
}

.mobileLogoutBtn:hover,
.mobileLogoutBtn:active {
  background: rgba(220, 53, 69, 0.2);
}

/* Mobile Breakpoints */
@media (max-width: 1024px) {
  .header {
    padding: 0 1rem;
  }

  .links {
    gap: 1rem;
  }

  .userEmail {
    display: none;
  }
}

@media (max-width: 767px) {
  .header {
    padding: 0 var(--space-4);
  }

  .links,
  .right {
    display: none;
  }

  .menuBtn {
    display: flex;
    align-items: center;
    justify-content: center;
  }

  .mobileMenu {
    display: block;
  }
}

/* Transitions */
:global(.mobile-menu-enter-active),
:global(.mobile-menu-leave-active) {
  transition: opacity 0.2s ease, transform 0.2s ease;
}

:global(.mobile-menu-enter-from),
:global(.mobile-menu-leave-to) {
  opacity: 0;
  transform: translateY(-10px);
}
</style>
