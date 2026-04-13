<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { fetchArbitrage, type ArbitrageEntry } from '@/lib/defi-api'
import { useBotsStore } from '@/stores/bots'
import Typography from '@/components/ui/Typography.vue'
import Button from '@/components/ui/Button.vue'
import BotCreateModal from '@/components/dashboard/BotCreateModal.vue'
import type { BotCreateRequest } from '@/types/bot'

const EXCHANGES = ['extended', 'grvt', 'variational', 'nado'] as const
type ExKey = (typeof EXCHANGES)[number]

const MA_PERIODS = [
  { key: '1d', label: '24h' },
  { key: '3d', label: '3d' },
  { key: '7d', label: '7d' },
  { key: '14d', label: '14d' },
  { key: '30d', label: '30d' },
] as const

const STABILITY_OPTIONS = [
  { key: 'all', label: 'All', minScore: 0 },
  { key: 'excellent', label: 'Excellent (4)', minScore: 4 },
  { key: 'good', label: 'Good (3+)', minScore: 3 },
  { key: 'fair', label: 'Fair (2+)', minScore: 2 },
  { key: 'low', label: 'Low (1+)', minScore: 1 },
] as const

const botsStore = useBotsStore()

// ── State ────────────────────────────────────────────
const loading = ref(true)
const error = ref('')
const rows = ref<ArbitrageEntry[]>([])
let pollInterval: ReturnType<typeof setInterval> | null = null

// Mode: 'live' = Current Rates, 'ma' = Moving Averages
const mode = ref<'live' | 'ma'>('ma')
const maPeriod = ref('1d')
const displayMode = ref<'hourly' | 'yearly'>('yearly')

// Filters
const search = ref('')
const selectedExchanges = ref<ExKey[]>([...EXCHANGES])
const stability = ref('all')
const minSpreadPct = ref(0) // in percent (0 = no filter)
const minOi = ref(0)        // in USD
const showExchangeDropdown = ref(false)
const exchangePillRef = ref<HTMLElement | null>(null)
const dropdownPos = ref({ top: '0px', left: '0px' })

// Sort
const sortKey = ref<'spread' | 'confidence' | 'volume' | 'token' | 'oi'>('spread')
const sortAsc = ref(false)

// Bot create modal
const showCreateModal = ref(false)
const prefill = ref<{ token: string; longExchange: string; shortExchange: string } | undefined>()

// ── Lifecycle ────────────────────────────────────────

const activePeriod = computed(() => mode.value === 'live' ? 'live' : maPeriod.value)

watch([mode, maPeriod], () => {
  loading.value = true
  loadData()
})

watch(selectedExchanges, () => {
  loading.value = true
  loadData()
}, { deep: true })

async function loadData() {
  try {
    rows.value = await fetchArbitrage(activePeriod.value, [...selectedExchanges.value])
    error.value = ''
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load arbitrage data'
  } finally {
    loading.value = false
  }
}

// ── Computed ─────────────────────────────────────────
const minScoreFilter = computed(() => {
  const opt = STABILITY_OPTIONS.find(o => o.key === stability.value)
  return opt ? opt.minScore : 0
})

const filtered = computed(() => {
  let data = rows.value

  // Text search
  const q = search.value.trim().toUpperCase()
  if (q) data = data.filter(r => r.ticker.toUpperCase().includes(q))

  // Stability
  if (minScoreFilter.value > 0) {
    data = data.filter(r => r.confidence_score >= minScoreFilter.value)
  }

  // Min spread
  if (minSpreadPct.value > 0) {
    data = data.filter(r => r.spread_apr * 100 >= minSpreadPct.value)
  }

  // Min OI
  if (minOi.value > 0) {
    data = data.filter(r => (r.open_interest ?? 0) >= minOi.value)
  }

  // Sort
  const dir = sortAsc.value ? 1 : -1
  return [...data].sort((a, b) => {
    if (sortKey.value === 'token') return dir * a.ticker.localeCompare(b.ticker)
    if (sortKey.value === 'spread') return dir * (a.spread_apr - b.spread_apr)
    if (sortKey.value === 'confidence') return dir * (a.confidence_score - b.confidence_score)
    if (sortKey.value === 'volume') return dir * ((a.volume_24h ?? 0) - (b.volume_24h ?? 0))
    if (sortKey.value === 'oi') return dir * ((a.open_interest ?? 0) - (b.open_interest ?? 0))
    return 0
  })
})

