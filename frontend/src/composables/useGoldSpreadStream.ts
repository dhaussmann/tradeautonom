import { ref, onUnmounted } from 'vue'
import { fetchGoldSpreadStatus } from '@/lib/gold-spread-api'
import type { GoldSpreadStatus } from '@/types/gold-spread'

/**
 * SSE-backed live status stream for the Gold-Spread bot. Mirrors
 * `useBotStream` (composables/useBotStream.ts) — initial REST fetch for an
 * immediate render, then upgrades to a Server-Sent Events stream against
 * `/api/gold-spread/stream` so the chart/UI updates without polling.
 */
export function useGoldSpreadStream(intervalMs = 2000) {
  const data = ref<GoldSpreadStatus | null>(null)
  const connected = ref(false)
  const error = ref<string | null>(null)
  let eventSource: EventSource | null = null

  function connect() {
    disconnect()
    // Immediate REST fetch so the UI has data instantly.
    fetchGoldSpreadStatus()
      .then((s) => {
        if (!data.value) data.value = s
      })
      .catch((err) => {
        error.value = err instanceof Error ? err.message : String(err)
      })
    // Upgrade to SSE for live updates.
    const url = `/api/gold-spread/stream?interval_ms=${intervalMs}`
    eventSource = new EventSource(url)
    eventSource.onopen = () => {
      connected.value = true
      error.value = null
    }
    eventSource.onmessage = (ev) => {
      try {
        data.value = JSON.parse(ev.data) as GoldSpreadStatus
      } catch {
        /* ignore parse errors */
      }
    }
    eventSource.onerror = () => {
      connected.value = false
      error.value = 'SSE connection lost'
    }
  }

  function disconnect() {
    if (eventSource) {
      eventSource.close()
      eventSource = null
    }
    connected.value = false
  }

  // Auto-connect on mount.
  connect()
  onUnmounted(disconnect)

  return { data, connected, error, reconnect: connect, disconnect }
}
