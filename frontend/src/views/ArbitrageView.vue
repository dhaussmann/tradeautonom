<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { fetchArbOpportunities, fetchArbConfig, type ArbOpportunity, type ArbConfig } from '@/lib/api'
import Typography from '@/components/ui/Typography.vue'

// ── State ────────────────────────────────────────────
const loading = ref(true)
const error = ref('')
const rows = ref<ArbOpportunity[]>([])
const config = ref<ArbConfig | null>(null)
let pollInterval: ReturnType<typeof setInterval> | null = null

// Filters
const search = ref('')
const minProfitBps = ref(0)

// Sort
const sortKey = ref<'net_profit_bps' | 'bbo_spread_bps' | 'max_notional_usd' | 'token'>('net_profit_bps')
const sortAsc = ref(false)

// ── Lifecycle ────────────────────────────────────────

async function loadData() {
  try {
    const [opps, cfg] = await Promise.all([
      fetchArbOpportunities(),
      fetchArbConfig(),
    ])
    rows.value = opps
    config.value = cfg
    error.value = ''
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load arbitrage data'
  } finally {
    loading.value = false
  }
}

onMounted(async () => {
  await loadData()
  pollInterval = setInterval(loadData, 2000)
})

onUnmounted(() => {
  if (pollInterval) clearInterval(pollInterval)
})

// ── Computed ─────────────────────────────────────────

const filtered = computed(() => {
  let data = rows.value

  // Text search
  const q = search.value.trim().toUpperCase()
  if (q) data = data.filter(r =>
    r.token.toUpperCase().includes(q) ||
    r.buy_exchange.toUpperCase().includes(q) ||
    r.sell_exchange.toUpperCase().includes(q)
  )

  // Min profit
  if (minProfitBps.value > 0) {
    data = data.filter(r => r.net_profit_bps >= minProfitBps.value)
  }

  // Sort
  const dir = sortAsc.value ? 1 : -1
  return [...data].sort((a, b) => {
    if (sortKey.value === 'token') return dir * a.token.localeCompare(b.token)
    if (sortKey.value === 'net_profit_bps') return dir * (a.net_profit_bps - b.net_profit_bps)
    if (sortKey.value === 'bbo_spread_bps') return dir * (a.bbo_spread_bps - b.bbo_spread_bps)
    if (sortKey.value === 'max_notional_usd') return dir * (a.max_notional_usd - b.max_notional_usd)
    return 0
  })
})

// ── Stat cards ───────────────────────────────────────

const statTotal = computed(() => filtered.value.length)
const statBestProfit = computed(() => {
  if (!filtered.value.length) return 0
  return Math.max(...filtered.value.map(r => r.net_profit_bps))
})
const statTotalNotional = computed(() => {
  return filtered.value.reduce((s, r) => s + r.max_notional_usd, 0)
})
const statExchanges = computed(() => {
  const exs = new Set<string>()
  for (const r of filtered.value) {
    exs.add(r.buy_exchange)
    exs.add(r.sell_exchange)
  }
  return exs.size
})

// ── Sort helpers ─────────────────────────────────────

function toggleSort(key: typeof sortKey.value) {
  if (sortKey.value === key) sortAsc.value = !sortAsc.value
  else { sortKey.value = key; sortAsc.value = false }
}

function sortIcon(key: string): string {
  if (sortKey.value !== key) return ''
  return sortAsc.value ? ' ▲' : ' ▼'
}

// ── Formatting helpers ───────────────────────────────

function displayExchange(ex: string): string {
  if (ex === 'grvt') return 'GRVT'
  if (ex === 'nado') return 'Nado'
  return ex.charAt(0).toUpperCase() + ex.slice(1)
}

function bpsToPercent(bps: number): string {
  return (bps / 100).toFixed(3)
}

function profitColor(bps: number): string {
  if (bps >= 10) return '#22c55e'
  if (bps >= 5) return '#4ade80'
  if (bps >= 2) return 'var(--color-text-primary)'
  return 'var(--color-text-tertiary)'
}

function profitBgColor(bps: number): string {
  if (bps >= 10) return 'rgba(34, 197, 94, 0.15)'
  if (bps >= 5) return 'rgba(74, 222, 128, 0.1)'
  return 'var(--color-white-4)'
}

function formatUsd(val: number): string {
  if (val >= 1_000_000) return `$${(val / 1_000_000).toFixed(1)}M`
  if (val >= 1_000) return `$${(val / 1_000).toFixed(0)}K`
  return `$${val.toFixed(0)}`
}