// ── Stat cards ───────────────────────────────────────
const statOpportunities = computed(() => filtered.value.length)
const statAvgSpread = computed(() => {
  if (!filtered.value.length) return 0
  const sum = filtered.value.reduce((s, r) => s + r.spread_apr, 0)
  return sum / filtered.value.length
})
const statStable = computed(() => filtered.value.filter(r => r.confidence_score >= 3).length)
const statHighSpread = computed(() => filtered.value.filter(r => r.spread_apr * 100 >= 50).length)

// ── Sort helpers ─────────────────────────────────────
function toggleSort(key: typeof sortKey.value) {
  if (sortKey.value === key) sortAsc.value = !sortAsc.value
  else { sortKey.value = key; sortAsc.value = false }
}

function sortIcon(key: string): string {
  if (sortKey.value !== key) return ''
  return sortAsc.value ? ' ▲' : ' ▼'
}

// ── Exchange filter ──────────────────────────────────
function openExchangeDropdown() {
  if (showExchangeDropdown.value) {
    showExchangeDropdown.value = false
    return
  }
  if (exchangePillRef.value) {
    const rect = exchangePillRef.value.getBoundingClientRect()
    dropdownPos.value = {
      top: `${rect.bottom + 6}px`,
      left: `${rect.left}px`,
    }
  }
  showExchangeDropdown.value = true
}

function closeExchangeDropdown(e: Event) {
  const target = e.target as HTMLElement
  if (exchangePillRef.value && exchangePillRef.value.contains(target)) return
  showExchangeDropdown.value = false
}

onMounted(async () => {
  document.addEventListener('click', closeExchangeDropdown)
  await loadData()
  pollInterval = setInterval(loadData, 300_000)
})

onUnmounted(() => {
  document.removeEventListener('click', closeExchangeDropdown)
  if (pollInterval) clearInterval(pollInterval)
})

function toggleExchange(ex: ExKey) {
  const idx = selectedExchanges.value.indexOf(ex)
  if (idx >= 0 && selectedExchanges.value.length > 1) {
    selectedExchanges.value = selectedExchanges.value.filter(e => e !== ex)
  } else if (idx < 0) {
    selectedExchanges.value = [...selectedExchanges.value, ex]
  }
}

// ── Formatting helpers ───────────────────────────────
function displayExchange(ex: string): string {
  if (ex === 'grvt') return 'GRVT'
  if (ex === 'nado') return 'Nado'
  return ex.charAt(0).toUpperCase() + ex.slice(1)
}

function formatRate(val: number): string {
  if (displayMode.value === 'hourly') {
    return `${(val * 100 / 8760).toFixed(4)}%`
  }
  return `${(val * 100).toFixed(2)}%`
}

function aprColor(val: number): string {
  const pct = val * 100
  if (pct > 30) return '#22c55e'
  if (pct > 10) return '#4ade80'
  if (pct > 0) return 'var(--color-text-primary)'
  return '#ef4444'
}

function formatUsd(val: number | null | undefined): string {
  if (val === undefined || val === null) return '—'
  if (val >= 1_000_000_000) return `$${(val / 1_000_000_000).toFixed(2)}B`
  if (val >= 1_000_000) return `$${(val / 1_000_000).toFixed(1)}M`
  if (val >= 1_000) return `$${(val / 1_000).toFixed(0)}K`
  return `$${val.toFixed(0)}`
}

