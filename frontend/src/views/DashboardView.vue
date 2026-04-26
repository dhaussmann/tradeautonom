<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useBotsStore } from '@/stores/bots'
import { useAccountStore } from '@/stores/account'
import {
  fetchExchanges,
  fetchJournalPoints,
  fetchJournalSummary,
  fetchJournalPositions,
} from '@/lib/api'
import Typography from '@/components/ui/Typography.vue'
import DashboardStatCard from '@/components/dashboard/DashboardStatCard.vue'
import BotMiniCard from '@/components/dashboard/BotMiniCard.vue'
import BotCreateModal from '@/components/dashboard/BotCreateModal.vue'
import BotStateTiles, {
  type BotStateFilter,
} from '@/components/dashboard/BotStateTiles.vue'
import AdvancedAnalyticsSection from '@/components/dashboard/AdvancedAnalyticsSection.vue'
import type { BotCreateRequest, BotSummary } from '@/types/bot'
import type {
  PointsRecord,
  JournalSummary,
  Position as JournalPosition,
  PositionStats,
} from '@/types/journal'

const router = useRouter()
const botsStore = useBotsStore()
const accountStore = useAccountStore()

const showCreateModal = ref(false)
const exchanges = ref<string[]>([])
const actionLoading = ref<Record<string, string | null>>({})
const stateFilter = ref<BotStateFilter>('ALL')
let pollInterval: ReturnType<typeof setInterval> | null = null
let analyticsPollInterval: ReturnType<typeof setInterval> | null = null

// Analytics data
const allPoints = ref<PointsRecord[]>([])
const summaryAll = ref<JournalSummary | null>(null)
const summaryByToken = ref<JournalSummary | null>(null)
const closedPositions = ref<JournalPosition[]>([])
const positionStats = ref<PositionStats | null>(null)

// ── Computed metrics ──────────────────────────────────

const activeBots = computed(() =>
  botsStore.bots.filter((b: BotSummary) => b.state !== 'IDLE').length
)

// Bot list filtered by selected state tile
const filteredBots = computed<BotSummary[]>(() => {
  const f = stateFilter.value
  if (f === 'ALL') return botsStore.bots
  return botsStore.bots.filter((b: BotSummary) => {
    if (f === 'ENTERING') {
      return b.state === 'ENTERING' || b.state === 'PAUSED_ENTERING'
    }
    if (f === 'EXITING') {
      return b.state === 'EXITING' || b.state === 'PAUSED_EXITING'
    }
    return b.state === f
  })
})

// 1. Total PnL
const totalPnl = computed(() => positionStats.value?.total_net_pnl ?? 0)
const closedCount = computed(() => positionStats.value?.closed_positions ?? 0)

// 2. Point Factor (points per $100K volume)
const totalPoints = computed(() =>
  allPoints.value.reduce((s: number, p: PointsRecord) => s + p.points, 0)
)
const totalVolume = computed(() => {
  if (!summaryAll.value?.fills) return 0
  return summaryAll.value.fills.reduce((s: number, f: any) => s + (f.total_value || 0), 0)
})
const pointFactor = computed(() => {
  if (totalVolume.value <= 0) return 0
  return (totalPoints.value / totalVolume.value) * 100_000
})

// 3. Most Traded (top 3 tokens by volume)
const mostTraded = computed(() => {
  if (!summaryByToken.value?.fills) return []
  const tokenMap: Record<string, number> = {}
  for (const f of summaryByToken.value.fills) {
    const tok = (f as any).token || 'unknown'
    tokenMap[tok] = (tokenMap[tok] || 0) + (f.total_value || 0)
  }
  const entries: Array<[string, number]> = []
  for (const key of Object.keys(tokenMap)) entries.push([key, tokenMap[key]])
  return entries
    .sort((a: [string, number], b: [string, number]) => b[1] - a[1])
    .slice(0, 3)
    .map((e: [string, number]) => ({ token: e[0], volume: e[1] }))
})

// 5. Paid Fees
const totalFees = computed(() => positionStats.value?.total_fees ?? 0)

// 6. Paid Funding
const totalFunding = computed(() => positionStats.value?.total_funding ?? 0)

// 7. Average Hold Time
const avgHoldMs = computed(() => {
  const closed = closedPositions.value.filter((p: JournalPosition) => p.status === 'CLOSED')
  if (closed.length === 0) return 0
  const sum = closed.reduce((s: number, p: JournalPosition) => s + (p.duration_ms || 0), 0)
  return sum / closed.length
})

