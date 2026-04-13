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
import Button from '@/components/ui/Button.vue'
import DashboardStatCard from '@/components/dashboard/DashboardStatCard.vue'
import PointsEfficiencyWidget from '@/components/dashboard/PointsEfficiencyWidget.vue'
import BotMiniCard from '@/components/dashboard/BotMiniCard.vue'
import BotCreateModal from '@/components/dashboard/BotCreateModal.vue'
import FundingWidget from '@/components/dashboard/FundingWidget.vue'
import type { BotCreateRequest } from '@/types/bot'
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
  botsStore.bots.filter((b: any) => b.state !== 'IDLE').length
)

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

function formatVolume(n: number): string {
  if (n >= 1_000_000) return '$' + (n / 1_000_000).toFixed(2) + 'M'
  if (n >= 1_000) return '$' + (n / 1_000).toFixed(1) + 'K'
  return '$' + n.toFixed(0)
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
</script>

<template>
  <div :class="$style.dashboard">
    <!-- ── Row 1: 4 Stat Cards ── -->
    <div :class="$style.statsGrid">
      <DashboardStatCard
        title="Total PnL"
        :value="formatUsd(totalPnl)"
        :subtitle="`${closedCount} closed positions`"
        :color="totalPnl >= 0 ? 'success' : 'error'"
      />
      <DashboardStatCard
        title="Point Factor"
        :value="pointFactor > 0 ? pointFactor.toFixed(1) : '—'"
        subtitle="points per $100K volume"
      />
      <DashboardStatCard
        title="Active Bots"
        :value="`${activeBots} / ${botsStore.bots.length}`"
        subtitle="running / total"
        :color="activeBots > 0 ? 'success' : 'secondary'"
      />
      <div :class="$style.mostTradedCard">
        <Typography size="text-xs" color="tertiary" :class="$style.cardTitle">MOST TRADED</Typography>
        <div v-if="mostTraded.length" :class="$style.tokenList">
          <div v-for="(t, i) in mostTraded" :key="t.token" :class="$style.tokenRow">
            <Typography size="text-sm" weight="semibold">{{ i + 1 }}. {{ t.token }}</Typography>
            <Typography size="text-xs" color="tertiary">{{ formatVolume(t.volume) }}</Typography>
          </div>
        </div>
        <Typography v-else size="text-sm" color="tertiary" :class="$style.noData">No trades yet</Typography>
      </div>
    </div>

    <!-- ── Row 2: 4 Stat Cards ── -->
    <div :class="$style.statsGrid">
      <DashboardStatCard
        title="Paid Fees"
        :value="formatUsd(Math.abs(totalFees))"
        :subtitle="`${closedCount} positions`"
        color="error"
      />
      <DashboardStatCard
        title="Paid Funding"
        :value="formatUsd(totalFunding)"
        :color="totalFunding >= 0 ? 'success' : 'error'"
      />
      <DashboardStatCard
        title="Avg Hold Time"
        :value="formatDuration(avgHoldMs)"
        :subtitle="`${closedCount} closed positions`"
      />
      <DashboardStatCard
        title="Delta Neutral Factor"
        :value="deltaNeutralFactor != null ? deltaNeutralFactor.toFixed(1) + '%' : '—'"
        :gauge="deltaNeutralFactor"
      />
    </div>

    <!-- ── Row 3: Points & Efficiency + Bots ── -->
    <div :class="$style.mainGrid">
      <PointsEfficiencyWidget />

      <div :class="$style.botsPanel">
        <div :class="$style.botsPanelHeader">
          <Typography size="text-lg" weight="semibold">Bots</Typography>
          <Button variant="outline" size="sm" @click="showCreateModal = true">
            <template #prefix>+</template>
            Add Bot
          </Button>
        </div>

        <div v-if="botsStore.error" :class="$style.error">
          <Typography size="text-sm" color="error">{{ botsStore.error }}</Typography>
        </div>

        <div v-if="botsStore.loading && !botsStore.bots.length" :class="$style.empty">
          <Typography size="text-sm" color="secondary">Loading bots...</Typography>
        </div>
        <div v-else-if="!botsStore.bots.length" :class="$style.empty">
          <Typography size="text-sm" color="tertiary">No bots configured yet.</Typography>
        </div>
        <div v-else :class="$style.botList">
          <BotMiniCard
            v-for="bot in botsStore.bots"
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
    </div>

    <!-- ── Row 4: Funding Widget ── -->
    <FundingWidget />

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

.statsGrid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--space-4);
}

.mainGrid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-6);
  align-items: start;
}

/* Most Traded — custom card (not DashboardStatCard) */
.mostTradedCard {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: var(--space-4) var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-height: 120px;
}

.cardTitle {
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.tokenList {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-top: auto;
}

.tokenRow {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.noData {
  margin-top: auto;
}

/* Bots panel */
.botsPanel {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.botsPanelHeader {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
}

.botList {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3);
  max-height: 600px;
  overflow-y: auto;
}

.error {
  padding: var(--space-3) var(--space-4);
  margin: var(--space-3);
  background: var(--color-error-bg);
  border: 1px solid var(--color-error-stroke);
  border-radius: var(--radius-md);
}

.empty {
  padding: var(--space-10) var(--space-5);
  text-align: center;
}

@media (max-width: 900px) {
  .statsGrid {
    grid-template-columns: repeat(2, 1fr);
  }
  .mainGrid {
    grid-template-columns: 1fr;
  }
  .dashboard {
    padding: 24px 16px;
  }
}

@media (max-width: 480px) {
  .statsGrid {
    grid-template-columns: 1fr;
  }
}
</style>