function stabilityLabel(score: number): string {
  if (score >= 4) return 'Excellent'
  if (score >= 3) return 'Good'
  if (score >= 2) return 'Fair'
  if (score >= 1) return 'Low'
  return 'Minimal'
}

function stabilityColor(score: number): string {
  if (score >= 4) return '#22c55e'
  if (score >= 3) return '#4ade80'
  if (score >= 2) return '#facc15'
  if (score >= 1) return '#fb923c'
  return '#ef4444'
}

function statusLabel(row: ArbitrageEntry): string {
  if (row.confidence_score >= 3 && row.spread_apr * 100 >= 10) return 'Strong'
  if (row.confidence_score >= 2 && row.spread_apr * 100 >= 5) return 'Active'
  if (row.spread_apr * 100 >= 1) return 'Weak'
  return 'Low'
}

function statusVariant(row: ArbitrageEntry): string {
  const label = statusLabel(row)
  if (label === 'Strong') return 'success'
  if (label === 'Active') return 'info'
  if (label === 'Weak') return 'warning'
  return 'neutral'
}

const periodLabel = computed(() => {
  if (mode.value === 'live') return 'current rates'
  const p = MA_PERIODS.find(p => p.key === maPeriod.value)
  return `${p?.label ?? maPeriod.value} moving averages`
})

// ── Bot creation ─────────────────────────────────────
function startBot(row: ArbitrageEntry) {
  prefill.value = {
    token: row.ticker,
    longExchange: row.long_exchange,
    shortExchange: row.short_exchange,
  }
  showCreateModal.value = true
}

async function handleCreate(req: BotCreateRequest) {
  showCreateModal.value = false
  prefill.value = undefined
  try {
    await botsStore.create(req)
  } catch (e) {
    console.error('Bot creation failed:', e)
  }
}

function handleModalClose() {
  showCreateModal.value = false
  prefill.value = undefined
}
</script>