// 8. Delta Neutral Factor
const deltaNeutralFactor = computed(() => {
  const closed = closedPositions.value.filter((p: JournalPosition) => p.status === 'CLOSED')
  if (closed.length === 0) return null

  // Group by token
  const byToken: Record<string, { longs: JournalPosition[]; shorts: JournalPosition[] }> = {}
  for (const p of closed) {
    if (!byToken[p.token]) byToken[p.token] = { longs: [], shorts: [] }
    if (p.side === 'LONG') byToken[p.token].longs.push(p)
    else byToken[p.token].shorts.push(p)
  }

  const factors: number[] = []

  for (const tok of Object.keys(byToken)) {
    const { longs, shorts } = byToken[tok]
    // Match pairs by overlapping time windows
    const usedShorts: Record<number, boolean> = {}
    for (const long of longs) {
      const longOpen = long.opened_at
      const longClose = long.closed_at || Date.now()
      for (let si = 0; si < shorts.length; si++) {
        if (usedShorts[si]) continue
        const short = shorts[si]
        const shortOpen = short.opened_at
        const shortClose = short.closed_at || Date.now()
        // Check overlap
        if (longOpen <= shortClose && shortOpen <= longClose) {
          usedShorts[si] = true
          const denom = Math.max(Math.abs(long.net_pnl), Math.abs(short.net_pnl))
          if (denom > 0) {
            factors.push(((long.net_pnl + short.net_pnl) / denom) * 100)
          }
          break
        }
      }
    }
  }

  if (factors.length === 0) return null
  return factors.reduce((s, f) => s + f, 0) / factors.length
})

// ── Formatters ────────────────────────────────────────

