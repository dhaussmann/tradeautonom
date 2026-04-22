/**
 * useMobile composable — reactive mobile detection
 *
 * Provides:
 * - isMobile: < 768px (phones)
 * - isTablet: 768px - 1024px (tablets)
 * - isDesktop: > 1024px (desktops)
 * - isTouch: device has touch capability
 * - orientation: 'portrait' | 'landscape'
 */

import { ref, computed, onMounted, onUnmounted } from 'vue'

const MOBILE_BREAKPOINT = 768
const TABLET_BREAKPOINT = 1024

export function useMobile() {
  const width = ref(window.innerWidth)
  const height = ref(window.innerHeight)

  const isMobile = computed(() => width.value < MOBILE_BREAKPOINT)
  const isTablet = computed(() => width.value >= MOBILE_BREAKPOINT && width.value < TABLET_BREAKPOINT)
  const isDesktop = computed(() => width.value >= TABLET_BREAKPOINT)
  const isTouch = computed(() => 'ontouchstart' in window || navigator.maxTouchPoints > 0)
  const orientation = computed(() => width.value > height.value ? 'landscape' : 'portrait')

  function updateDimensions() {
    width.value = window.innerWidth
    height.value = window.innerHeight
  }

  onMounted(() => {
    window.addEventListener('resize', updateDimensions, { passive: true })
    // Also listen for orientation changes on mobile
    window.addEventListener('orientationchange', updateDimensions)
  })

  onUnmounted(() => {
    window.removeEventListener('resize', updateDimensions)
    window.removeEventListener('orientationchange', updateDimensions)
  })

  return {
    width: readonly(width),
    height: readonly(height),
    isMobile,
    isTablet,
    isDesktop,
    isTouch,
    orientation,
  }
}

// Helper to make refs readonly
function readonly<T>(ref: { value: T }) {
  return computed(() => ref.value)
}

/**
 * useMediaQuery — reactive media query matcher
 */
export function useMediaQuery(query: string) {
  const matches = ref(false)
  let mediaQuery: MediaQueryList | null = null

  onMounted(() => {
    mediaQuery = window.matchMedia(query)
    matches.value = mediaQuery.matches

    const handler = (e: MediaQueryListEvent) => {
      matches.value = e.matches
    }

    mediaQuery.addEventListener('change', handler)

    onUnmounted(() => {
      mediaQuery?.removeEventListener('change', handler)
    })
  })

  return matches
}
