<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch, nextTick } from 'vue'
import { createChart, LineSeries, type IChartApi, type ISeriesApi, type LineData, type Time, ColorType } from 'lightweight-charts'
import { fetchEquityHistory, fetchTradesHistory, fetchJournalOrders, fetchJournalFills, fetchJournalFunding, fetchJournalPoints, fetchJournalPositions, fetchJournalPairedTrades, fetchJournalSummary } from '@/lib/api'
import Typography from '@/components/ui/Typography.vue'
import type { EquitySnapshot, Trade } from '@/types/history'
import type { OrderRecord, FillRecord, FundingPayment, PointsRecord, JournalSummary, Position, PositionStats, PairedTrade, PairedTradeStats } from '@/types/journal'

// ── State ────────────────────────────────────────────
const activeTab = ref<'equity' | 'trades' | 'paired-trades' | 'positions' | 'orders' | 'fills' | 'funding' | 'points' | 'summary'>('equity')
const loading = ref(false)
const error = ref<string | null>(null)

// Equity chart
const equityData = ref<EquitySnapshot[]>([])
const chartContainer = ref<HTMLDivElement | null>(null)
let chart: IChartApi | null = null
let series: Map<string, ISeriesApi<'Line'>> = new Map()

const timeRange = ref<'1d' | '7d' | '30d' | 'all'>('7d')

// Trades
const trades = ref<Trade[]>([])
const tradesLoading = ref(false)

// Journal
const journalOrders = ref<OrderRecord[]>([])
const journalFills = ref<FillRecord[]>([])
const journalFunding = ref<FundingPayment[]>([])
const journalPoints = ref<PointsRecord[]>([])
const journalSummary = ref<JournalSummary | null>(null)
const journalPositions = ref<Position[]>([])
const journalPositionStats = ref<PositionStats | null>(null)
const positionStatusFilter = ref<'all' | 'open' | 'closed'>('all')

// Paired Trades
const pairedTrades = ref<PairedTrade[]>([])
const pairedTradeStats = ref<PairedTradeStats | null>(null)
const pairedTradesLoading = ref(false)
const pairedTradesRange = ref<'1d' | '7d' | '30d' | 'all'>('all')
const pairedTradesStatusFilter = ref<'all' | 'open' | 'closed'>('all')
const pairedTradesToken = ref<string>('')
const journalLoading = ref(false)
const journalExchange = ref<string>('')
const journalToken = ref<string>('')
const journalRange = ref<'1d' | '7d' | '30d' | 'all'>('7d')

// ── Helpers ──────────────────────────────────────────
function formatUsd(v: number) {
  const prefix = v >= 0 ? '$' : '-$'
  return `${prefix}${Math.abs(v).toFixed(2)}`
}

function formatDateTime(tsMs: number) {
  const d = new Date(tsMs)
  return d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' }) + ' ' +
    d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
}

function getFromTs(): number {
  const now = Date.now()
  switch (timeRange.value) {
    case '1d': return now - 24 * 60 * 60 * 1000
    case '7d': return now - 7 * 24 * 60 * 60 * 1000
    case '30d': return now - 30 * 24 * 60 * 60 * 1000
    case 'all': return 0
  }
}

const exchangeColors: Record<string, string> = {
  extended: '#3b82f6',
  grvt: '#f59e0b',
  variational: '#8b5cf6',
  nado: '#10b981',
}

// ── Chart ────────────────────────────────────────────
async function loadEquity() {
  loading.value = true
  error.value = null
  try {
    const from = getFromTs()
    const resp = await fetchEquityHistory({ from, limit: 5000 })
    equityData.value = resp.data
    renderChart()
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    loading.value = false
  }
}

function renderChart() {
  if (!chartContainer.value) return
  // Destroy old chart
  if (chart) {
    chart.remove()
    chart = null
    series.clear()
  }

  chart = createChart(chartContainer.value, {
    width: chartContainer.value.clientWidth,
    height: 400,
    layout: {
      background: { type: ColorType.Solid, color: 'transparent' },
      textColor: 'rgba(255,255,255,0.6)',
      fontFamily: 'Inter, system-ui, sans-serif',
      fontSize: 12,
    },
    grid: {
      vertLines: { color: 'rgba(255,255,255,0.04)' },
      horzLines: { color: 'rgba(255,255,255,0.04)' },
    },
    crosshair: {
      vertLine: { color: 'rgba(255,255,255,0.15)' },
      horzLine: { color: 'rgba(255,255,255,0.15)' },
    },
    timeScale: {
      timeVisible: true,
      borderColor: 'rgba(255,255,255,0.08)',
    },
    rightPriceScale: {
      borderColor: 'rgba(255,255,255,0.08)',
    },
  })

  // Group by exchange
  const byExchange = new Map<string, { time: Time; value: number }[]>()
  const totalMap = new Map<number, number>()

  for (const snap of equityData.value) {
    if (!snap.equity && snap.exchange !== 'total') continue
    const exch = snap.exchange
    if (!byExchange.has(exch)) byExchange.set(exch, [])
    const timeSec = Math.floor(snap.ts / 1000) as Time
    byExchange.get(exch)!.push({ time: timeSec, value: snap.equity })
    totalMap.set(snap.ts, (totalMap.get(snap.ts) || 0) + snap.equity)
  }

  // Add total series
  const totalData: { time: Time; value: number }[] = []
  for (const [ts, val] of [...totalMap.entries()].sort((a, b) => a[0] - b[0])) {
    totalData.push({ time: Math.floor(ts / 1000) as Time, value: val })
  }

  if (totalData.length > 0) {
    const totalSeries = chart.addSeries(LineSeries, {
      color: '#22c55e',
      lineWidth: 2,
      title: 'Total',
    })
    totalSeries.setData(totalData as LineData[])
    series.set('total', totalSeries)
  }

  // Per-exchange series
  for (const [exch, data] of byExchange) {
    if (!data.length) continue
    const s = chart.addSeries(LineSeries, {
      color: exchangeColors[exch] || '#6b7280',
      lineWidth: 1,
      title: exch,
    })
    s.setData(data.sort((a, b) => (a.time as number) - (b.time as number)) as LineData[])
    series.set(exch, s)
  }

  chart.timeScale().fitContent()
}

// ── Trades ───────────────────────────────────────────
async function loadTrades() {
  tradesLoading.value = true
  try {
    const resp = await fetchTradesHistory({ limit: 200 })
    trades.value = resp.data
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    tradesLoading.value = false
  }
}

// ── Lifecycle ────────────────────────────────────────
let resizeObserver: ResizeObserver | null = null