function formatUsd(n: number): string {
  const abs = Math.abs(n)
  const sign = n < 0 ? '-' : ''
  if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(2)}M`
  if (abs >= 1_000) return `${sign}$${(abs / 1_000).toFixed(1)}K`
  return `${sign}$${abs.toFixed(2)}`
}

function formatDuration(ms: number): string {
  if (ms <= 0) return '—'
  const totalMin = Math.round(ms / 60_000)
  if (totalMin < 60) return `${totalMin}m`
  const h = Math.floor(totalMin / 60)
  const m = totalMin % 60
  if (h >= 24) {
    const d = Math.floor(h / 24)
    const rh = h % 24
    return rh > 0 ? `${d}d ${rh}h` : `${d}d`
  }
  return m > 0 ? `${h}h ${m}m` : `${h}h`
}

// ── Data loading ──────────────────────────────────────

onMounted(async () => {
  await Promise.allSettled([
    botsStore.load(),
    accountStore.loadPositions(),
    fetchExchanges().then(ex => { exchanges.value = ex }).catch(() => {}),
    loadAnalytics(),
  ])
  pollInterval = setInterval(() => {
    botsStore.load()
    accountStore.loadPositions()
  }, 5000)
  analyticsPollInterval = setInterval(() => {
    loadAnalytics()
  }, 60000)
})

onUnmounted(() => {
  if (pollInterval) clearInterval(pollInterval)
  if (analyticsPollInterval) clearInterval(analyticsPollInterval)
})

async function loadAnalytics() {
  try {
    const [pointsResp, exchSummary, tokenSummary, posResp] = await Promise.all([
      fetchJournalPoints(),
      fetchJournalSummary({ group_by: 'exchange' }),
      fetchJournalSummary({ group_by: 'token' }),
      fetchJournalPositions({ status: 'closed' }),
    ])
    allPoints.value = pointsResp.data
    summaryAll.value = exchSummary
    summaryByToken.value = tokenSummary
    closedPositions.value = posResp.positions
    positionStats.value = posResp.stats
  } catch {
    // widgets show "—" when data unavailable
  }
}

// ── Bot actions ───────────────────────────────────────

async function handleStart(botId: string) {
  actionLoading.value[botId] = 'start'
  try { await botsStore.start(botId) }
  catch { /* error handled in store */ }
  finally { actionLoading.value[botId] = null }
}

async function handleStop(botId: string) {
  actionLoading.value[botId] = 'stop'
  try { await botsStore.stop(botId) }
  catch { /* error handled in store */ }
  finally { actionLoading.value[botId] = null }
}

async function handleKill(botId: string) {
  actionLoading.value[botId] = 'kill'
  try { await botsStore.kill(botId) }
  catch { /* error handled in store */ }
  finally { actionLoading.value[botId] = null }
}

async function handleDelete(botId: string) {
  if (!confirm(`Delete bot "${botId}"?`)) return
  actionLoading.value[botId] = 'delete'
  try { await botsStore.remove(botId) }
  catch { /* error handled in store */ }
  finally { actionLoading.value[botId] = null }
}

async function handleCreate(req: BotCreateRequest) {
  try {
    await botsStore.create(req)
    showCreateModal.value = false
  } catch {
    /* error shown in modal */
  }
}

function handleNavigate(botId: string) {
  router.push({ name: 'bot-detail', params: { botId } })
}

// ── Filter helpers ────────────────────────────────────

function handleFilterChange(filter: BotStateFilter) {
  stateFilter.value = filter
}

const filterEmptyMessage = computed(() => {
  if (botsStore.bots.length === 0) return 'No bots configured yet.'
  if (filteredBots.value.length === 0) {
    const f = stateFilter.value
    if (f === 'ALL') return 'No bots configured yet.'
    return `No bots in ${f} state.`
  }
  return null
})
</script>

<template>
  <div :class="$style.dashboard">
    <!-- ── Hero: Bot State Tiles ── -->
    <BotStateTiles
      :bots="botsStore.bots"
      :active-filter="stateFilter"
      @filter-change="handleFilterChange"
      @add-bot="showCreateModal = true"
    />

    <!-- ── Main: Bot list (left) + KPI stack (right) ── -->
    <div :class="$style.mainGrid">
      <!-- Bot list panel -->
      <div :class="$style.botsPanel">
        <div :class="$style.botsPanelHeader">
          <div :class="$style.headerLeft">
            <Typography size="text-lg" weight="semibold">Bots</Typography>
            <Typography
              v-if="stateFilter !== 'ALL'"
              size="text-xs"
              color="tertiary"
              :class="$style.filterBadge"
            >
              filtered: {{ stateFilter }}
            </Typography>
          </div>
          <Typography size="text-xs" color="tertiary">
            {{ filteredBots.length }} of {{ botsStore.bots.length }}
          </Typography>
        </div>

        <div v-if="botsStore.error" :class="$style.error">
          <Typography size="text-sm" color="error">{{ botsStore.error }}</Typography>
        </div>

        <div v-if="botsStore.loading && !botsStore.bots.length" :class="$style.empty">
          <Typography size="text-sm" color="secondary">Loading bots...</Typography>
        </div>
        <div v-else-if="filterEmptyMessage" :class="$style.empty">
          <Typography size="text-sm" color="tertiary">{{ filterEmptyMessage }}</Typography>
          <button
            v-if="stateFilter !== 'ALL' && botsStore.bots.length > 0"
            type="button"
            :class="$style.clearFilterBtn"
            @click="stateFilter = 'ALL'"
          >
            Show all bots
          </button>
        </div>
        <div v-else :class="$style.botList">
          <BotMiniCard
            v-for="bot in filteredBots"
            :key="bot.bot_id"
            :bot="bot"
            :action-loading="actionLoading[bot.bot_id]"
            @start="handleStart"
            @stop="handleStop"
            @kill="handleKill"
            @delete="handleDelete"
            @navigate="handleNavigate"
          />
        </div>
      </div>

      <!-- KPI stack -->
      <div :class="$style.kpiStack">
        <DashboardStatCard
          title="Total PnL"
          :value="formatUsd(totalPnl)"
          :subtitle="`${closedCount} closed positions`"
          :color="totalPnl >= 0 ? 'success' : 'error'"
        />
        <DashboardStatCard
          title="Active Bots"
          :value="`${activeBots} / ${botsStore.bots.length}`"
          subtitle="running / total"
          :color="activeBots > 0 ? 'success' : 'secondary'"
        />
        <DashboardStatCard
          title="Avg Hold Time"
          :value="formatDuration(avgHoldMs)"
          :subtitle="`${closedCount} closed positions`"
        />
        <DashboardStatCard
          title="Paid Funding"
          :value="formatUsd(totalFunding)"
          :color="totalFunding >= 0 ? 'success' : 'error'"
        />
      </div>
    </div>

    <!-- ── Advanced Analytics (collapsible, default closed) ── -->
    <AdvancedAnalyticsSection
      :point-factor="pointFactor"
      :most-traded="mostTraded"
      :delta-neutral-factor="deltaNeutralFactor"
      :total-fees="totalFees"
      :closed-count="closedCount"
    />

    <!-- Create Modal -->
    <BotCreateModal
      :open="showCreateModal"
      @close="showCreateModal = false"
      @create="handleCreate"
    />
  </div>
</template>

<style module>
.dashboard {
  padding: 50px 40px;
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
  max-width: 1400px;
  margin: 0 auto;
}

.mainGrid {
  display: grid;
  grid-template-columns: minmax(0, 65fr) minmax(0, 35fr);
  gap: var(--space-5);
  align-items: stretch;
}

/* Bots panel */
.botsPanel {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 480px;
}

.botsPanelHeader {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
}

.headerLeft {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.filterBadge {
  text-transform: uppercase;
  letter-spacing: 0.04em;
  padding: 2px var(--space-2);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-md);
  background: var(--color-white-2);
}

.botList {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  max-height: 640px;
  overflow-y: auto;
}

.error {
  padding: var(--space-3) var(--space-4);
  margin: var(--space-3);
  background: var(--color-error-bg, rgba(220, 53, 69, 0.1));
  border: 1px solid var(--color-error-stroke, var(--color-error));
  border-radius: var(--radius-md);
}

.empty {
  padding: var(--space-10) var(--space-5);
  text-align: center;
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  align-items: center;
}

.clearFilterBtn {
  appearance: none;
  background: transparent;
  border: 1px solid var(--color-stroke-divider);
  color: var(--color-text-secondary);
  font: inherit;
  font-size: var(--text-sm);
  padding: var(--space-2) var(--space-4);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.clearFilterBtn:hover {
  background: var(--color-white-4);
  border-color: var(--color-stroke-primary);
}

/* KPI stack — 4 cards vertical */
.kpiStack {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.kpiStack > * {
  /* Stretch each card to fill column */
  flex: 1 1 0;
}

@media (max-width: 900px) {
  .mainGrid {
    grid-template-columns: 1fr;
  }
  .kpiStack {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
  }
  .dashboard {
    padding: 24px 16px;
  }
}

@media (max-width: 480px) {
  .kpiStack {
    grid-template-columns: 1fr;
  }
}
</style>