<template>
  <div :class="$style.page">
    <!-- ── Header ── -->
    <div :class="$style.header">
      <div>
        <Typography size="text-h5" weight="semibold" font="bricolage">Arbitrage Analyzer</Typography>
        <Typography size="text-sm" color="tertiary">
          Find stable funding rate arbitrage using moving averages
        </Typography>
      </div>
    </div>

    <!-- ── Mode tabs: Current Rates / Moving Averages ── -->
    <div :class="$style.modeTabs">
      <button
        :class="[$style.modeTab, mode === 'live' && $style.modeTabActive]"
        @click="mode = 'live'"
      >
        <span :class="$style.modeIcon">✦</span> Current Rates
      </button>
      <button
        :class="[$style.modeTab, mode === 'ma' && $style.modeTabActive]"
        @click="mode = 'ma'"
      >
        <span :class="$style.modeIcon">▧</span> Moving Averages
      </button>
    </div>

    <!-- ── Stat cards ── -->
    <div :class="$style.statRow">
      <div :class="$style.statCard">
        <div :class="$style.statLabel">
          <span :class="$style.statIcon">⇄</span>
          <Typography size="text-xs" color="tertiary">Opportunities</Typography>
        </div>
        <Typography size="text-h5" weight="bold">{{ statOpportunities }}</Typography>
      </div>
      <div :class="$style.statCard">
        <div :class="$style.statLabel">
          <span :class="[$style.statIcon, $style.statIconGreen]">↗</span>
          <Typography size="text-xs" color="tertiary">Avg Spread</Typography>
        </div>
        <Typography size="text-h5" weight="bold">{{ (statAvgSpread * 100).toFixed(1) }}% APR</Typography>
      </div>
      <div :class="$style.statCard">
        <div :class="$style.statLabel">
          <span :class="[$style.statIcon, $style.statIconGreen]">◎</span>
          <Typography size="text-xs" color="tertiary">Stable</Typography>
        </div>
        <Typography size="text-h5" weight="bold">{{ statStable }}</Typography>
      </div>
      <div :class="$style.statCard">
        <div :class="$style.statLabel">
          <span :class="$style.statIcon">$</span>
          <Typography size="text-xs" color="tertiary">High Spread</Typography>
        </div>
        <Typography size="text-h5" weight="bold">{{ statHighSpread }}</Typography>
      </div>
    </div>

    <!-- ── Filter row 1: Exchanges + Display + Timeframe + Stability ── -->
    <div :class="$style.filterRow">
      <!-- Exchange filter -->
      <div :class="$style.filterItem">
        <div
          ref="exchangePillRef"
          :class="[$style.exchangePill, showExchangeDropdown && $style.exchangePillOpen]"
          @click.stop="openExchangeDropdown"
        >
          <span :class="$style.exchangePillIcon">⊕</span>
          Exchanges
          <span :class="$style.exchangeCount">{{ selectedExchanges.length }}</span>
          <span :class="$style.chevron">{{ showExchangeDropdown ? '▲' : '▼' }}</span>
        </div>
      </div>

      <!-- Display toggle -->
      <div :class="$style.filterItem">
        <Typography size="text-xs" color="tertiary" :class="$style.filterLabel">Display:</Typography>
        <div :class="$style.toggleGroup">
          <button
            :class="[$style.toggleBtn, displayMode === 'hourly' && $style.toggleActive]"
            @click="displayMode = 'hourly'"
          >% Hourly</button>
          <button
            :class="[$style.toggleBtn, displayMode === 'yearly' && $style.toggleActive]"
            @click="displayMode = 'yearly'"
          >↗ Yearly</button>
        </div>
      </div>

      <!-- Timeframe (MA mode only) -->
      <div v-if="mode === 'ma'" :class="$style.filterItem">
        <Typography size="text-xs" color="tertiary" :class="$style.filterLabel">Timeframe:</Typography>
        <select v-model="maPeriod" :class="$style.selectInput">
          <option v-for="p in MA_PERIODS" :key="p.key" :value="p.key">{{ p.label }}</option>
        </select>
      </div>

      <!-- Stability -->
      <div :class="$style.filterItem">
        <Typography size="text-xs" color="tertiary" :class="$style.filterLabel">Stability:</Typography>
        <select v-model="stability" :class="$style.selectInput">
          <option v-for="s in STABILITY_OPTIONS" :key="s.key" :value="s.key">{{ s.label }}</option>
        </select>
      </div>

      <!-- Min Spread -->
      <div :class="$style.filterItem">
        <Typography size="text-xs" color="tertiary" :class="$style.filterLabel">Min Spread:</Typography>
        <div :class="$style.sliderWrap">
          <input
            v-model.number="minSpreadPct"
            type="range"
            min="0"
            max="100"
            step="1"
            :class="$style.slider"
          />
          <span :class="$style.sliderValue">{{ minSpreadPct }}% APR</span>
        </div>
      </div>

      <!-- Min OI (right side) -->
      <div :class="$style.filterItem">
        <Typography size="text-xs" color="tertiary" :class="$style.filterLabel">Min OI:</Typography>
        <div :class="$style.sliderWrap">
          <input
            v-model.number="minOi"
            type="range"
            min="0"
            max="50000000"
            step="100000"
            :class="$style.slider"
          />
          <span :class="$style.sliderValue">{{ formatUsd(minOi) }}</span>
        </div>
      </div>

      <!-- Search -->
      <div :class="$style.filterItemRight">
        <input
          v-model="search"
          :class="$style.searchInput"
          type="text"
          placeholder="Search token..."
          spellcheck="false"
        />
      </div>
    </div>

    <Teleport to="body">
      <template v-if="showExchangeDropdown">
        <div :class="$style.overlay" @click="showExchangeDropdown = false" />
        <div :class="$style.exchangeDropdown" :style="dropdownPos" @click.stop>
          <label
            v-for="ex in EXCHANGES"
            :key="ex"
            :class="$style.exchangeOption"
          >
            <input
              type="checkbox"
              :checked="selectedExchanges.includes(ex)"
              @change="toggleExchange(ex)"
              :class="$style.checkbox"
            />
            {{ displayExchange(ex) }}
          </label>
        </div>
      </template>
    </Teleport>

    <!-- ── Info text ── -->
    <div :class="$style.infoText">
      <Typography size="text-sm" color="secondary">
        Showing {{ filtered.length }} opportunities based on {{ periodLabel }}
      </Typography>
    </div>

    <!-- ── Loading / Error / Empty ── -->
    <div v-if="loading" :class="$style.empty">
      <Typography color="secondary">Loading arbitrage data...</Typography>
    </div>

    <div v-else-if="error" :class="$style.errorBox">
      <Typography size="text-sm" color="error">{{ error }}</Typography>
    </div>

    <div v-else-if="!filtered.length" :class="$style.empty">
      <Typography color="tertiary">No arbitrage pairs found{{ search ? ` matching "${search}"` : '' }}</Typography>
    </div>

    <!-- ── Table ── -->
    <div v-else :class="$style.table">
      <div :class="[$style.row, $style.rowHeader]">
        <div :class="[$style.cell, $style.cellToken]" @click="toggleSort('token')">
          <Typography size="text-xs" weight="semibold" color="tertiary">Token{{ sortIcon('token') }}</Typography>
        </div>
        <div :class="[$style.cell, $style.cellExch]">
          <Typography size="text-xs" weight="semibold" color="tertiary">Long</Typography>
        </div>
        <div :class="[$style.cell, $style.cellExch]">
          <Typography size="text-xs" weight="semibold" color="tertiary">Short</Typography>
        </div>
        <div :class="[$style.cell, $style.cellRight]" @click="toggleSort('spread')">
          <Typography size="text-xs" weight="semibold" color="tertiary">{{ displayMode === 'yearly' ? 'Yearly' : 'Hourly' }} Spread{{ sortIcon('spread') }}</Typography>
        </div>
        <div :class="[$style.cell, $style.cellRight]" @click="toggleSort('oi')">
          <Typography size="text-xs" weight="semibold" color="tertiary">Open Interest{{ sortIcon('oi') }}</Typography>
        </div>
        <div :class="[$style.cell, $style.cellStability]" @click="toggleSort('confidence')">
          <Typography size="text-xs" weight="semibold" color="tertiary">Stability{{ sortIcon('confidence') }}</Typography>
        </div>
        <div :class="[$style.cell, $style.cellStatus]">
          <Typography size="text-xs" weight="semibold" color="tertiary">Status</Typography>
        </div>
        <div :class="[$style.cell, $style.cellAction]"></div>
      </div>

      <div
        v-for="row in filtered"
        :key="`${row.ticker}-${row.long_exchange}-${row.short_exchange}`"
        :class="[$style.row, $style.rowData]"
      >
        <div :class="[$style.cell, $style.cellToken]">
          <Typography size="text-md" weight="medium">{{ row.ticker }}</Typography>
        </div>
        <div :class="[$style.cell, $style.cellExch]">
          <span :class="$style.badge">{{ displayExchange(row.long_exchange) }}</span>
        </div>
        <div :class="[$style.cell, $style.cellExch]">
          <span :class="$style.badge">{{ displayExchange(row.short_exchange) }}</span>
        </div>
        <div :class="[$style.cell, $style.cellRight]">
          <Typography size="text-sm" weight="semibold" :style="{ color: aprColor(row.spread_apr) }">
            {{ formatRate(row.spread_apr) }}
          </Typography>
        </div>
        <div :class="[$style.cell, $style.cellRight]">
          <Typography size="text-sm" color="secondary">{{ formatUsd(row.open_interest) }}</Typography>
        </div>
        <div :class="[$style.cell, $style.cellStability]">
          <span :class="$style.stabilityDots">
            <span
              v-for="i in 4"
              :key="i"
              :class="$style.dot"
              :style="{ background: i <= row.confidence_score ? stabilityColor(row.confidence_score) : 'var(--color-stroke-divider)' }"
            />
          </span>
          <Typography size="text-xs" color="tertiary">{{ stabilityLabel(row.confidence_score) }}</Typography>
        </div>
        <div :class="[$style.cell, $style.cellStatus]">
          <span :class="[$style.statusChip, $style[`statusChip--${statusVariant(row)}`]]">
            {{ statusLabel(row) }}
          </span>
        </div>
        <div :class="[$style.cell, $style.cellAction]">
          <Button variant="solid" color="success" size="sm" @click="startBot(row)">Start Bot</Button>
        </div>
      </div>
    </div>

    <!-- Bot create modal -->
    <BotCreateModal
      :open="showCreateModal"
      :prefill="prefill"
      @close="handleModalClose"
      @create="handleCreate"
    />
  </div>
