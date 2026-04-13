import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('@/views/LoginView.vue'),
    meta: { public: true },
  },
  {
    path: '/',
    name: 'dashboard',
    component: () => import('@/views/DashboardView.vue'),
  },
  {
    path: '/bot/:botId',
    name: 'bot-detail',
    component: () => import('@/views/BotDetailView.vue'),
    props: true,
  },
  {
    path: '/positions',
    name: 'positions',
    component: () => import('@/views/PositionsView.vue'),
  },
  {
    path: '/account',
    name: 'account',
    component: () => import('@/views/AccountView.vue'),
  },
  {
    path: '/history',
    name: 'history',
    component: () => import('@/views/HistoryView.vue'),
  },
  {
    path: '/markets',
    name: 'markets',
    component: () => import('@/views/MarketsView.vue'),
  },
  {
    path: '/strategies',
    name: 'strategies',
    component: () => import('@/views/StrategiesView.vue'),
  },
  {
    path: '/settings',
    name: 'settings',
    component: () => import('@/views/SettingsView.vue'),
  },
  {
    path: '/admin',
    name: 'admin',
    component: () => import('@/views/AdminView.vue'),
    meta: { admin: true },
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/',
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  if (to.meta.public) return true

  const authStore = useAuthStore()
  // If we haven't checked the session yet, do so now
  if (authStore.loading) {
    await authStore.checkSession()
  }
  if (!authStore.isAuthenticated) {
    return { name: 'login' }
  }
  return true
})

export default router
