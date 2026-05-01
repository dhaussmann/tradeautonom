import { defineStore } from 'pinia'
import { ref } from 'vue'
import type { BotSummary } from '@/types/bot'
import { fetchBots, createBot, deleteBot, startBot, stopBot, killBot, pauseBot, resumeBot, resetBot } from '@/lib/api'
import type { BotCreateRequest, BotStartRequest } from '@/types/bot'

export const useBotsStore = defineStore('bots', () => {
  const bots = ref<BotSummary[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)

  async function load() {
    loading.value = true
    error.value = null
    try {
      bots.value = await fetchBots()
    } catch (e) {
      error.value = e instanceof Error ? e.message : 'Failed to load bots'
    } finally {
      loading.value = false
    }
  }

  async function create(req: BotCreateRequest) {
    const result = await createBot(req)
    await load()
    return result
  }

  async function remove(botId: string) {
    const result = await deleteBot(botId)
    await load()
    return result
  }

  async function start(botId: string, req?: BotStartRequest) {
    const result = await startBot(botId, req)
    await load()
    return result
  }

  async function stop(botId: string) {
    const result = await stopBot(botId)
    await load()
    return result
  }

  async function kill(botId: string) {
    const result = await killBot(botId)
    await load()
    return result
  }

  async function pause(botId: string) {
    const result = await pauseBot(botId)
    await load()
    return result
  }

  async function resume(botId: string) {
    const result = await resumeBot(botId)
    await load()
    return result
  }

  async function reset(botId: string) {
    const result = await resetBot(botId)
    await load()
    return result
  }

  /** Reset all session-scoped bot state (called by /logout). */
  function resetSession() {
    bots.value = []
    loading.value = false
    error.value = null
  }

  return { bots, loading, error, load, create, remove, start, stop, kill, pause, resume, reset, resetSession }
})