</template>

<style module>
.page {
  padding: 50px 40px;
  max-width: 1400px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

/* ── Header ── */
.header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}

/* ── Mode tabs ── */
.modeTabs {
  display: flex;
  gap: 0;
  background: var(--color-white-4);
  border-radius: var(--radius-lg);
  padding: 3px;
  width: fit-content;
}

.modeTab {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 20px;
  border-radius: var(--radius-md);
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: var(--text-sm);
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
}
.modeTab:hover { color: var(--color-text-primary); }
.modeTabActive {
  background: var(--color-white-2);
  color: var(--color-text-primary);
  font-weight: 600;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.12);
}

.modeIcon {
  font-size: 14px;
}

/* ── Stat cards ── */
.statRow {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--space-4);
}

.statCard {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: var(--space-4) var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.statLabel {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.statIcon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  border-radius: var(--radius-sm);
  background: var(--color-white-10);
  font-size: 12px;
  color: var(--color-text-tertiary);
}

.statIconGreen {
  background: rgba(34, 197, 94, 0.12);
  color: #22c55e;
}

/* ── Filter rows ── */
.filterRow {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  flex-wrap: wrap;
  position: relative;
  z-index: 10;
}

.filterItemRight {
  margin-left: auto;
}

.filterItem {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  position: relative;
}

.filterLabel {
  white-space: nowrap;
}

/* Exchange pill */
.exchangePill {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 14px;
  border-radius: 999px;
  border: 1px solid #22c55e;
  background: rgba(34, 197, 94, 0.08);
  color: #22c55e;
  font-size: var(--text-sm);
  font-weight: 500;
  cursor: pointer;
  user-select: none;
  transition: all 0.15s;
}
.exchangePill:hover { background: rgba(34, 197, 94, 0.14); }
.exchangePillOpen { background: rgba(34, 197, 94, 0.14); }

.exchangePillIcon { font-size: 14px; }

.exchangeCount {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 20px;
  height: 20px;
  border-radius: 999px;
  background: #22c55e;
  color: #fff;
  font-size: 11px;
  font-weight: 700;
}

.chevron {
  font-size: 9px;
  margin-left: 2px;
}

.exchangeDropdown {
  position: fixed;
  background: var(--color-white-2);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-md);
  padding: var(--space-2);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  z-index: 9999;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.3);
  min-width: 180px;
}