onMounted(async () => {
  await loadEquity()
  await loadTrades()

  // Resize handler
  await nextTick()
  if (chartContainer.value) {
    resizeObserver = new ResizeObserver(() => {
      if (chart && chartContainer.value) {
        chart.applyOptions({ width: chartContainer.value.clientWidth })
      }
    })
    resizeObserver.observe(chartContainer.value)
  }
})

onUnmounted(() => {
  if (chart) { chart.remove(); chart = null }
  if (resizeObserver) resizeObserver.disconnect()
})

function getJournalFromTs(): number {
  const now = Date.now()
  switch (journalRange.value) {
    case '1d': return now - 24 * 60 * 60 * 1000
    case '7d': return now - 7 * 24 * 60 * 60 * 1000
    case '30d': return now - 30 * 24 * 60 * 60 * 1000
    case 'all': return 0
  }
}

function getPairedFromTs(): number {
  const now = Date.now()
  switch (pairedTradesRange.value) {
    case '1d': return now - 24 * 60 * 60 * 1000
    case '7d': return now - 7 * 24 * 60 * 60 * 1000
    case '30d': return now - 30 * 24 * 60 * 60 * 1000
    case 'all': return 0
  }
}

async function loadPairedTrades() {
  pairedTradesLoading.value = true
  try {
    const params: Record<string, any> = { from: getPairedFromTs(), status: pairedTradesStatusFilter.value }
    if (pairedTradesToken.value) params.token = pairedTradesToken.value
    const resp = await fetchJournalPairedTrades(params)
    pairedTrades.value = resp.trades
    pairedTradeStats.value = resp.stats
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    pairedTradesLoading.value = false
  }
}

async function loadJournalPositions() {
  journalLoading.value = true
  try {
    const params: Record<string, any> = { from: getJournalFromTs(), status: positionStatusFilter.value }
    if (journalExchange.value) params.exchange = journalExchange.value
    if (journalToken.value) params.token = journalToken.value
    const resp = await fetchJournalPositions(params)
    journalPositions.value = resp.positions
    journalPositionStats.value = resp.stats
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    journalLoading.value = false
  }
}

async function loadJournalOrders() {
  journalLoading.value = true
  try {
    const params: Record<string, any> = { from: getJournalFromTs(), limit: 500 }
    if (journalExchange.value) params.exchange = journalExchange.value
    if (journalToken.value) params.token = journalToken.value
    const resp = await fetchJournalOrders(params)
    journalOrders.value = resp.data
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    journalLoading.value = false
  }
}

async function loadJournalFills() {
  journalLoading.value = true
  try {
    const params: Record<string, any> = { from: getJournalFromTs(), limit: 500 }
    if (journalExchange.value) params.exchange = journalExchange.value
    if (journalToken.value) params.token = journalToken.value
    const resp = await fetchJournalFills(params)
    journalFills.value = resp.data
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    journalLoading.value = false
  }
}

async function loadJournalFunding() {
  journalLoading.value = true
  try {
    const params: Record<string, any> = { from: getJournalFromTs(), limit: 500 }
    if (journalExchange.value) params.exchange = journalExchange.value
    if (journalToken.value) params.token = journalToken.value
    const resp = await fetchJournalFunding(params)
    journalFunding.value = resp.data
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    journalLoading.value = false
  }
}

async function loadJournalPoints() {
  journalLoading.value = true
  try {
    const params: Record<string, any> = {}
    if (journalExchange.value) params.exchange = journalExchange.value
    const resp = await fetchJournalPoints(params)
    journalPoints.value = resp.data
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    journalLoading.value = false
  }
}

async function loadJournalSummary() {
  journalLoading.value = true
  try {
    const resp = await fetchJournalSummary({ from: getJournalFromTs(), group_by: 'exchange' })
    journalSummary.value = resp
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e)
  } finally {
    journalLoading.value = false
  }
}

function formatRate(v: number) {
  return (v * 100).toFixed(6) + '%'
}

function formatDuration(ms: number): string {
  if (ms < 60000) return `${Math.round(ms / 1000)}s`
  if (ms < 3600000) return `${Math.round(ms / 60000)}m`
  if (ms < 86400000) {
    const h = Math.floor(ms / 3600000)
    const m = Math.round((ms % 3600000) / 60000)
    return `${h}h ${m}m`
  }
  const d = Math.floor(ms / 86400000)
  const h = Math.round((ms % 86400000) / 3600000)
  return `${d}d ${h}h`
}

function formatPct(v: number): string {
  return (v * 100).toFixed(1) + '%'
}

watch(timeRange, () => loadEquity())
watch(activeTab, (tab) => {
  if (tab === 'equity') nextTick(() => renderChart())
  else if (tab === 'paired-trades') loadPairedTrades()
  else if (tab === 'positions') loadJournalPositions()
  else if (tab === 'orders') loadJournalOrders()
  else if (tab === 'fills') loadJournalFills()
  else if (tab === 'funding') loadJournalFunding()
  else if (tab === 'points') loadJournalPoints()
  else if (tab === 'summary') loadJournalSummary()
})
watch(journalRange, () => {
  const tab = activeTab.value
  if (tab === 'positions') loadJournalPositions()
  else if (tab === 'orders') loadJournalOrders()
  else if (tab === 'fills') loadJournalFills()
  else if (tab === 'funding') loadJournalFunding()
  else if (tab === 'summary') loadJournalSummary()
})
watch(positionStatusFilter, () => {
  if (activeTab.value === 'positions') loadJournalPositions()
})
watch(pairedTradesRange, () => {
  if (activeTab.value === 'paired-trades') loadPairedTrades()
})
watch(pairedTradesStatusFilter, () => {
  if (activeTab.value === 'paired-trades') loadPairedTrades()
})
</script>

