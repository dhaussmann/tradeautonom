import { ref, onMounted, onUnmounted } from 'vue'
import type { PortfolioSnapshot } from '@/types/portfolio'

export function usePortfolioStream(intervalMs = 3000) {
  const data = ref<PortfolioSnapshot | null>(null)
  const connected = ref(false)
  const error = ref<string | null>(null)
  let eventSource: EventSource | null = null

  function connect() {
    disconnect()
    const url = `/api/portfolio/stream?interval_ms=${intervalMs}`
    eventSource = new EventSource(url)
    eventSource.onopen = () => {
      connected.value = true
      error.value = null
    }
    eventSource.onmessage = (ev) => {
      try {
        data.value = JSON.parse(ev.data)
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

  onMounted(connect)
  onUnmounted(disconnect)

  return { data, connected, error, disconnect }
}