.exchangeOption {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  cursor: pointer;
  font-size: var(--text-sm);
  color: var(--color-text-primary);
  transition: background 0.1s;
}
.exchangeOption:hover { background: var(--color-white-4); }

.checkbox {
  accent-color: #22c55e;
  width: 16px;
  height: 16px;
}

/* Toggle group */
.toggleGroup {
  display: flex;
  gap: 0;
  background: var(--color-white-4);
  border-radius: var(--radius-sm);
  padding: 2px;
}

.toggleBtn {
  padding: 4px 12px;
  border: none;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-tertiary);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
}
.toggleBtn:hover { color: var(--color-text-secondary); }
.toggleActive {
  background: var(--color-white-2);
  color: var(--color-text-primary);
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
}

/* Select inputs */
.selectInput {
  height: 32px;
  padding: 0 var(--space-3);
  padding-right: 28px;
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  color: var(--color-text-primary);
  font-size: var(--text-sm);
  outline: none;
  cursor: pointer;
  appearance: auto;
}

/* Slider */
.sliderWrap {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 160px;
}

.slider {
  flex: 1;
  height: 4px;
  -webkit-appearance: none;
  appearance: none;
  background: var(--color-stroke-divider);
  border-radius: 2px;
  outline: none;
}
.slider::-webkit-slider-thumb {
  -webkit-appearance: none;
  appearance: none;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: var(--color-text-primary);
  cursor: pointer;
  border: 2px solid var(--color-white-2);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
}