<template>
  <div :class="$style.page">
    <!-- Header -->
    <div :class="$style.header">
      <div>
        <Typography size="text-h4" weight="bold">History</Typography>
        <Typography size="text-sm" color="tertiary" style="margin-top: 4px">
          Equity curves &amp; trade journal
        </Typography>
      </div>
    </div>

    <!-- Tabs -->
    <div :class="$style.tabs">
      <button
        :class="[$style.tab, activeTab === 'equity' && $style.tabActive]"
        @click="activeTab = 'equity'"
      >Equity Chart</button>
      <button
        :class="[$style.tab, activeTab === 'paired-trades' && $style.tabActive]"
        @click="activeTab = 'paired-trades'"
      >Trades</button>
      <button
        :class="[$style.tab, activeTab === 'trades' && $style.tabActive]"
        @click="activeTab = 'trades'"
      >Trade Journal</button>
      <button
        :class="[$style.tab, activeTab === 'positions' && $style.tabActive]"
        @click="activeTab = 'positions'"
      >Positions</button>
      <button
        :class="[$style.tab, activeTab === 'orders' && $style.tabActive]"
        @click="activeTab = 'orders'"
      >Orders</button>
      <button
        :class="[$style.tab, activeTab === 'fills' && $style.tabActive]"
        @click="activeTab = 'fills'"
      >Fills</button>
      <button
        :class="[$style.tab, activeTab === 'funding' && $style.tabActive]"
        @click="activeTab = 'funding'"
      >Funding</button>
      <button
        :class="[$style.tab, activeTab === 'points' && $style.tabActive]"
        @click="activeTab = 'points'"
      >Points</button>
      <button
        :class="[$style.tab, activeTab === 'summary' && $style.tabActive]"
        @click="activeTab = 'summary'"
      >Summary</button>
    </div>

    <!-- Error -->
    <div v-if="error" :class="$style.error">
      <Typography size="text-sm" color="error">{{ error }}</Typography>
    </div>

    <!-- ── Equity Chart Tab ───────────────────────── -->
    <template v-if="activeTab === 'equity'">
      <!-- Time range selector -->
      <div :class="$style.rangeBar">
        <button
          v-for="r in (['1d', '7d', '30d', 'all'] as const)"
          :key="r"
          :class="[$style.rangeBtn, timeRange === r && $style.rangeBtnActive]"
          @click="timeRange = r"
        >{{ r === 'all' ? 'All' : r.toUpperCase() }}</button>
        <button :class="$style.refreshBtn" @click="loadEquity" :disabled="loading">
          {{ loading ? 'Loading...' : 'Refresh' }}
        </button>
      </div>

      <div :class="$style.chartCard">
        <div v-if="loading && !equityData.length" :class="$style.chartPlaceholder">
          <Typography color="secondary">Loading equity data...</Typography>
        </div>
        <div v-else-if="!equityData.length" :class="$style.chartPlaceholder">
          <Typography color="tertiary">No equity data yet. Snapshots are recorded every 5 minutes.</Typography>
        </div>
        <div ref="chartContainer" :class="$style.chartContainer" />
      </div>

      <!-- Legend -->
      <div v-if="equityData.length" :class="$style.legend">
        <div :class="$style.legendItem">
          <span :class="$style.legendDot" style="background: #22c55e" />
          <Typography size="text-xs" color="secondary">Total</Typography>
        </div>
        <div v-for="(color, exch) in exchangeColors" :key="exch" :class="$style.legendItem">
          <span :class="$style.legendDot" :style="{ background: color }" />
          <Typography size="text-xs" color="secondary">{{ exch }}</Typography>
        </div>
      </div>
    </template>

    <!-- ── Paired Trades Tab ─────────────────────── -->
    <template v-if="activeTab === 'paired-trades'">
      <div :class="$style.rangeBar">
        <button
          v-for="r in (['1d', '7d', '30d', 'all'] as const)"
          :key="r"
          :class="[$style.rangeBtn, pairedTradesRange === r && $style.rangeBtnActive]"
          @click="pairedTradesRange = r"
        >{{ r === 'all' ? 'All' : r.toUpperCase() }}</button>
        <select v-model="pairedTradesStatusFilter" :class="$style.filterInput">
          <option value="all">All</option>
          <option value="open">Open</option>
          <option value="closed">Closed</option>
        </select>
        <input
          v-model="pairedTradesToken"
          :class="$style.filterInput"
          placeholder="Token"
          @change="loadPairedTrades()"
        />
      </div>

      <!-- Stats Cards -->
      <div v-if="pairedTradeStats && !pairedTradesLoading" :class="$style.posStatsBar">
        <div :class="$style.posStatCard">
          <Typography size="text-xs" color="tertiary">Trades</Typography>
          <Typography size="text-md" weight="bold">{{ pairedTradeStats.total_trades }}</Typography>
          <Typography size="text-xs" color="secondary">{{ pairedTradeStats.open_trades }} open</Typography>
        </div>
        <div :class="$style.posStatCard">
          <Typography size="text-xs" color="tertiary">Net PnL</Typography>
          <Typography size="text-md" weight="bold" :color="pairedTradeStats.total_net_pnl >= 0 ? 'success' : 'error'">
            {{ formatUsd(pairedTradeStats.total_net_pnl) }}
          </Typography>
        </div>
        <div :class="$style.posStatCard">
          <Typography size="text-xs" color="tertiary">Realized PnL</Typography>
          <Typography size="text-md" weight="bold" :color="pairedTradeStats.total_realized_pnl >= 0 ? 'success' : 'error'">
            {{ formatUsd(pairedTradeStats.total_realized_pnl) }}
          </Typography>
        </div>
        <div :class="$style.posStatCard">
          <Typography size="text-xs" color="tertiary">Fees</Typography>
          <Typography size="text-md" weight="bold" color="error">{{ formatUsd(pairedTradeStats.total_fees) }}</Typography>
        </div>
        <div :class="$style.posStatCard">
          <Typography size="text-xs" color="tertiary">Funding</Typography>
          <Typography size="text-md" weight="bold" :color="pairedTradeStats.total_funding >= 0 ? 'success' : 'error'">
            {{ formatUsd(pairedTradeStats.total_funding) }}
          </Typography>
        </div>
        <div :class="$style.posStatCard">
          <Typography size="text-xs" color="tertiary">Win Rate</Typography>
          <Typography size="text-md" weight="bold">{{ formatPct(pairedTradeStats.win_rate) }}</Typography>
          <Typography size="text-xs" color="secondary">{{ pairedTradeStats.wins }}W / {{ pairedTradeStats.losses }}L</Typography>
        </div>
      </div>

      <div v-if="pairedTradesLoading && !pairedTrades.length" :class="$style.empty">
        <Typography color="secondary">Loading trades...</Typography>
      </div>

      <div v-else-if="!pairedTrades.length" :class="$style.empty">
        <Typography color="tertiary">No paired trades found. Trades are computed from fill history.</Typography>
      </div>

      <!-- Trade Cards -->
      <div v-else :class="$style.ptGrid">
        <div
          v-for="trade in pairedTrades"
          :key="trade.id"
          :class="$style.ptCard"
        >
          <div :class="$style.ptHeader">
            <Typography size="text-lg" weight="bold">{{ trade.token }}</Typography>
            <span :class="[$style.badge, trade.status === 'OPEN' ? $style.badgeOpen : $style.badgeDefault]">
              {{ trade.status }}
            </span>
          </div>

          <!-- Legs table -->
          <div :class="$style.tableCard" style="border: none; background: transparent;">
            <table :class="$style.table">
              <thead>
                <tr>
                  <th>Side</th>
                  <th>Exchange</th>
                  <th>Size</th>
                  <th>Entry</th>
                  <th>Exit</th>
                  <th>PnL</th>
                  <th>Fees</th>
                  <th>Funding</th>
                  <th>Net PnL</th>
                </tr>
              </thead>
              <tbody>
                <tr v-if="trade.long">
                  <td><span :class="[$style.badge, $style.badgeSuccess]">LONG</span></td>
                  <td><Typography size="text-sm" color="secondary">{{ trade.long.exchange }}</Typography></td>
                  <td><Typography size="text-sm">{{ trade.long.entry_qty.toFixed(4) }}</Typography></td>
                  <td><Typography size="text-sm">{{ formatUsd(trade.long.entry_price) }}</Typography></td>
                  <td><Typography size="text-sm">{{ trade.long.exit_price ? formatUsd(trade.long.exit_price) : '—' }}</Typography></td>
                  <td><Typography size="text-sm" :color="trade.long.realized_pnl >= 0 ? 'success' : 'error'">{{ formatUsd(trade.long.realized_pnl) }}</Typography></td>
                  <td><Typography size="text-sm" color="error">{{ formatUsd(trade.long.total_fees) }}</Typography></td>
                  <td><Typography size="text-sm" :color="trade.long.total_funding >= 0 ? 'success' : 'error'">{{ formatUsd(trade.long.total_funding) }}</Typography></td>
                  <td><Typography size="text-sm" weight="semibold" :color="trade.long.net_pnl >= 0 ? 'success' : 'error'">{{ formatUsd(trade.long.net_pnl) }}</Typography></td>
                </tr>
                <tr v-if="trade.short">
                  <td><span :class="[$style.badge, $style.badgeError]">SHORT</span></td>
                  <td><Typography size="text-sm" color="secondary">{{ trade.short.exchange }}</Typography></td>
                  <td><Typography size="text-sm">{{ trade.short.entry_qty.toFixed(4) }}</Typography></td>
                  <td><Typography size="text-sm">{{ formatUsd(trade.short.entry_price) }}</Typography></td>
                  <td><Typography size="text-sm">{{ trade.short.exit_price ? formatUsd(trade.short.exit_price) : '—' }}</Typography></td>
                  <td><Typography size="text-sm" :color="trade.short.realized_pnl >= 0 ? 'success' : 'error'">{{ formatUsd(trade.short.realized_pnl) }}</Typography></td>
                  <td><Typography size="text-sm" color="error">{{ formatUsd(trade.short.total_fees) }}</Typography></td>
                  <td><Typography size="text-sm" :color="trade.short.total_funding >= 0 ? 'success' : 'error'">{{ formatUsd(trade.short.total_funding) }}</Typography></td>
                  <td><Typography size="text-sm" weight="semibold" :color="trade.short.net_pnl >= 0 ? 'success' : 'error'">{{ formatUsd(trade.short.net_pnl) }}</Typography></td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- Combined footer -->
          <div :class="$style.ptFooter">
            <div :class="$style.ptStat">
              <Typography size="text-xs" color="tertiary">Opened</Typography>
              <Typography size="text-sm" color="secondary">{{ formatDateTime(trade.combined.opened_at) }}</Typography>
            </div>
            <div :class="$style.ptStat">
              <Typography size="text-xs" color="tertiary">Closed</Typography>
              <Typography size="text-sm" color="secondary">{{ trade.combined.closed_at ? formatDateTime(trade.combined.closed_at) : '—' }}</Typography>
            </div>
            <div :class="$style.ptStat">
              <Typography size="text-xs" color="tertiary">Duration</Typography>
              <Typography size="text-sm" color="secondary">{{ formatDuration(trade.combined.duration_ms) }}</Typography>
            </div>
            <div :class="$style.ptStat">
              <Typography size="text-xs" color="tertiary">Fills</Typography>
              <Typography size="text-sm" color="secondary">{{ trade.combined.fill_count }}</Typography>
            </div>
            <div :class="$style.ptStat">
              <Typography size="text-xs" color="tertiary">Combined PnL</Typography>
              <Typography size="text-sm" weight="semibold" :color="trade.combined.realized_pnl >= 0 ? 'success' : 'error'">{{ formatUsd(trade.combined.realized_pnl) }}</Typography>
            </div>
            <div :class="$style.ptStat">
              <Typography size="text-xs" color="tertiary">Total Fees</Typography>
              <Typography size="text-sm" weight="semibold" color="error">{{ formatUsd(trade.combined.total_fees) }}</Typography>
            </div>
            <div :class="$style.ptStat">
              <Typography size="text-xs" color="tertiary">Net Funding</Typography>
              <Typography size="text-sm" weight="semibold" :color="trade.combined.total_funding >= 0 ? 'success' : 'error'">{{ formatUsd(trade.combined.total_funding) }}</Typography>
            </div>
            <div :class="$style.ptStat">
              <Typography size="text-xs" color="tertiary">Net PnL</Typography>
              <Typography size="text-lg" weight="bold" :color="trade.combined.net_pnl >= 0 ? 'success' : 'error'">{{ formatUsd(trade.combined.net_pnl) }}</Typography>
            </div>
          </div>
        </div>
      </div>
    </template>

    <!-- ── Trade Journal Tab ──────────────────────── -->
    <template v-if="activeTab === 'trades'">
      <div :class="$style.tableCard">
        <div v-if="tradesLoading" :class="$style.empty">
          <Typography color="secondary">Loading trades...</Typography>
        </div>
        <div v-else-if="!trades.length" :class="$style.empty">
          <Typography color="tertiary">No closed trades recorded yet. Trades are detected when a position disappears between snapshots.</Typography>
        </div>
        <table v-else :class="$style.table">
          <thead>
            <tr>
              <th>Closed</th>
              <th>Token</th>
              <th>Exchange</th>
              <th>Side</th>
              <th>Size</th>
              <th>Entry</th>
              <th>Exit</th>
              <th>Realized PnL</th>
              <th>Funding</th>
              <th>Total PnL</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in trades" :key="t.id">
              <td>
                <Typography size="text-sm" color="secondary">{{ formatDateTime(t.closed_at) }}</Typography>
              </td>
              <td>
                <Typography size="text-sm" weight="semibold">{{ t.token }}</Typography>
              </td>
              <td>
                <Typography size="text-sm" color="secondary">{{ t.exchange }}</Typography>
              </td>
              <td>
                <Typography size="text-sm" :color="t.side === 'LONG' ? 'success' : 'error'">
                  {{ t.side }}
                </Typography>
              </td>
              <td>
                <Typography size="text-sm">{{ t.size.toFixed(4) }}</Typography>
              </td>
              <td>
                <Typography size="text-sm">{{ formatUsd(t.entry_price) }}</Typography>
              </td>
              <td>
                <Typography size="text-sm">{{ formatUsd(t.exit_price) }}</Typography>
              </td>
              <td>
                <Typography size="text-sm" :color="t.realized_pnl >= 0 ? 'success' : 'error'">
                  {{ formatUsd(t.realized_pnl) }}
                </Typography>
              </td>
              <td>
                <Typography size="text-sm" :color="t.cumulative_funding >= 0 ? 'success' : 'error'">
                  {{ formatUsd(t.cumulative_funding) }}
                </Typography>
              </td>
              <td>
                <Typography size="text-md" weight="semibold" :color="t.total_pnl >= 0 ? 'success' : 'error'">
                  {{ formatUsd(t.total_pnl) }}
                </Typography>
              </td>
            </tr>
          </tbody>
          <tfoot v-if="trades.length > 1">
            <tr>
              <td colspan="7">
                <Typography size="text-sm" weight="semibold" color="secondary">
                  {{ trades.length }} trades
                </Typography>
              </td>
              <td>
                <Typography
                  size="text-sm" weight="semibold"
                  :color="trades.reduce((s, t) => s + t.realized_pnl, 0) >= 0 ? 'success' : 'error'"
                >{{ formatUsd(trades.reduce((s, t) => s + t.realized_pnl, 0)) }}</Typography>
              </td>
              <td>
                <Typography
                  size="text-sm" weight="semibold"
                  :color="trades.reduce((s, t) => s + t.cumulative_funding, 0) >= 0 ? 'success' : 'error'"
                >{{ formatUsd(trades.reduce((s, t) => s + t.cumulative_funding, 0)) }}</Typography>
              </td>
              <td>
                <Typography
                  size="text-md" weight="bold"
                  :color="trades.reduce((s, t) => s + t.total_pnl, 0) >= 0 ? 'success' : 'error'"
                >{{ formatUsd(trades.reduce((s, t) => s + t.total_pnl, 0)) }}</Typography>
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </template>

    <!-- ── Positions Tab ──────────────────────────── -->
    <template v-if="activeTab === 'positions'">
      <div :class="$style.rangeBar">
        <button
          v-for="r in (['1d', '7d', '30d', 'all'] as const)"
          :key="r"
          :class="[$style.rangeBtn, journalRange === r && $style.rangeBtnActive]"
          @click="journalRange = r"
        >{{ r === 'all' ? 'All' : r.toUpperCase() }}</button>
        <select v-model="positionStatusFilter" :class="$style.filterInput">
          <option value="all">All</option>
          <option value="open">Open</option>
          <option value="closed">Closed</option>
        </select>
        <input
          v-model="journalExchange"
          :class="$style.filterInput"
          placeholder="Exchange"
          @change="loadJournalPositions()"
        />
        <input
          v-model="journalToken"
          :class="$style.filterInput"
          placeholder="Token"
          @change="loadJournalPositions()"
        />
      </div>

      <!-- Stats Cards -->
      <div v-if="journalPositionStats && !journalLoading" :class="$style.posStatsBar">
        <div :class="$style.posStatCard">
          <Typography size="text-xs" color="tertiary">Positions</Typography>
          <Typography size="text-md" weight="bold">{{ journalPositionStats.total_positions }}</Typography>
          <Typography size="text-xs" color="secondary">{{ journalPositionStats.open_positions }} open</Typography>
        </div>
        <div :class="$style.posStatCard">
          <Typography size="text-xs" color="tertiary">Net PnL</Typography>
          <Typography size="text-md" weight="bold" :color="journalPositionStats.total_net_pnl >= 0 ? 'success' : 'error'">
            {{ formatUsd(journalPositionStats.total_net_pnl) }}
          </Typography>
        </div>
        <div :class="$style.posStatCard">
          <Typography size="text-xs" color="tertiary">Realized PnL</Typography>
          <Typography size="text-md" weight="bold" :color="journalPositionStats.total_realized_pnl >= 0 ? 'success' : 'error'">
            {{ formatUsd(journalPositionStats.total_realized_pnl) }}
          </Typography>
        </div>
        <div :class="$style.posStatCard">
          <Typography size="text-xs" color="tertiary">Fees</Typography>
          <Typography size="text-md" weight="bold" color="error">{{ formatUsd(journalPositionStats.total_fees) }}</Typography>
        </div>
        <div :class="$style.posStatCard">
          <Typography size="text-xs" color="tertiary">Funding</Typography>
          <Typography size="text-md" weight="bold" :color="journalPositionStats.total_funding >= 0 ? 'success' : 'error'">
            {{ formatUsd(journalPositionStats.total_funding) }}
          </Typography>
        </div>
        <div :class="$style.posStatCard">
          <Typography size="text-xs" color="tertiary">Win Rate</Typography>
          <Typography size="text-md" weight="bold">
            {{ formatPct(journalPositionStats.win_rate) }}
          </Typography>
          <Typography size="text-xs" color="secondary">
            {{ journalPositionStats.wins }}W / {{ journalPositionStats.losses }}L
          </Typography>
        </div>
      </div>

      <div :class="$style.tableCard">
        <div v-if="journalLoading" :class="$style.empty">
          <Typography color="secondary">Loading positions...</Typography>
        </div>
        <div v-else-if="!journalPositions.length" :class="$style.empty">
          <Typography color="tertiary">No positions found. Positions are aggregated from fills.</Typography>
        </div>
        <table v-else :class="$style.table">
          <thead>
            <tr>
              <th>Status</th>
              <th>Opened</th>
              <th>Closed</th>
              <th>Exchange</th>
              <th>Token</th>
              <th>Side</th>
              <th>Size</th>
              <th>Entry</th>
              <th>Exit</th>
              <th>PnL</th>
              <th>Fees</th>
              <th>Funding</th>
              <th>Net PnL</th>
              <th>Duration</th>
              <th>Fills</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in journalPositions" :key="p.id">
              <td>
                <span :class="[$style.badge, p.status === 'OPEN' ? $style.badgeOpen : $style.badgeDefault]">
                  {{ p.status }}
                </span>
              </td>
              <td><Typography size="text-sm" color="secondary">{{ formatDateTime(p.opened_at) }}</Typography></td>
              <td><Typography size="text-sm" color="secondary">{{ p.closed_at ? formatDateTime(p.closed_at) : '—' }}</Typography></td>
              <td><Typography size="text-sm" color="secondary">{{ p.exchange }}</Typography></td>
              <td><Typography size="text-sm" weight="semibold">{{ p.token }}</Typography></td>
              <td>
                <Typography size="text-sm" :color="p.side === 'LONG' ? 'success' : 'error'">{{ p.side }}</Typography>
              </td>
              <td>
                <Typography size="text-sm">
                  {{ p.entry_qty.toFixed(4) }}
                  <span v-if="p.remaining_qty > 0" style="opacity: 0.5"> ({{ p.remaining_qty.toFixed(4) }} open)</span>
                </Typography>
              </td>
              <td><Typography size="text-sm">{{ formatUsd(p.entry_price) }}</Typography></td>
              <td><Typography size="text-sm">{{ p.exit_price ? formatUsd(p.exit_price) : '—' }}</Typography></td>
              <td>
                <Typography size="text-sm" :color="p.realized_pnl >= 0 ? 'success' : 'error'">
                  {{ formatUsd(p.realized_pnl) }}
                </Typography>
              </td>
              <td><Typography size="text-sm" color="error">{{ formatUsd(p.total_fees) }}</Typography></td>
              <td>
                <Typography size="text-sm" :color="p.total_funding >= 0 ? 'success' : 'error'">
                  {{ formatUsd(p.total_funding) }}
                </Typography>
              </td>
              <td>
                <Typography size="text-md" weight="semibold" :color="p.net_pnl >= 0 ? 'success' : 'error'">
                  {{ formatUsd(p.net_pnl) }}
                </Typography>
              </td>
              <td><Typography size="text-sm" color="secondary">{{ formatDuration(p.duration_ms) }}</Typography></td>
              <td><Typography size="text-sm" color="tertiary">{{ p.fill_count }}</Typography></td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- ── Journal Filter Bar (shared by orders/fills/funding) ── -->
    <div v-if="['orders','fills','funding','points','summary'].includes(activeTab)" :class="$style.rangeBar">
      <button
        v-for="r in (['1d', '7d', '30d', 'all'] as const)"
        :key="r"
        :class="[$style.rangeBtn, journalRange === r && $style.rangeBtnActive]"
        @click="journalRange = r"
      >{{ r === 'all' ? 'All' : r.toUpperCase() }}</button>
      <input
        v-model="journalExchange"
        :class="$style.filterInput"
        placeholder="Exchange filter"
        @change="activeTab === 'orders' ? loadJournalOrders() : activeTab === 'fills' ? loadJournalFills() : activeTab === 'funding' ? loadJournalFunding() : activeTab === 'points' ? loadJournalPoints() : loadJournalSummary()"
      />
      <input
        v-if="activeTab !== 'points' && activeTab !== 'summary'"
        v-model="journalToken"
        :class="$style.filterInput"
        placeholder="Token filter"
        @change="activeTab === 'orders' ? loadJournalOrders() : activeTab === 'fills' ? loadJournalFills() : loadJournalFunding()"
      />
    </div>

    <!-- ── Orders Tab ────────────────────────────────── -->
    <template v-if="activeTab === 'orders'">
      <div :class="$style.tableCard">
        <div v-if="journalLoading" :class="$style.empty">
          <Typography color="secondary">Loading orders...</Typography>
        </div>
        <div v-else-if="!journalOrders.length" :class="$style.empty">
          <Typography color="tertiary">No orders recorded yet. The journal collector syncs every 5 minutes.</Typography>
        </div>
        <table v-else :class="$style.table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Exchange</th>
              <th>Token</th>
              <th>Side</th>
              <th>Type</th>
              <th>Status</th>
              <th>Price</th>
              <th>Avg Price</th>
              <th>Qty</th>
              <th>Filled</th>
              <th>Fee</th>
              <th>Bot</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="o in journalOrders" :key="o.exchange_order_id">
              <td><Typography size="text-sm" color="secondary">{{ formatDateTime(o.created_at) }}</Typography></td>
              <td><Typography size="text-sm" color="secondary">{{ o.exchange }}</Typography></td>
              <td><Typography size="text-sm" weight="semibold">{{ o.token }}</Typography></td>
              <td><Typography size="text-sm" :color="o.side === 'BUY' ? 'success' : 'error'">{{ o.side }}</Typography></td>
              <td><Typography size="text-sm" color="secondary">{{ o.order_type }}</Typography></td>
              <td>
                <span :class="[$style.badge, o.status === 'FILLED' ? $style.badgeSuccess : o.status === 'CANCELLED' ? $style.badgeError : $style.badgeDefault]">
                  {{ o.status }}
                </span>
              </td>
              <td><Typography size="text-sm">{{ formatUsd(o.price) }}</Typography></td>
              <td><Typography size="text-sm">{{ o.average_price ? formatUsd(o.average_price) : '—' }}</Typography></td>
              <td><Typography size="text-sm">{{ o.qty.toFixed(4) }}</Typography></td>
              <td><Typography size="text-sm">{{ o.filled_qty.toFixed(4) }}</Typography></td>
              <td><Typography size="text-sm" color="secondary">{{ formatUsd(o.fee) }}</Typography></td>
              <td><Typography size="text-xs" color="tertiary">{{ o.bot_id || '—' }}</Typography></td>
            </tr>
          </tbody>
        </table>
      </div>
    </template>

    <!-- ── Fills Tab ─────────────────────────────────── -->
    <template v-if="activeTab === 'fills'">
      <div :class="$style.tableCard">
        <div v-if="journalLoading" :class="$style.empty">
          <Typography color="secondary">Loading fills...</Typography>
        </div>
        <div v-else-if="!journalFills.length" :class="$style.empty">
          <Typography color="tertiary">No fills recorded yet.</Typography>
        </div>
        <table v-else :class="$style.table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Exchange</th>
              <th>Token</th>
              <th>Side</th>
              <th>Price</th>
              <th>Qty</th>
              <th>Value</th>
              <th>Fee</th>
              <th>Taker</th>
              <th>Bot</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="f in journalFills" :key="f.exchange_fill_id">
              <td><Typography size="text-sm" color="secondary">{{ formatDateTime(f.created_at) }}</Typography></td>
              <td><Typography size="text-sm" color="secondary">{{ f.exchange }}</Typography></td>
              <td><Typography size="text-sm" weight="semibold">{{ f.token }}</Typography></td>
              <td><Typography size="text-sm" :color="f.side === 'BUY' ? 'success' : 'error'">{{ f.side }}</Typography></td>
              <td><Typography size="text-sm">{{ formatUsd(f.price) }}</Typography></td>
              <td><Typography size="text-sm">{{ f.qty.toFixed(4) }}</Typography></td>
              <td><Typography size="text-sm">{{ formatUsd(f.value) }}</Typography></td>
              <td><Typography size="text-sm" color="secondary">{{ formatUsd(f.fee) }}</Typography></td>
              <td>
                <span :class="[$style.badge, f.is_taker ? $style.badgeDefault : $style.badgeSuccess]">
                  {{ f.is_taker ? 'Taker' : 'Maker' }}
                </span>
              </td>
              <td><Typography size="text-xs" color="tertiary">{{ f.bot_id || '—' }}</Typography></td>
            </tr>
          </tbody>
          <tfoot v-if="journalFills.length > 1">
            <tr>
              <td colspan="6">
                <Typography size="text-sm" weight="semibold" color="secondary">
                  {{ journalFills.length }} fills
                </Typography>
              </td>
              <td>
                <Typography size="text-sm" weight="semibold">
                  {{ formatUsd(journalFills.reduce((s, f) => s + f.value, 0)) }}
                </Typography>
              </td>
              <td>
                <Typography size="text-sm" weight="semibold" color="error">
                  {{ formatUsd(journalFills.reduce((s, f) => s + f.fee, 0)) }}
                </Typography>
              </td>
              <td colspan="2" />
            </tr>
          </tfoot>
        </table>
      </div>
    </template>

    <!-- ── Funding Tab ───────────────────────────────── -->
    <template v-if="activeTab === 'funding'">
      <div :class="$style.tableCard">
        <div v-if="journalLoading" :class="$style.empty">
          <Typography color="secondary">Loading funding payments...</Typography>
        </div>
        <div v-else-if="!journalFunding.length" :class="$style.empty">
          <Typography color="tertiary">No funding payments recorded yet.</Typography>
        </div>
        <table v-else :class="$style.table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Exchange</th>
              <th>Token</th>
              <th>Side</th>
              <th>Size</th>
              <th>Rate</th>
              <th>Fee</th>
              <th>Mark Price</th>
              <th>Bot</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="fp in journalFunding" :key="fp.exchange_payment_id">
              <td><Typography size="text-sm" color="secondary">{{ formatDateTime(fp.paid_at) }}</Typography></td>
              <td><Typography size="text-sm" color="secondary">{{ fp.exchange }}</Typography></td>
              <td><Typography size="text-sm" weight="semibold">{{ fp.token }}</Typography></td>
              <td><Typography size="text-sm" :color="fp.side === 'LONG' ? 'success' : 'error'">{{ fp.side }}</Typography></td>
              <td><Typography size="text-sm">{{ fp.size.toFixed(4) }}</Typography></td>
              <td><Typography size="text-sm" color="secondary">{{ formatRate(fp.funding_rate) }}</Typography></td>
              <td>
                <Typography size="text-sm" :color="fp.funding_fee >= 0 ? 'success' : 'error'">
                  {{ formatUsd(fp.funding_fee) }}
                </Typography>
              </td>
              <td><Typography size="text-sm">{{ formatUsd(fp.mark_price) }}</Typography></td>
              <td><Typography size="text-xs" color="tertiary">{{ fp.bot_id || '—' }}</Typography></td>
            </tr>
          </tbody>
          <tfoot v-if="journalFunding.length > 1">
            <tr>
              <td colspan="6">
                <Typography size="text-sm" weight="semibold" color="secondary">
                  {{ journalFunding.length }} payments
                </Typography>
              </td>
              <td>
                <Typography size="text-sm" weight="bold" :color="journalFunding.reduce((s, f) => s + f.funding_fee, 0) >= 0 ? 'success' : 'error'">
                  {{ formatUsd(journalFunding.reduce((s, f) => s + f.funding_fee, 0)) }}
                </Typography>
              </td>
              <td colspan="2" />
            </tr>
          </tfoot>
        </table>
      </div>
    </template>

    <!-- ── Points Tab ────────────────────────────────── -->
    <template v-if="activeTab === 'points'">
      <div :class="$style.tableCard">
        <div v-if="journalLoading" :class="$style.empty">
          <Typography color="secondary">Loading points...</Typography>
        </div>
        <div v-else-if="!journalPoints.length" :class="$style.empty">
          <Typography color="tertiary">No points data recorded yet.</Typography>
        </div>
        <table v-else :class="$style.table">
          <thead>
            <tr>
              <th>Exchange</th>
              <th>Season</th>
              <th>Epoch</th>
              <th>Period</th>
              <th>Points</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in journalPoints" :key="`${p.exchange}-${p.season_id}-${p.epoch_id}`">
              <td><Typography size="text-sm" color="secondary">{{ p.exchange }}</Typography></td>
              <td><Typography size="text-sm">{{ p.season_id }}</Typography></td>
              <td><Typography size="text-sm">{{ p.epoch_id }}</Typography></td>
              <td><Typography size="text-sm" color="secondary">{{ p.start_date }} → {{ p.end_date }}</Typography></td>
              <td><Typography size="text-sm" weight="semibold">{{ p.points.toLocaleString() }}</Typography></td>
            </tr>
          </tbody>
          <tfoot v-if="journalPoints.length > 1">
            <tr>
              <td colspan="4">
                <Typography size="text-sm" weight="semibold" color="secondary">
                  {{ journalPoints.length }} entries
                </Typography>
              </td>
              <td>
                <Typography size="text-sm" weight="bold">
                  {{ journalPoints.reduce((s, p) => s + p.points, 0).toLocaleString() }}
                </Typography>
              </td>
            </tr>
          </tfoot>
        </table>
      </div>
    </template>

    <!-- ── Summary Tab ───────────────────────────────── -->
    <template v-if="activeTab === 'summary'">
      <div v-if="journalLoading" :class="$style.empty">
        <Typography color="secondary">Loading summary...</Typography>
      </div>
      <div v-else-if="!journalSummary" :class="$style.empty">
        <Typography color="tertiary">No data available for summary.</Typography>
      </div>
      <div v-else :class="$style.summaryGrid">
        <!-- Fills summary -->
        <div :class="$style.summaryCard">
          <Typography size="text-sm" weight="semibold" color="secondary" style="margin-bottom: 12px">Fill Volume</Typography>
          <div v-for="row in journalSummary.fills" :key="`${row.exchange}-${row.side}`" :class="$style.summaryRow">
            <Typography size="text-sm" weight="semibold">{{ row.exchange || '—' }}</Typography>
            <Typography size="text-sm" :color="row.side === 'BUY' ? 'success' : 'error'">{{ row.side }}</Typography>
            <Typography size="text-sm">{{ row.fill_count }} fills</Typography>
            <Typography size="text-sm" weight="semibold">{{ formatUsd(row.total_value) }}</Typography>
            <Typography size="text-sm" color="error">Fee: {{ formatUsd(row.total_fee) }}</Typography>
          </div>
          <div v-if="!journalSummary.fills.length" :class="$style.summaryEmpty">
            <Typography size="text-sm" color="tertiary">No fills in period</Typography>
          </div>
        </div>

        <!-- Funding summary -->
        <div :class="$style.summaryCard">
          <Typography size="text-sm" weight="semibold" color="secondary" style="margin-bottom: 12px">Funding Payments</Typography>
          <div v-for="row in journalSummary.funding" :key="row.exchange" :class="$style.summaryRow">
            <Typography size="text-sm" weight="semibold">{{ row.exchange || '—' }}</Typography>
            <Typography size="text-sm">{{ row.payment_count }} payments</Typography>
            <Typography size="text-sm" weight="bold" :color="row.total_funding >= 0 ? 'success' : 'error'">
              {{ formatUsd(row.total_funding) }}
            </Typography>
          </div>
          <div v-if="!journalSummary.funding.length" :class="$style.summaryEmpty">
            <Typography size="text-sm" color="tertiary">No funding in period</Typography>
          </div>
        </div>

        <!-- Orders summary -->
        <div :class="$style.summaryCard">
          <Typography size="text-sm" weight="semibold" color="secondary" style="margin-bottom: 12px">Orders</Typography>
          <div v-for="row in journalSummary.orders" :key="row.exchange" :class="$style.summaryRow">
            <Typography size="text-sm" weight="semibold">{{ row.exchange || '—' }}</Typography>
            <Typography size="text-sm">{{ row.order_count }} total</Typography>
            <Typography size="text-sm" color="success">{{ row.filled_count }} filled</Typography>
            <Typography size="text-sm" color="error">{{ row.cancelled_count }} cancelled</Typography>
          </div>
          <div v-if="!journalSummary.orders.length" :class="$style.summaryEmpty">
            <Typography size="text-sm" color="tertiary">No orders in period</Typography>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style module>
