<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { fetchJournalPoints, fetchJournalSummary } from '@/lib/api'
import Typography from '@/components/ui/Typography.vue'
import type { PointsRecord, JournalSummary } from '@/types/journal'

const loading = ref(true)
const error = ref<string | null>(null)

const allPoints = ref<PointsRecord[]>([])
const summaryAll = ref<JournalSummary | null>(null)
const summary7d = ref<JournalSummary | null>(null)

const sevenDaysAgo = Date.now() - 7 * 24 * 60 * 60 * 1000

interface ExchangeEfficiency {
  exchange: string
  totalPoints: number
  totalVolume: number
  totalRatio: number
  points7d: number
  volume7d: number
  ratio7d: number
  trendUp: boolean | null
}

const exchangeData = computed<ExchangeEfficiency[]>(() => {
  if (!summaryAll.value) return []

  // Collect all exchanges from points + summary
  const exchanges = new Set<string>()
  for (const p of allPoints.value) exchanges.add(p.exchange)
  if (summaryAll.value?.fills) {
    for (const f of summaryAll.value.fills) {
      if (f.exchange) exchanges.add(f.exchange)
    }
  }

  const result: ExchangeEfficiency[] = []
  for (const ex of exchanges) {
    // Total points for this exchange
    const totalPoints = allPoints.value
      .filter(p => p.exchange === ex)
      .reduce((s, p) => s + p.points, 0)

    // 7d points: epochs whose end_date falls within last 7 days
    const points7d = allPoints.value
      .filter(p => p.exchange === ex && new Date(p.end_date).getTime() >= sevenDaysAgo)
      .reduce((s, p) => s + p.points, 0)

    // Total volume (sum BUY + SELL sides)
    const totalVolume = (summaryAll.value?.fills ?? [])
      .filter((f: any) => f.exchange === ex)
      .reduce((s: number, f: any) => s + (f.total_value || 0), 0)

    // 7d volume
    const volume7d = (summary7d.value?.fills ?? [])
      .filter((f: any) => f.exchange === ex)
      .reduce((s: number, f: any) => s + (f.total_value || 0), 0)

    const totalRatio = totalVolume > 0 ? totalPoints / totalVolume : 0
    const ratio7d = volume7d > 0 ? points7d / volume7d : 0
    const trendUp = totalRatio > 0 && ratio7d > 0 ? ratio7d > totalRatio : null

    result.push({ exchange: ex, totalPoints, totalVolume, totalRatio, points7d, volume7d, ratio7d, trendUp })
  }

  return result.sort((a, b) => b.totalPoints - a.totalPoints)
})

const totals = computed(() => {
  const totalPoints = exchangeData.value.reduce((s, e) => s + e.totalPoints, 0)
  const totalVolume = exchangeData.value.reduce((s, e) => s + e.totalVolume, 0)
  const points7d = exchangeData.value.reduce((s, e) => s + e.points7d, 0)
  const volume7d = exchangeData.value.reduce((s, e) => s + e.volume7d, 0)
  return { totalPoints, totalVolume, points7d, volume7d }
})

async function loadData() {
  loading.value = true
  error.value = null
  try {
    const [pointsResp, allSummary, weekSummary] = await Promise.all([
      fetchJournalPoints(),
      fetchJournalSummary({ group_by: 'exchange' }),
      fetchJournalSummary({ from: sevenDaysAgo, group_by: 'exchange' }),
    ])
    allPoints.value = pointsResp.data
    summaryAll.value = allSummary
    summary7d.value = weekSummary
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'Failed to load analytics'
  } finally {
    loading.value = false
  }
}

onMounted(loadData)

defineExpose({ refresh: loadData })

function formatNumber(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return n.toFixed(0)
}

function formatVolume(n: number): string {
  if (n >= 1_000_000) return '$' + (n / 1_000_000).toFixed(2) + 'M'
  if (n >= 1_000) return '$' + (n / 1_000).toFixed(1) + 'K'
  return '$' + n.toFixed(0)
}

function formatRatio(n: number): string {
  if (n === 0) return '—'
  return n.toFixed(4)
}

function exchangeColor(exchange: string): string {
  const map: Record<string, string> = {
    extended: 'var(--color-extended-brand)',
    grvt: 'var(--color-grvt-brand)',
    variational: 'var(--color-variational-brand)',
    nado: 'var(--color-nado-brand)',
  }
  return map[exchange] || 'var(--color-text-secondary)'
}
</script>

