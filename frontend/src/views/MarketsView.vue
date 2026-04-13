<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { fetchMarketsLatest, fetchMarketsBySymbol, type MarketEntry } from '@/lib/defi-api'
import Typography from '@/components/ui/Typography.vue'

const ALLOWED_EXCHANGES = ['extended', 'variational', 'grvt']

const loading = ref(true)
const error = ref('')
const search = ref('')

// Group latest data by symbol
interface TokenRow {
  symbol: string
  exchange: string
  fundingApr: number
  price: number | null
  volume24h: number | null
  marketType: string
}
const tokens = ref<TokenRow[]>([])

const expandedToken = ref<string | null>(null)
const expandedData = ref<MarketEntry[]>([])
const expandedLoading = ref(false)
let pollInterval: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
  await loadTokens()
  pollInterval = setInterval(loadTokens, 60000)
})

onUnmounted(() => {
  if (pollInterval) clearInterval(pollInterval)
})

async function loadTokens() {
  try {
    const data = await fetchMarketsLatest()
    tokens.value = data
      .filter(d => ALLOWED_EXCHANGES.includes(d.exchange))
      .map(d => ({
        symbol: d.normalized_symbol,
        exchange: d.exchange,
        fundingApr: d.funding_rate_apr,
        price: d.market_price,
        volume24h: d.volume_24h,
        marketType: d.market_type,
      }))
    error.value = ''
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load tokens'
  } finally {
    loading.value = false
  }
}

const filtered = computed(() => {
  const q = search.value.trim().toUpperCase()
  if (!q) return tokens.value
  return tokens.value.filter(t => t.symbol.toUpperCase().includes(q))
})

async function toggleExpand(symbol: string) {
  if (expandedToken.value === symbol) {
    expandedToken.value = null
    expandedData.value = []
    return
  }
  expandedToken.value = symbol
  expandedData.value = []
  expandedLoading.value = true
  try {
    const all = await fetchMarketsBySymbol(symbol)
    expandedData.value = all.filter(d => ALLOWED_EXCHANGES.includes(d.exchange))
  } catch {
    expandedData.value = []
  } finally {
    expandedLoading.value = false
  }
}

function formatUsd(val: number | null | undefined): string {
  if (val === undefined || val === null) return '—'
  if (val >= 1_000_000_000) return `$${(val / 1_000_000_000).toFixed(2)}B`
  if (val >= 1_000_000) return `$${(val / 1_000_000).toFixed(2)}M`
  if (val >= 1_000) return `$${(val / 1_000).toFixed(1)}K`
  return `$${val.toFixed(2)}`
}

function formatPrice(val: number | null | undefined): string {
  if (val === undefined || val === null) return '—'
  if (val >= 1000) return `$${val.toLocaleString(undefined, { maximumFractionDigits: 2 })}`
  if (val >= 1) return `$${val.toFixed(4)}`
  return `$${val.toFixed(6)}`
}

function formatApr(val: number | null | undefined): string {
  if (val === undefined || val === null) return '—'
  return `${(val * 100).toFixed(2)}%`
}

function fundingColor(rate: number): string {
  const pct = rate * 100
  if (pct > 20) return 'var(--color-success-text, #22c55e)'
  if (pct > 0) return 'var(--color-text-primary)'
  return 'var(--color-error-text, #ef4444)'
}

function displayExchange(ex: string): string {
  if (ex === 'grvt') return 'GRVT'
  return ex.charAt(0).toUpperCase() + ex.slice(1)
}
</script>