.page {
  padding: 50px 40px;
  max-width: 1600px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}

.tabs {
  display: flex;
  gap: var(--space-1);
  background: var(--color-white-4);
  border-radius: var(--radius-lg);
  padding: 3px;
  width: fit-content;
}

.tab {
  padding: var(--space-2) var(--space-5);
  border-radius: var(--radius-md);
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s ease;
}

.tab:hover {
  color: var(--color-text-primary);
}

.tabActive {
  background: var(--color-white-2);
  color: var(--color-text-primary);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
}

.error {
  padding: var(--space-3) var(--space-4);
  background: var(--color-error-bg);
  border: 1px solid var(--color-error-stroke);
  border-radius: var(--radius-md);
}

.rangeBar {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.rangeBtn {
  padding: var(--space-1) var(--space-3);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: transparent;
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
}

.rangeBtn:hover {
  color: var(--color-text-primary);
  border-color: var(--color-text-tertiary);
}

.rangeBtnActive {
  background: var(--color-white-2);
  color: var(--color-text-primary);
  border-color: var(--color-text-tertiary);
}

.refreshBtn {
  margin-left: auto;
  padding: var(--space-1) var(--space-3);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: transparent;
  color: var(--color-text-secondary);
  font-size: var(--text-xs);
  cursor: pointer;
  transition: all 0.15s;
}

.refreshBtn:hover:not(:disabled) {
  color: var(--color-text-primary);
}

.refreshBtn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.chartCard {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  overflow: hidden;
}

.chartContainer {
  width: 100%;
  min-height: 400px;
}

.chartPlaceholder {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 400px;
}

.legend {
  display: flex;
  gap: var(--space-5);
  flex-wrap: wrap;
}

.legendItem {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.legendDot {
  width: 10px;
  height: 3px;
  border-radius: 2px;
}

.tableCard {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  overflow-x: auto;
}

.table {
  width: 100%;
  border-collapse: collapse;
}

.table th {
  text-align: left;
  padding: var(--space-2) var(--space-3);
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--color-text-tertiary);
  border-bottom: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  white-space: nowrap;
}

.table td {
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-stroke-divider);
  white-space: nowrap;
}