.sliderValue {
  font-size: 12px;
  color: var(--color-text-secondary);
  min-width: 60px;
  text-align: right;
  white-space: nowrap;
}

.searchInput {
  width: 260px;
  height: 36px;
  padding: 0 var(--space-4);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  color: var(--color-text-primary);
  font-size: var(--text-sm);
  outline: none;
  transition: border-color 0.15s;
}
.searchInput::placeholder { color: var(--color-text-tertiary); }
.searchInput:focus { border-color: var(--color-text-secondary); }

/* Overlay behind dropdown */
.overlay {
  position: fixed;
  inset: 0;
  z-index: 9998;
  background: transparent;
}

/* ── Info text ── */
.infoText {
  padding: var(--space-1) 0;
}

/* ── Table ── */
.table {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  overflow: hidden;
}

.row {
  display: flex;
  align-items: center;
  padding: var(--space-3) var(--space-5);
  border-bottom: 1px solid var(--color-stroke-divider);
}
.row:last-child { border-bottom: none; }

.rowHeader {
  background: var(--color-bg-secondary);
  cursor: pointer;
  user-select: none;
}

.rowData {
  transition: background 0.1s;
}
.rowData:hover { background: var(--color-white-4); }

.cell { flex: 1; }
.cellToken { flex: 0 0 90px; }
.cellExch { flex: 0 0 110px; }
.cellRight {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 1px;
}
.cellStability {
  flex: 0 0 120px;
  display: flex;
  align-items: center;
  gap: 6px;
}
.cellStatus {
  flex: 0 0 80px;
  display: flex;
  align-items: center;
}
.cellAction {
  flex: 0 0 100px;
  display: flex;
  justify-content: flex-end;
}

/* Badges */
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  background: var(--color-white-4);
  border: 1px solid var(--color-stroke-divider);
  font-size: 11px;
  color: var(--color-text-secondary);
}

/* Stability dots */
.stabilityDots {
  display: flex;
  gap: 3px;
  align-items: center;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

/* Status chip */
.statusChip {
  display: inline-flex;
  align-items: center;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
}

.statusChip--success {
  background: rgba(34, 197, 94, 0.12);
  color: #22c55e;
}
.statusChip--info {
  background: rgba(59, 130, 246, 0.12);
  color: #3b82f6;
}
.statusChip--warning {
  background: rgba(251, 146, 60, 0.12);
  color: #fb923c;
}
.statusChip--neutral {
  background: var(--color-white-10);
  color: var(--color-text-tertiary);
}

/* ── Error / Empty ── */
.errorBox {
  padding: var(--space-3) var(--space-4);
  background: var(--color-error-bg);
  border: 1px solid var(--color-error-stroke);
  border-radius: var(--radius-md);
}

.empty {
  padding: var(--space-16) 0;
  text-align: center;
}
</style>