<template>
  <div :class="$style.widget">
    <div :class="$style.widgetHeader">
      <Typography size="text-lg" weight="semibold">Points & Efficiency</Typography>
      <button :class="$style.refreshBtn" @click="loadData" :disabled="loading">
        {{ loading ? '...' : '↻' }}
      </button>
    </div>

    <div v-if="error" :class="$style.errorMsg">
      <Typography size="text-xs" color="error">{{ error }}</Typography>
    </div>

    <div v-if="loading && !exchangeData.length" :class="$style.empty">
      <Typography size="text-sm" color="secondary">Loading analytics...</Typography>
    </div>

    <template v-else>
      <!-- Per-exchange cards -->
      <div :class="$style.exchangeList">
        <div
          v-for="ex in exchangeData"
          :key="ex.exchange"
          :class="$style.exchangeCard"
        >
          <div :class="$style.ecHeader">
            <Typography size="text-md" weight="semibold" :style="{ color: exchangeColor(ex.exchange) }">
              {{ ex.exchange.charAt(0).toUpperCase() + ex.exchange.slice(1) }}
            </Typography>
            <span
              v-if="ex.trendUp !== null"
              :class="[$style.trend, ex.trendUp ? $style.trendUp : $style.trendDown]"
            >{{ ex.trendUp ? '▲' : '▼' }}</span>
          </div>

          <!-- Stats grid: 2 columns (All Time | Last 7d) -->
          <div :class="$style.statsGrid">
            <div :class="$style.colLabel" />
            <Typography :class="$style.colHeader" size="text-xs" color="tertiary">All Time</Typography>
            <Typography :class="$style.colHeader" size="text-xs" color="tertiary">Last 7d</Typography>

            <Typography :class="$style.rowLabel" size="text-xs" color="tertiary">Points</Typography>
            <Typography size="text-sm" weight="medium">{{ formatNumber(ex.totalPoints) }}</Typography>
            <Typography size="text-sm" weight="medium">{{ formatNumber(ex.points7d) }}</Typography>

            <Typography :class="$style.rowLabel" size="text-xs" color="tertiary">Volume</Typography>
            <Typography size="text-sm" color="secondary">{{ formatVolume(ex.totalVolume) }}</Typography>
            <Typography size="text-sm" color="secondary">{{ formatVolume(ex.volume7d) }}</Typography>

            <Typography :class="$style.rowLabel" size="text-xs" color="tertiary">Pts/Vol</Typography>
            <Typography size="text-sm" weight="semibold" :color="ex.totalRatio > 0 ? 'primary' : 'tertiary'">
              {{ formatRatio(ex.totalRatio) }}
            </Typography>
            <Typography
              size="text-sm"
              weight="semibold"
              :color="ex.trendUp === true ? 'success' : ex.trendUp === false ? 'error' : 'tertiary'"
            >
              {{ formatRatio(ex.ratio7d) }}
            </Typography>
          </div>
        </div>
      </div>

      <!-- Totals footer -->
      <div v-if="exchangeData.length" :class="$style.totalsBar">
        <div :class="$style.totalItem">
          <Typography size="text-xs" color="tertiary">Total Points</Typography>
          <Typography size="text-md" weight="bold">{{ formatNumber(totals.totalPoints) }}</Typography>
        </div>
        <div :class="$style.totalItem">
          <Typography size="text-xs" color="tertiary">Total Volume</Typography>
          <Typography size="text-md" weight="bold">{{ formatVolume(totals.totalVolume) }}</Typography>
        </div>
        <div :class="$style.totalItem">
          <Typography size="text-xs" color="tertiary">7d Points</Typography>
          <Typography size="text-md" weight="bold">{{ formatNumber(totals.points7d) }}</Typography>
        </div>
        <div :class="$style.totalItem">
          <Typography size="text-xs" color="tertiary">7d Volume</Typography>
          <Typography size="text-md" weight="bold">{{ formatVolume(totals.volume7d) }}</Typography>
        </div>
      </div>

      <div v-if="!exchangeData.length && !loading" :class="$style.empty">
        <Typography size="text-sm" color="tertiary">No points or volume data yet.</Typography>
      </div>
    </template>
  </div>
</template>

<style module>
.widget {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.widgetHeader {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
}

.refreshBtn {
  background: none;
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-sm);
  color: var(--color-text-secondary);
  cursor: pointer;
  padding: 2px 8px;
  font-size: var(--text-sm);
  transition: all 0.15s ease;
}

.refreshBtn:hover {
  background: var(--color-white-10);
  color: var(--color-text-primary);
}

.refreshBtn:disabled {
  opacity: 0.5;
  cursor: default;
}

.errorMsg {
  padding: var(--space-2) var(--space-5);
}

.exchangeList {
  display: flex;
  flex-direction: column;
}

.exchangeCard {
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-stroke-divider);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.exchangeCard:last-child {
  border-bottom: none;
}

.ecHeader {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.trend {
  font-size: 10px;
  font-weight: 600;
}

.trendUp {
  color: var(--color-success);
}

.trendDown {
  color: var(--color-error);
}

.statsGrid {
  display: grid;
  grid-template-columns: 56px 1fr 1fr;
  gap: var(--space-1) var(--space-3);
  align-items: center;
}

.colLabel {
  /* empty top-left cell */
}

.colHeader {
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.rowLabel {
  text-align: right;
}

.totalsBar {
  display: flex;
  gap: var(--space-6);
  padding: var(--space-4) var(--space-5);
  border-top: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  flex-wrap: wrap;
}

.totalItem {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.empty {
  padding: var(--space-8) var(--space-5);
  text-align: center;
}
</style>