<template>
  <div :class="$style.page">
    <div :class="$style.header">
      <Typography size="text-h5" weight="semibold" font="bricolage">Markets</Typography>
      <Typography size="text-sm" color="tertiary">
        {{ tokens.length }} tokens across 7 exchanges · via api.fundingrate.de
      </Typography>
    </div>

    <div :class="$style.searchBar">
      <input
        v-model="search"
        :class="$style.searchInput"
        type="text"
        placeholder="Search token (e.g. BTC, SOL, ETH...)"
        spellcheck="false"
      />
    </div>

    <div v-if="loading" :class="$style.empty">
      <Typography color="secondary">Loading market data...</Typography>
    </div>

    <div v-else-if="error" :class="$style.error">
      <Typography size="text-sm" color="error">{{ error }}</Typography>
    </div>

    <div v-else-if="!filtered.length" :class="$style.empty">
      <Typography color="tertiary">No tokens match "{{ search }}"</Typography>
    </div>

    <div v-else :class="$style.table">
      <!-- Table header -->
      <div :class="[$style.row, $style.rowHeader]">
        <div :class="[$style.cell, $style.cellToken]">
          <Typography size="text-xs" weight="semibold" color="tertiary">TOKEN</Typography>
        </div>
        <div :class="[$style.cell, $style.cellExchange]">
          <Typography size="text-xs" weight="semibold" color="tertiary">BEST APR ON</Typography>
        </div>
        <div :class="[$style.cell, $style.cellRight]">
          <Typography size="text-xs" weight="semibold" color="tertiary">FUNDING APR</Typography>
        </div>
        <div :class="[$style.cell, $style.cellRight]">
          <Typography size="text-xs" weight="semibold" color="tertiary">PRICE</Typography>
        </div>
        <div :class="[$style.cell, $style.cellChevron]"></div>
      </div>

      <!-- Token rows -->
      <template v-for="token in filtered" :key="token.symbol + token.exchange">
        <div
          :class="[$style.row, $style.rowData, expandedToken === token.symbol && $style.rowExpanded]"
          @click="toggleExpand(token.symbol)"
        >
          <div :class="[$style.cell, $style.cellToken]">
            <Typography size="text-md" weight="medium">{{ token.symbol }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellExchange]">
            <span :class="$style.badge">{{ displayExchange(token.exchange) }}</span>
          </div>
          <div :class="[$style.cell, $style.cellRight]">
            <Typography size="text-sm" :style="{ color: fundingColor(token.fundingApr) }">
              {{ formatApr(token.fundingApr) }}
            </Typography>
          </div>
          <div :class="[$style.cell, $style.cellRight]">
            <Typography size="text-sm">{{ formatPrice(token.price) }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellChevron]">
            <span :class="$style.chevron">{{ expandedToken === token.symbol ? '▲' : '▼' }}</span>
          </div>
        </div>

        <!-- Expanded detail: all exchanges for this symbol -->
        <div v-if="expandedToken === token.symbol" :class="$style.detail">
          <div v-if="expandedLoading" :class="$style.detailLoading">
            <Typography size="text-sm" color="secondary">Loading all exchanges...</Typography>
          </div>

          <template v-else-if="expandedData.length">
            <div :class="$style.subTable">
              <div :class="[$style.subRow, $style.subHeader]">
                <div :class="$style.subCell"><Typography size="text-xs" color="tertiary">Exchange</Typography></div>
                <div :class="[$style.subCell, $style.subRight]"><Typography size="text-xs" color="tertiary">Funding APR</Typography></div>
                <div :class="[$style.subCell, $style.subRight]"><Typography size="text-xs" color="tertiary">Price</Typography></div>
                <div :class="[$style.subCell, $style.subRight]"><Typography size="text-xs" color="tertiary">Open Interest</Typography></div>
                <div :class="[$style.subCell, $style.subRight]"><Typography size="text-xs" color="tertiary">24h Volume</Typography></div>
              </div>
              <div v-for="ex in expandedData" :key="ex.exchange" :class="$style.subRow">
                <div :class="$style.subCell">
                  <Typography size="text-sm" weight="medium">{{ displayExchange(ex.exchange) }}</Typography>
                </div>
                <div :class="[$style.subCell, $style.subRight]">
                  <Typography size="text-sm" :style="{ color: fundingColor(ex.funding_rate_apr) }">
                    {{ formatApr(ex.funding_rate_apr) }}
                  </Typography>
                </div>
                <div :class="[$style.subCell, $style.subRight]">
                  <Typography size="text-sm">{{ formatPrice(ex.market_price) }}</Typography>
                </div>
                <div :class="[$style.subCell, $style.subRight]">
                  <Typography size="text-sm">{{ formatUsd(ex.open_interest) }}</Typography>
                </div>
                <div :class="[$style.subCell, $style.subRight]">
                  <Typography size="text-sm">{{ formatUsd(ex.volume_24h) }}</Typography>
                </div>
              </div>
            </div>
          </template>

          <div v-else :class="$style.detailLoading">
            <Typography size="text-sm" color="error">Failed to load exchange data</Typography>
          </div>
        </div>
      </template>
    </div>
  </div>
</template>

<style module>
.page {
  padding: 50px 40px;
  max-width: 1100px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
}

.searchBar {
  display: flex;
}

.searchInput {
  width: 100%;
  max-width: 400px;
  height: 40px;
  padding: 0 var(--space-4);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  color: var(--color-text-primary);
  font-size: var(--text-md);
  outline: none;
  transition: border-color 0.15s;
}
.searchInput::placeholder { color: var(--color-text-tertiary); }
.searchInput:focus { border-color: var(--color-text-secondary); }

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
}

.rowData {
  cursor: pointer;
  transition: background 0.1s;
}
.rowData:hover { background: var(--color-white-4); }
.rowExpanded { background: var(--color-white-4); }

.cell { flex: 1; }
.cellToken { flex: 0 0 120px; }
.cellExchange { flex: 0 0 160px; }
.cellRight {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: flex-end;
}
.cellChevron {
  flex: 0 0 30px;
  display: flex;
  justify-content: flex-end;
}

.exchangeBadges {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  background: var(--color-white-4);
  border: 1px solid var(--color-stroke-divider);
  font-size: 11px;
  color: var(--color-text-secondary);
}

.chevron {
  font-size: 10px;
  color: var(--color-text-tertiary);
}

/* Expanded detail */
.detail {
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-stroke-divider);
  background: var(--color-bg-secondary);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.detailLoading {
  padding: var(--space-4);
  text-align: center;
}

.aggRow {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--space-3);
}

.aggCard {
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  background: var(--color-white-2);
  border: 1px solid var(--color-stroke-divider);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

/* Sub-table for per-exchange data */
.subTable {
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  overflow: hidden;
}

.subRow {
  display: grid;
  grid-template-columns: 1.5fr 1fr 1fr 1fr 1fr;
  padding: var(--space-2) var(--space-4);
  border-bottom: 1px solid var(--color-stroke-divider);
  align-items: center;
}
.subRow:last-child { border-bottom: none; }

.subHeader {
  background: var(--color-bg-secondary);
}

.subCell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.subRight {
  text-align: right;
  align-items: flex-end;
}

.error {
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
