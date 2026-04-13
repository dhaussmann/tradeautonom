import { ref, onUnmounted, watch, type Ref } from 'vue'
import { fetchBotStatus } from '@/lib/api'
import type { BotStatus } from '@/types/bot'

export function useBotStream(botId: Ref<string | null>, intervalMs = 2000) {
  const data = ref<BotStatus | null>(null)
  const connected = ref(false)
  const error = ref<string | null>(null)
  let eventSource: EventSource | null = null

  function connect(id: string) {
    disconnect()
    // Immediate REST fetch so the UI renders right away
    fetchBotStatus(id).then(s => {
      if (!data.value) data.value = s
    }).catch(() => {})
    // Then open SSE for live updates
    const url = `/api/fn/bots/${id}/stream?interval_ms=${intervalMs}`
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

  watch(botId, (id) => {
    if (id) connect(id)
    else disconnect()
  }, { immediate: true })

  onUnmounted(disconnect)

  return { data, connected, error, disconnect }
}