function formatQty(val: number): string {
  if (val >= 1_000_000) return `${(val / 1_000_000).toFixed(2)}M`
  if (val >= 1_000) return `${(val / 1_000).toFixed(1)}K`
  if (val >= 1) return val.toFixed(2)
  return val.toFixed(4)
}

function formatPrice(val: number): string {
  if (val >= 10000) return val.toFixed(0)
  if (val >= 100) return val.toFixed(2)
  if (val >= 1) return val.toFixed(3)
  return val.toFixed(6)
}

function ageLabel(ts: number): string {
  const age = Date.now() - ts
  if (age < 1000) return 'now'
  if (age < 60000) return `${Math.floor(age / 1000)}s ago`
  return `${Math.floor(age / 60000)}m ago`
}
</script>

<template>
  <div :class="$style.page">
    <!-- ── Header ── -->
    <div :class="$style.header">
      <div>
        <Typography size="text-h5" weight="semibold" font="bricolage">Cross-Exchange Arbitrage</Typography>
        <Typography size="text-sm" color="tertiary">
          Real-time VWAP-safe arbitrage opportunities across GRVT, Extended &amp; Nado
        </Typography>
      </div>
      <div v-if="config" :class="$style.headerMeta">
        <span :class="$style.metaPill">{{ config.tokens_tracked }} tokens</span>
        <span :class="$style.metaPill">{{ config.scan_interval_s }}s scan</span>
      </div>
    </div>

    <!-- ── Stat cards ── -->
    <div :class="$style.statRow">
      <div :class="$style.statCard">
        <div :class="$style.statLabel">
          <span :class="$style.statIcon">⇄</span>
          <Typography size="text-xs" color="tertiary">Opportunities</Typography>
        </div>
        <Typography size="text-h5" weight="bold">{{ statTotal }}</Typography>
      </div>
      <div :class="$style.statCard">
        <div :class="$style.statLabel">
          <span :class="[$style.statIcon, $style.statIconGreen]">↗</span>
          <Typography size="text-xs" color="tertiary">Best Profit</Typography>
        </div>
        <Typography size="text-h5" weight="bold" :style="{ color: profitColor(statBestProfit) }">
          {{ bpsToPercent(statBestProfit) }}%
        </Typography>
      </div>
      <div :class="$style.statCard">
        <div :class="$style.statLabel">
          <span :class="$style.statIcon">$</span>
          <Typography size="text-xs" color="tertiary">Total Notional</Typography>
        </div>
        <Typography size="text-h5" weight="bold">{{ formatUsd(statTotalNotional) }}</Typography>
      </div>
      <div :class="$style.statCard">
        <div :class="$style.statLabel">
          <span :class="$style.statIcon">◎</span>
          <Typography size="text-xs" color="tertiary">Active Exchanges</Typography>
        </div>
        <Typography size="text-h5" weight="bold">{{ statExchanges }}</Typography>
      </div>
    </div>

    <!-- ── Filter row ── -->
    <div :class="$style.filterRow">
      <div :class="$style.filterItem">
        <Typography size="text-xs" color="tertiary" :class="$style.filterLabel">Min Profit:</Typography>
        <div :class="$style.sliderWrap">
          <input
            v-model.number="minProfitBps"
            type="range"
            min="0"
            max="50"
            step="0.5"
            :class="$style.slider"
          />
          <span :class="$style.sliderValue">{{ bpsToPercent(minProfitBps) }}%</span>
        </div>
      </div>
      <div :class="$style.filterItemRight">
        <input
          v-model="search"
          :class="$style.searchInput"
          type="text"
          placeholder="Search token or exchange..."
          spellcheck="false"
        />
      </div>
    </div>

    <!-- ── Info text ── -->
    <div :class="$style.infoText">
      <Typography size="text-sm" color="secondary">
        Showing {{ filtered.length }} live arbitrage opportunities (auto-refreshing)
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
      <Typography color="tertiary">No arbitrage opportunities found{{ search ? ` matching "${search}"` : '' }}</Typography>
    </div>

    <!-- ── Results (Table + Cards) ── -->
    <template v-else>
      <!-- Desktop Table -->
      <div :class="$style.table">
        <div :class="[$style.row, $style.rowHeader]">
          <div :class="[$style.cell, $style.cellToken]" @click="toggleSort('token')">
            <Typography size="text-xs" weight="semibold" color="tertiary">Token{{ sortIcon('token') }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellExch]">
            <Typography size="text-xs" weight="semibold" color="tertiary">Buy</Typography>
          </div>
          <div :class="[$style.cell, $style.cellExch]">
            <Typography size="text-xs" weight="semibold" color="tertiary">Sell</Typography>
          </div>
          <div :class="[$style.cell, $style.cellPrice]">
            <Typography size="text-xs" weight="semibold" color="tertiary">Buy Ask</Typography>
          </div>
          <div :class="[$style.cell, $style.cellPrice]">
            <Typography size="text-xs" weight="semibold" color="tertiary">Sell Bid</Typography>
          </div>
          <div :class="[$style.cell, $style.cellRight]" @click="toggleSort('bbo_spread_bps')">
            <Typography size="text-xs" weight="semibold" color="tertiary">Spread %{{ sortIcon('bbo_spread_bps') }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellRight]" @click="toggleSort('net_profit_bps')">
            <Typography size="text-xs" weight="semibold" color="tertiary">Profit %{{ sortIcon('net_profit_bps') }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellQty]">
            <Typography size="text-xs" weight="semibold" color="tertiary">Max Qty</Typography>
          </div>
          <div :class="[$style.cell, $style.cellRight]" @click="toggleSort('max_notional_usd')">
            <Typography size="text-xs" weight="semibold" color="tertiary">Notional{{ sortIcon('max_notional_usd') }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellAge]">
            <Typography size="text-xs" weight="semibold" color="tertiary">Age</Typography>
          </div>
        </div>

        <div
          v-for="(row, idx) in filtered"
          :key="`${row.token}-${row.buy_exchange}-${row.sell_exchange}-${idx}`"
          :class="[$style.row, $style.rowData]"
        >
          <div :class="[$style.cell, $style.cellToken]">
            <Typography size="text-md" weight="medium">{{ row.token }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellExch]">
            <span :class="$style.badge">{{ displayExchange(row.buy_exchange) }}</span>
          </div>
          <div :class="[$style.cell, $style.cellExch]">
            <span :class="$style.badge">{{ displayExchange(row.sell_exchange) }}</span>
          </div>
          <div :class="[$style.cell, $style.cellPrice]">
            <Typography size="text-sm" color="secondary">{{ formatPrice(row.buy_price_bbo) }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellPrice]">
            <Typography size="text-sm" color="secondary">{{ formatPrice(row.sell_price_bbo) }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellRight]">
            <Typography size="text-sm" color="secondary">{{ bpsToPercent(row.bbo_spread_bps) }}%</Typography>
          </div>
          <div :class="[$style.cell, $style.cellRight]">
            <Typography size="text-sm" weight="semibold" :style="{ color: profitColor(row.net_profit_bps) }">
              {{ bpsToPercent(row.net_profit_bps) }}%
            </Typography>
          </div>
          <div :class="[$style.cell, $style.cellQty]">
            <Typography size="text-sm" color="secondary">{{ formatQty(row.max_qty) }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellRight]">
            <Typography size="text-sm" weight="medium">{{ formatUsd(row.max_notional_usd) }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellAge]">
            <Typography size="text-xs" color="tertiary">{{ ageLabel(row.timestamp_ms) }}</Typography>
          </div>
        </div>
      </div>

      <!-- Mobile Cards -->
      <div :class="$style.mobileCards">
        <div
          v-for="(row, idx) in filtered"
          :key="`mobile-${row.token}-${row.buy_exchange}-${row.sell_exchange}-${idx}`"
          :class="$style.oppCard"
          :style="{ background: profitBgColor(row.net_profit_bps) }"
        >
          <div :class="$style.oppHeader">
            <div :class="$style.oppTitle">
              <Typography size="text-lg" weight="bold">{{ row.token }}</Typography>
              <Typography
                size="text-sm"
                weight="bold"
                :style="{ color: profitColor(row.net_profit_bps) }"
              >
                {{ bpsToPercent(row.net_profit_bps) }}%
              </Typography>
            </div>
            <Typography size="text-xs" color="tertiary">{{ ageLabel(row.timestamp_ms) }}</Typography>
          </div>

          <div :class="$style.oppExchanges">
            <div :class="$style.oppExchange">
              <Typography size="text-xs" color="tertiary">BUY</Typography>
              <span :class="$style.badge">{{ displayExchange(row.buy_exchange) }}</span>
              <Typography size="text-sm">{{ formatPrice(row.buy_price_bbo) }}</Typography>
            </div>
            <div :class="$style.oppArrow">→</div>
            <div :class="$style.oppExchange">
              <Typography size="text-xs" color="tertiary">SELL</Typography>
              <span :class="$style.badge">{{ displayExchange(row.sell_exchange) }}</span>
              <Typography size="text-sm">{{ formatPrice(row.sell_price_bbo) }}</Typography>
            </div>
          </div>

          <div :class="$style.oppStats">
            <div :class="$style.oppStat">
              <Typography size="text-xs" color="tertiary">Spread</Typography>
              <Typography size="text-sm" color="secondary">{{ bpsToPercent(row.bbo_spread_bps) }}%</Typography>
            </div>
            <div :class="$style.oppStat">
              <Typography size="text-xs" color="tertiary">Max Qty</Typography>
              <Typography size="text-sm">{{ formatQty(row.max_qty) }}</Typography>
            </div>
            <div :class="$style.oppStat">
              <Typography size="text-xs" color="tertiary">Notional</Typography>
              <Typography size="text-sm" weight="medium">{{ formatUsd(row.max_notional_usd) }}</Typography>
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style module>
.page {
  padding: 50px 40px;
  max-width: 1500px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

/* ── Header ── */
.header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.headerMeta {
  display: flex;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.metaPill {
  display: inline-flex;
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--color-white-4);
  border: 1px solid var(--color-stroke-divider);
  font-size: 11px;
  color: var(--color-text-tertiary);
  white-space: nowrap;
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

/* ── Filter row ── */
.filterRow {
  display: flex;
  align-items: center;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.filterItem {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: 1;
  min-width: 250px;
}

.filterItemRight {
  margin-left: auto;
}

.filterLabel {
  white-space: nowrap;
}

.sliderWrap {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: 1;
}

.slider {
  flex: 1;
  height: 4px;
  -webkit-appearance: none;
  appearance: none;
  background: var(--color-stroke-divider);
  border-radius: 2px;
  outline: none;
  min-width: 120px;
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
  width: 280px;
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

/* ── Info text ── */
.infoText {
  padding: var(--space-1) 0;
}

/* ── Desktop Table ── */
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
.cellToken { flex: 0 0 80px; }
.cellExch { flex: 0 0 100px; }
.cellPrice {
  flex: 0 0 110px;
  text-align: right;
}
.cellRight {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 1px;
}
.cellQty {
  flex: 0 0 100px;
  text-align: right;
}
.cellAge {
  flex: 0 0 70px;
  text-align: right;
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

/* ── Mobile Cards ── */
.mobileCards {
  display: none;
  flex-direction: column;
  gap: var(--space-3);
}

.oppCard {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  transition: transform 0.1s ease;
}

.oppCard:active {
  transform: scale(0.99);
}

.oppHeader {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}

.oppTitle {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.oppExchanges {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  gap: var(--space-3);
  align-items: center;
  padding: var(--space-3);
  background: var(--color-white-4);
  border-radius: var(--radius-lg);
}

.oppExchange {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}

.oppArrow {
  font-size: 18px;
  color: var(--color-text-tertiary);
}

.oppStats {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--space-3);
  padding-top: var(--space-2);
  border-top: 1px solid var(--color-stroke-divider);
}

.oppStat {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}

/* ===== RESPONSIVE BREAKPOINTS ===== */

/* Tablet */
@media (max-width: 1024px) {
  .page {
    padding: 24px 20px;
  }

  .statRow {
    grid-template-columns: repeat(2, 1fr);
  }

  .searchInput {
    width: 200px;
  }
}

/* Mobile */
@media (max-width: 767px) {
  .page {
    padding: 16px;
    gap: var(--space-3);
  }

  .header {
    flex-direction: column;
    gap: var(--space-3);
  }

  .headerMeta {
    width: 100%;
  }

  .statRow {
    grid-template-columns: repeat(2, 1fr);
    gap: var(--space-3);
  }

  .statCard {
    padding: var(--space-3);
  }

  .filterRow {
    flex-direction: column;
    align-items: stretch;
    gap: var(--space-3);
  }

  .filterItem {
    width: 100%;
  }

  .filterItemRight {
    margin-left: 0;
  }

  .searchInput {
    width: 100%;
  }

  /* Hide desktop table on mobile */
  .table {
    display: none;
  }

  /* Show mobile cards */
  .mobileCards {
    display: flex;
  }
}

/* Small mobile */
@media (max-width: 480px) {
  .statRow {
    grid-template-columns: repeat(2, 1fr);
  }

  .oppExchanges {
    grid-template-columns: 1fr;
    gap: var(--space-2);
  }

  .oppArrow {
    transform: rotate(90deg);
  }

  .oppStats {
    grid-template-columns: 1fr;
    text-align: center;
  }

  .oppStat {
    flex-direction: row;
    justify-content: space-between;
    padding: var(--space-2) 0;
    border-bottom: 1px solid var(--color-stroke-divider);
  }

  .oppStat:last-child {
    border-bottom: none;
  }
}
</style>