.table tr:last-child td {
  border-bottom: none;
}

.table tbody tr:hover td {
  background: var(--color-white-4);
}

.table tfoot td {
  border-top: 2px solid var(--color-stroke-divider);
  border-bottom: none;
  background: var(--color-white-4);
  padding: var(--space-3) var(--space-3);
}

.empty {
  padding: var(--space-10) 0;
  text-align: center;
}

.filterInput {
  padding: var(--space-1) var(--space-3);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: transparent;
  color: var(--color-text-primary);
  font-size: var(--text-xs);
  width: 120px;
  outline: none;
  transition: border-color 0.15s;
}

.filterInput::placeholder {
  color: var(--color-text-tertiary);
}

.filterInput:focus {
  border-color: var(--color-text-tertiary);
}

.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: var(--radius-md);
  font-size: var(--text-xs);
  font-weight: 500;
  line-height: 1.4;
}

.badgeDefault {
  background: var(--color-white-4);
  color: var(--color-text-secondary);
}

.badgeSuccess {
  background: rgba(34, 197, 94, 0.12);
  color: #22c55e;
}

.badgeError {
  background: rgba(239, 68, 68, 0.12);
  color: #ef4444;
}

.badgeOpen {
  background: rgba(59, 130, 246, 0.15);
  color: #3b82f6;
}

.posStatsBar {
  display: flex;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.posStatCard {
  flex: 1;
  min-width: 120px;
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: var(--space-3) var(--space-4);
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.summaryGrid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(340px, 1fr));
  gap: var(--space-4);
}

.summaryCard {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: var(--space-5);
}

.summaryRow {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--color-stroke-divider);
}

.summaryRow:last-child {
  border-bottom: none;
}

.summaryEmpty {
  padding: var(--space-4) 0;
  text-align: center;
}

.ptGrid {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.ptCard {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  overflow: hidden;
}

.ptHeader {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5) var(--space-2);
}

.ptFooter {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-4);
  padding: var(--space-3) var(--space-5) var(--space-4);
  border-top: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
}

.ptStat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 90px;
}
</style>
