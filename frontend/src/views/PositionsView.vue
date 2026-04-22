<script setup lang="ts">
import { computed, ref, onMounted, onUnmounted } from 'vue'
import { usePortfolioStream } from '@/composables/usePortfolioStream'
import { fetchPortfolioPairs } from '@/lib/api'
import Typography from '@/components/ui/Typography.vue'
import Chip from '@/components/ui/Chip.vue'
import type { PortfolioExchange, DeltaNeutralPair } from '@/types/portfolio'

const { data, connected } = usePortfolioStream(3000)
const activeTab = ref<'positions' | 'pairs'>('positions')
const pairs = ref<DeltaNeutralPair[]>([])
const pairsLoading = ref(false)
const pairsError = ref<string | null>(null)
let pairsPollInterval: ReturnType<typeof setInterval> | null = null

const exchangeList = computed<PortfolioExchange[]>(() => {
  if (!data.value?.exchanges) return []
  return Object.values(data.value.exchanges)
})

const totalPnl = computed(() => {
  return exchangeList.value.reduce((sum, ex) => {
    return sum + ex.positions.reduce((s, p) => s + p.unrealized_pnl, 0)
  }, 0)
})

const totalPositions = computed(() => {
  return exchangeList.value.reduce((sum, ex) => sum + ex.positions.length, 0)
})

const matchedPairs = computed(() => pairs.value.filter(p => p.source !== 'unmatched'))
const unmatchedPairs = computed(() => pairs.value.filter(p => p.source === 'unmatched'))

const unmatchedTotals = computed(() => {
  let unrealized = 0, realized = 0, funding = 0
  for (const pair of unmatchedPairs.value) {
    const pos = pair.long || pair.short
    if (!pos) continue
    unrealized += pos.unrealized_pnl || 0
    realized += pos.realized_pnl || 0
    funding += pos.cumulative_funding || 0
  }
  return { unrealized, realized, total: unrealized + realized, funding }
})

async function loadPairs() {
  pairsLoading.value = true
  pairsError.value = null
  try {
    const resp = await fetchPortfolioPairs()
    pairs.value = resp.pairs
  } catch (e) {
    pairsError.value = e instanceof Error ? e.message : 'Failed to load pairs'
  } finally {
    pairsLoading.value = false
  }
}

onMounted(async () => {
  await loadPairs()
  pairsPollInterval = setInterval(loadPairs, 10000)
})

onUnmounted(() => {
  if (pairsPollInterval) clearInterval(pairsPollInterval)
})

function formatUsd(val: number | string | undefined | null): string {
  if (val == null) return '—'
  const n = Number(val) || 0
  const sign = n >= 0 ? '+' : ''
  return `${sign}$${n.toFixed(2)}`
}

function formatPrice(val: number | string): string {
  const n = Number(val)
  if (!n) return '—'
  if (n >= 1000) return `$${n.toFixed(2)}`
  if (n >= 1) return `$${n.toFixed(4)}`
  return `$${n.toFixed(6)}`
}

function formatFunding(rate: number | string): string {
  const r = Number(rate)
  if (!r) return '—'
  const pct = r * 100
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(4)}%`
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

function sourceLabel(source: string): string {
  if (source.startsWith('bot:')) return `Bot: ${source.slice(4)}`
  if (source === 'token-match') return 'Token Match'
  return 'Unmatched'
}

function sourceVariant(source: string): 'success' | 'neutral' | 'error' {
  if (source.startsWith('bot:')) return 'success'
  if (source === 'token-match') return 'neutral'
  return 'error'
}
</script>

<template>
  <div :class="$style.page">
    <!-- Header -->
    <div :class="$style.header">
      <div>
        <Typography size="text-h5" weight="semibold" font="bricolage">Portfolio Positions</Typography>
        <div :class="$style.headerSub">
          <Typography size="text-sm" color="tertiary">{{ totalPositions }} open position{{ totalPositions !== 1 ? 's' : '' }}</Typography>
          <span :class="[$style.dot, connected ? $style.dotOn : $style.dotOff]" />
          <Typography size="text-xs" :color="connected ? 'success' : 'error'">{{ connected ? 'Live' : 'Disconnected' }}</Typography>
        </div>
      </div>
      <div :class="$style.totalPnl">
        <Typography size="text-sm" color="tertiary">Total uPnL</Typography>
        <Typography
          size="text-h4"
          weight="bold"
          :color="totalPnl >= 0 ? 'success' : 'error'"
        >{{ formatUsd(totalPnl) }}</Typography>
      </div>
    </div>

    <!-- Tab Toggle -->
    <div :class="$style.tabs">
      <button
        :class="[$style.tab, activeTab === 'positions' && $style.tabActive]"
        @click="activeTab = 'positions'"
      >Positions</button>
      <button
        :class="[$style.tab, activeTab === 'pairs' && $style.tabActive]"
        @click="activeTab = 'pairs'"
      >Delta-Neutral Pairs</button>
    </div>

    <!-- ===== POSITIONS TAB ===== -->
    <template v-if="activeTab === 'positions'">
      <!-- Exchange Cards -->
      <div v-if="exchangeList.length" :class="$style.exchangeCards">
        <div
          v-for="ex in exchangeList"
          :key="ex.exchange"
          :class="$style.exchangeCard"
        >
          <div :class="$style.ecHeader">
            <Typography size="text-lg" weight="semibold" :style="{ color: exchangeColor(ex.exchange) }">
              {{ ex.exchange.toUpperCase() }}
            </Typography>
            <Chip v-if="ex.error" variant="error" size="sm">Error</Chip>
            <Chip v-else variant="neutral" size="sm">{{ ex.positions.length }} pos</Chip>
          </div>
          <div :class="$style.ecStats">
            <div :class="$style.ecStat">
              <Typography size="text-xs" color="tertiary">Equity</Typography>
              <Typography size="text-md" weight="medium">${{ ex.equity != null ? Number(ex.equity).toFixed(2) : '0.00' }}</Typography>
            </div>
            <div :class="$style.ecStat">
              <Typography size="text-xs" color="tertiary">uPnL</Typography>
              <Typography
                size="text-md"
                weight="medium"
                :color="(ex.unrealized_pnl || 0) >= 0 ? 'success' : 'error'"
              >{{ formatUsd(ex.unrealized_pnl || 0) }}</Typography>
            </div>
          </div>
          <Typography v-if="ex.error" size="text-xs" color="error" :class="$style.ecError">{{ ex.error }}</Typography>
        </div>
      </div>

      <!-- Positions: Desktop Table -->
      <div v-for="ex in exchangeList" :key="'t-' + ex.exchange" :class="$style.tableSection">
        <div v-if="ex.positions.length" :class="$style.tableSectionHeader">
          <Typography size="text-md" weight="semibold" :style="{ color: exchangeColor(ex.exchange) }">
            {{ ex.exchange.toUpperCase() }}
          </Typography>
          <Typography size="text-xs" color="tertiary">{{ ex.positions.length }} position{{ ex.positions.length !== 1 ? 's' : '' }}</Typography>
        </div>

        <!-- Desktop Table -->
        <div v-if="ex.positions.length" :class="$style.tableWrap">
          <table :class="$style.table">
            <thead>
              <tr>
                <th>Token</th>
                <th>Side</th>
                <th>Size</th>
                <th>Entry Price</th>
                <th>Mark Price</th>
                <th>uPnL</th>
                <th>rPnL</th>
                <th>Total PnL</th>
                <th>Funding Paid</th>
                <th>Leverage</th>
                <th>Funding Rate</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(pos, i) in ex.positions" :key="i">
                <td>
                  <div :class="$style.tokenCell">
                    <Typography size="text-sm" weight="semibold">{{ pos.token }}</Typography>
                    <Typography size="text-xs" color="tertiary">{{ pos.instrument }}</Typography>
                  </div>
                </td>
                <td>
                  <Chip :variant="pos.side === 'LONG' ? 'long' : 'short'" size="sm">
                    {{ pos.side }}
                  </Chip>
                </td>
                <td>
                  <Typography size="text-sm">{{ pos.size }}</Typography>
                </td>
                <td>
                  <Typography size="text-sm" color="secondary">{{ formatPrice(pos.entry_price) }}</Typography>
                </td>
                <td>
                  <Typography size="text-sm">{{ formatPrice(pos.mark_price) }}</Typography>
                </td>
                <td>
                  <Typography
                    size="text-sm"
                    weight="medium"
                    :color="pos.unrealized_pnl >= 0 ? 'success' : 'error'"
                  >{{ formatUsd(pos.unrealized_pnl) }}</Typography>
                </td>
                <td>
                  <Typography
                    size="text-sm"
                    :color="pos.realized_pnl > 0 ? 'success' : pos.realized_pnl < 0 ? 'error' : 'secondary'"
                  >{{ pos.realized_pnl ? formatUsd(pos.realized_pnl) : '—' }}</Typography>
                </td>
                <td>
                  <Typography
                    size="text-sm"
                    weight="semibold"
                    :color="pos.total_pnl >= 0 ? 'success' : 'error'"
                  >{{ formatUsd(pos.total_pnl) }}</Typography>
                </td>
                <td>
                  <Typography
                    size="text-sm"
                    :color="pos.cumulative_funding > 0 ? 'success' : pos.cumulative_funding < 0 ? 'error' : 'secondary'"
                  >{{ pos.cumulative_funding ? formatUsd(pos.cumulative_funding) : '—' }}</Typography>
                </td>
                <td>
                  <Typography size="text-sm" color="secondary">{{ pos.leverage ? `${pos.leverage}x` : '—' }}</Typography>
                </td>
                <td>
                  <Typography
                    size="text-sm"
                    :color="pos.funding_rate > 0 ? 'error' : pos.funding_rate < 0 ? 'success' : 'secondary'"
                  >{{ formatFunding(pos.funding_rate) }}</Typography>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- Mobile Cards -->
        <div v-if="ex.positions.length" :class="$style.mobileCards">
          <div
            v-for="(pos, i) in ex.positions"
            :key="i"
            :class="$style.positionCard"
          >
            <div :class="$style.cardHeader">
              <div :class="$style.cardTitle">
                <Typography size="text-md" weight="semibold">{{ pos.token }}</Typography>
                <Chip :variant="pos.side === 'LONG' ? 'long' : 'short'" size="sm">
                  {{ pos.side }}
                </Chip>
              </div>
              <Typography size="text-xs" color="tertiary">{{ pos.instrument }}</Typography>
            </div>

            <div :class="$style.cardBody">
              <div :class="$style.cardRow">
                <span :class="$style.cardLabel">Size</span>
                <Typography size="text-sm" weight="medium">{{ pos.size }}</Typography>
              </div>
              <div :class="$style.cardRow">
                <span :class="$style.cardLabel">Entry</span>
                <Typography size="text-sm" color="secondary">{{ formatPrice(pos.entry_price) }}</Typography>
              </div>
              <div :class="$style.cardRow">
                <span :class="$style.cardLabel">Mark</span>
                <Typography size="text-sm">{{ formatPrice(pos.mark_price) }}</Typography>
              </div>
              <div :class="$style.cardRow">
                <span :class="$style.cardLabel">Leverage</span>
                <Typography size="text-sm" color="secondary">{{ pos.leverage ? `${pos.leverage}x` : '—' }}</Typography>
              </div>
            </div>

            <div :class="$style.cardFooter">
              <div :class="$style.cardStat">
                <span :class="$style.cardLabel">uPnL</span>
                <Typography
                  size="text-md"
                  weight="bold"
                  :color="pos.unrealized_pnl >= 0 ? 'success' : 'error'"
                >{{ formatUsd(pos.unrealized_pnl) }}</Typography>
              </div>
              <div :class="$style.cardStat">
                <span :class="$style.cardLabel">Total PnL</span>
                <Typography
                  size="text-md"
                  weight="bold"
                  :color="pos.total_pnl >= 0 ? 'success' : 'error'"
                >{{ formatUsd(pos.total_pnl) }}</Typography>
              </div>
              <div :class="$style.cardStat">
                <span :class="$style.cardLabel">Funding</span>
                <Typography
                  size="text-sm"
                  :color="pos.cumulative_funding > 0 ? 'success' : pos.cumulative_funding < 0 ? 'error' : 'secondary'"
                >{{ pos.cumulative_funding ? formatUsd(pos.cumulative_funding) : '—' }}</Typography>
              </div>
            </div>

            <div v-if="pos.funding_rate" :class="$style.cardRate">
              <Typography size="text-xs" color="tertiary">Funding Rate</Typography>
              <Typography
                size="text-sm"
                :color="pos.funding_rate > 0 ? 'error' : 'success'"
              >{{ formatFunding(pos.funding_rate) }}</Typography>
            </div>
          </div>
        </div>
      </div>

      <!-- Empty state -->
      <div v-if="connected && totalPositions === 0" :class="$style.empty">
        <Typography color="tertiary">No open positions across any exchange</Typography>
      </div>
    </template>

    <!-- ===== PAIRS TAB ===== -->
    <template v-if="activeTab === 'pairs'">
      <div v-if="pairsLoading && !pairs.length" :class="$style.empty">
        <Typography color="secondary">Loading pairs...</Typography>
      </div>

      <div v-if="pairsError" :class="$style.empty">
        <Typography color="error">{{ pairsError }}</Typography>
      </div>

      <!-- Matched Pairs: Desktop -->
      <div v-if="matchedPairs.length" :class="$style.pairsGrid">
        <div
          v-for="(pair, idx) in matchedPairs"
          :key="'pair-' + idx"
          :class="$style.pairCard"
        >
          <div :class="$style.pairHeader">
            <Typography size="text-lg" weight="bold">{{ pair.token }}</Typography>
            <Chip :variant="sourceVariant(pair.source)" size="sm">{{ sourceLabel(pair.source) }}</Chip>
          </div>

          <!-- Desktop Pair Table -->
          <div :class="$style.tableWrap">
            <table :class="$style.table">
              <thead>
                <tr>
                  <th>Side</th>
                  <th>Exchange</th>
                  <th>Size</th>
                  <th>Entry</th>
                  <th>Mark</th>
                  <th>uPnL</th>
                  <th>rPnL</th>
                  <th>Funding Paid</th>
                  <th>Rate</th>
                </tr>
              </thead>
              <tbody>
                <tr v-if="pair.long">
                  <td><Chip variant="long" size="sm">LONG</Chip></td>
                  <td><Typography size="text-sm" :style="{ color: exchangeColor(pair.long.exchange) }">{{ pair.long.exchange }}</Typography></td>
                  <td><Typography size="text-sm">{{ pair.long.size }}</Typography></td>
                  <td><Typography size="text-sm" color="secondary">{{ formatPrice(pair.long.entry_price) }}</Typography></td>
                  <td><Typography size="text-sm">{{ formatPrice(pair.long.mark_price) }}</Typography></td>
                  <td><Typography size="text-sm" weight="medium" :color="pair.long.unrealized_pnl >= 0 ? 'success' : 'error'">{{ formatUsd(pair.long.unrealized_pnl) }}</Typography></td>
                  <td><Typography size="text-sm" :color="pair.long.realized_pnl > 0 ? 'success' : pair.long.realized_pnl < 0 ? 'error' : 'secondary'">{{ pair.long.realized_pnl ? formatUsd(pair.long.realized_pnl) : '—' }}</Typography></td>
                  <td><Typography size="text-sm" :color="pair.long.cumulative_funding > 0 ? 'success' : pair.long.cumulative_funding < 0 ? 'error' : 'secondary'">{{ pair.long.cumulative_funding ? formatUsd(pair.long.cumulative_funding) : '—' }}</Typography></td>
                  <td><Typography size="text-sm" color="secondary">{{ formatFunding(pair.long.funding_rate) }}</Typography></td>
                </tr>
                <tr v-if="pair.short">
                  <td><Chip variant="short" size="sm">SHORT</Chip></td>
                  <td><Typography size="text-sm" :style="{ color: exchangeColor(pair.short.exchange) }">{{ pair.short.exchange }}</Typography></td>
                  <td><Typography size="text-sm">{{ pair.short.size }}</Typography></td>
                  <td><Typography size="text-sm" color="secondary">{{ formatPrice(pair.short.entry_price) }}</Typography></td>
                  <td><Typography size="text-sm">{{ formatPrice(pair.short.mark_price) }}</Typography></td>
                  <td><Typography size="text-sm" weight="medium" :color="pair.short.unrealized_pnl >= 0 ? 'success' : 'error'">{{ formatUsd(pair.short.unrealized_pnl) }}</Typography></td>
                  <td><Typography size="text-sm" :color="pair.short.realized_pnl > 0 ? 'success' : pair.short.realized_pnl < 0 ? 'error' : 'secondary'">{{ pair.short.realized_pnl ? formatUsd(pair.short.realized_pnl) : '—' }}</Typography></td>
                  <td><Typography size="text-sm" :color="pair.short.cumulative_funding > 0 ? 'success' : pair.short.cumulative_funding < 0 ? 'error' : 'secondary'">{{ pair.short.cumulative_funding ? formatUsd(pair.short.cumulative_funding) : '—' }}</Typography></td>
                  <td><Typography size="text-sm" color="secondary">{{ formatFunding(pair.short.funding_rate) }}</Typography></td>
                </tr>
              </tbody>
            </table>
          </div>

          <!-- Mobile Pair Cards -->
          <div :class="$style.mobilePairCards">
            <!-- Long Position -->
            <div v-if="pair.long" :class="$style.positionCard">
              <div :class="$style.cardHeader">
                <div :class="$style.cardTitle">
                  <Typography size="text-md" weight="semibold">LONG</Typography>
                  <Chip variant="long" size="sm">LONG</Chip>
                </div>
                <Typography size="text-xs" color="tertiary" :style="{ color: exchangeColor(pair.long.exchange) }">
                  {{ pair.long.exchange.toUpperCase() }}
                </Typography>
              </div>
              <div :class="$style.cardBody">
                <div :class="$style.cardRow">
                  <span :class="$style.cardLabel">Size</span>
                  <Typography size="text-sm">{{ pair.long.size }}</Typography>
                </div>
                <div :class="$style.cardRow">
                  <span :class="$style.cardLabel">Entry</span>
                  <Typography size="text-sm" color="secondary">{{ formatPrice(pair.long.entry_price) }}</Typography>
                </div>
                <div :class="$style.cardRow">
                  <span :class="$style.cardLabel">Mark</span>
                  <Typography size="text-sm">{{ formatPrice(pair.long.mark_price) }}</Typography>
                </div>
              </div>
              <div :class="$style.cardFooter">
                <div :class="$style.cardStat">
                  <span :class="$style.cardLabel">uPnL</span>
                  <Typography size="text-sm" weight="bold" :color="pair.long.unrealized_pnl >= 0 ? 'success' : 'error'">{{ formatUsd(pair.long.unrealized_pnl) }}</Typography>
                </div>
                <div :class="$style.cardStat">
                  <span :class="$style.cardLabel">Funding</span>
                  <Typography size="text-xs" :color="pair.long.cumulative_funding > 0 ? 'success' : 'error'">{{ formatUsd(pair.long.cumulative_funding) }}</Typography>
                </div>
              </div>
            </div>

            <!-- Short Position -->
            <div v-if="pair.short" :class="$style.positionCard">
              <div :class="$style.cardHeader">
                <div :class="$style.cardTitle">
                  <Typography size="text-md" weight="semibold">SHORT</Typography>
                  <Chip variant="short" size="sm">SHORT</Chip>
                </div>
                <Typography size="text-xs" color="tertiary" :style="{ color: exchangeColor(pair.short.exchange) }">
                  {{ pair.short.exchange.toUpperCase() }}
                </Typography>
              </div>
              <div :class="$style.cardBody">
                <div :class="$style.cardRow">
                  <span :class="$style.cardLabel">Size</span>
                  <Typography size="text-sm">{{ pair.short.size }}</Typography>
                </div>
                <div :class="$style.cardRow">
                  <span :class="$style.cardLabel">Entry</span>
                  <Typography size="text-sm" color="secondary">{{ formatPrice(pair.short.entry_price) }}</Typography>
                </div>
                <div :class="$style.cardRow">
                  <span :class="$style.cardLabel">Mark</span>
                  <Typography size="text-sm">{{ formatPrice(pair.short.mark_price) }}</Typography>
                </div>
              </div>
              <div :class="$style.cardFooter">
                <div :class="$style.cardStat">
                  <span :class="$style.cardLabel">uPnL</span>
                  <Typography size="text-sm" weight="bold" :color="pair.short.unrealized_pnl >= 0 ? 'success' : 'error'">{{ formatUsd(pair.short.unrealized_pnl) }}</Typography>
                </div>
                <div :class="$style.cardStat">
                  <span :class="$style.cardLabel">Funding</span>
                  <Typography size="text-xs" :color="pair.short.cumulative_funding > 0 ? 'success' : 'error'">{{ formatUsd(pair.short.cumulative_funding) }}</Typography>
                </div>
              </div>
            </div>
          </div>

          <!-- Combined PnL footer -->
          <div :class="$style.pairFooter">
            <div :class="$style.pairStat">
              <Typography size="text-xs" color="tertiary">Combined uPnL</Typography>
              <Typography size="text-md" weight="bold" :color="pair.combined_pnl.unrealized >= 0 ? 'success' : 'error'">{{ formatUsd(pair.combined_pnl.unrealized) }}</Typography>
            </div>
            <div :class="$style.pairStat">
              <Typography size="text-xs" color="tertiary">Combined rPnL</Typography>
              <Typography size="text-md" weight="bold" :color="pair.combined_pnl.realized >= 0 ? 'success' : 'error'">{{ formatUsd(pair.combined_pnl.realized) }}</Typography>
            </div>
            <div :class="$style.pairStat">
              <Typography size="text-xs" color="tertiary">Net Funding</Typography>
              <Typography size="text-md" weight="bold" :color="pair.combined_pnl.funding_net >= 0 ? 'success' : 'error'">{{ formatUsd(pair.combined_pnl.funding_net) }}</Typography>
            </div>
            <div :class="$style.pairStat">
              <Typography size="text-xs" color="tertiary">Total PnL</Typography>
              <Typography size="text-lg" weight="bold" :color="pair.combined_pnl.total >= 0 ? 'success' : 'error'">{{ formatUsd(pair.combined_pnl.total) }}</Typography>
            </div>
          </div>
        </div>
      </div>

      <!-- Unmatched positions: Desktop -->
      <div v-if="unmatchedPairs.length" :class="$style.tableSection">
        <div :class="$style.tableSectionHeader">
          <Typography size="text-md" weight="semibold">Unmatched Positions</Typography>
          <Typography size="text-xs" color="tertiary">{{ unmatchedPairs.length }} position{{ unmatchedPairs.length !== 1 ? 's' : '' }}</Typography>
        </div>
        <div :class="$style.tableWrap">
          <table :class="$style.table">
            <thead>
              <tr>
                <th>Token</th>
                <th>Exchange</th>
                <th>Side</th>
                <th>Size</th>
                <th>Entry</th>
                <th>Mark</th>
                <th>uPnL</th>
                <th>rPnL</th>
                <th>Total PnL</th>
                <th>Funding Paid</th>
                <th>Rate</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(pair, idx) in unmatchedPairs" :key="'um-' + idx">
                <template v-if="pair.long || pair.short">
                  <td><Typography size="text-sm" weight="semibold">{{ pair.token }}</Typography></td>
                  <td><Typography size="text-sm" :style="{ color: exchangeColor((pair.long || pair.short)!.exchange) }">{{ (pair.long || pair.short)!.exchange }}</Typography></td>
                  <td><Chip :variant="(pair.long || pair.short)!.side === 'LONG' ? 'long' : 'short'" size="sm">{{ (pair.long || pair.short)!.side }}</Chip></td>
                  <td><Typography size="text-sm">{{ (pair.long || pair.short)!.size }}</Typography></td>
                  <td><Typography size="text-sm" color="secondary">{{ formatPrice((pair.long || pair.short)!.entry_price) }}</Typography></td>
                  <td><Typography size="text-sm">{{ formatPrice((pair.long || pair.short)!.mark_price) }}</Typography></td>
                  <td><Typography size="text-sm" weight="medium" :color="(pair.long || pair.short)!.unrealized_pnl >= 0 ? 'success' : 'error'">{{ formatUsd((pair.long || pair.short)!.unrealized_pnl) }}</Typography></td>
                  <td><Typography size="text-sm" :color="((pair.long || pair.short)!.realized_pnl || 0) > 0 ? 'success' : ((pair.long || pair.short)!.realized_pnl || 0) < 0 ? 'error' : 'secondary'">{{ formatUsd((pair.long || pair.short)!.realized_pnl) }}</Typography></td>
                  <td>
                    <Typography
                      size="text-sm"
                      weight="semibold"
                      :color="((pair.long || pair.short)!.unrealized_pnl + ((pair.long || pair.short)!.realized_pnl || 0)) >= 0 ? 'success' : 'error'"
                    >{{ formatUsd((pair.long || pair.short)!.unrealized_pnl + ((pair.long || pair.short)!.realized_pnl || 0)) }}</Typography>
                  </td>
                  <td><Typography size="text-sm" :color="((pair.long || pair.short)!.cumulative_funding || 0) > 0 ? 'success' : ((pair.long || pair.short)!.cumulative_funding || 0) < 0 ? 'error' : 'secondary'">{{ formatUsd((pair.long || pair.short)!.cumulative_funding) }}</Typography></td>
                  <td><Typography size="text-sm" color="secondary">{{ formatFunding((pair.long || pair.short)!.funding_rate) }}</Typography></td>
                </template>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- Unmatched Mobile Cards -->
        <div :class="$style.mobileCards">
          <div
            v-for="(pair, idx) in unmatchedPairs"
            :key="'um-mobile-' + idx"
            :class="$style.positionCard"
          >
            <div v-if="pair.long || pair.short" :class="$style.cardContent">
              <div :class="$style.cardHeader">
                <div :class="$style.cardTitle">
                  <Typography size="text-md" weight="semibold">{{ pair.token }}</Typography>
                  <Chip :variant="(pair.long || pair.short)!.side === 'LONG' ? 'long' : 'short'" size="sm">
                    {{ (pair.long || pair.short)!.side }}
                  </Chip>
                </div>
                <Typography size="text-xs" color="tertiary" :style="{ color: exchangeColor((pair.long || pair.short)!.exchange) }">
                  {{ (pair.long || pair.short)!.exchange.toUpperCase() }}
                </Typography>
              </div>
              <div :class="$style.cardBody">
                <div :class="$style.cardRow">
                  <span :class="$style.cardLabel">Size</span>
                  <Typography size="text-sm">{{ (pair.long || pair.short)!.size }}</Typography>
                </div>
                <div :class="$style.cardRow">
                  <span :class="$style.cardLabel">Entry</span>
                  <Typography size="text-sm" color="secondary">{{ formatPrice((pair.long || pair.short)!.entry_price) }}</Typography>
                </div>
                <div :class="$style.cardRow">
                  <span :class="$style.cardLabel">Mark</span>
                  <Typography size="text-sm">{{ formatPrice((pair.long || pair.short)!.mark_price) }}</Typography>
                </div>
              </div>
              <div :class="$style.cardFooter">
                <div :class="$style.cardStat">
                  <span :class="$style.cardLabel">uPnL</span>
                  <Typography size="text-sm" weight="bold" :color="((pair.long || pair.short)!.unrealized_pnl || 0) >= 0 ? 'success' : 'error'">
                    {{ formatUsd((pair.long || pair.short)!.unrealized_pnl) }}
                  </Typography>
                </div>
                <div :class="$style.cardStat">
                  <span :class="$style.cardLabel">Total</span>
                  <Typography size="text-sm" weight="bold" :color="((pair.long || pair.short)!.unrealized_pnl + ((pair.long || pair.short)!.realized_pnl || 0)) >= 0 ? 'success' : 'error'">
                    {{ formatUsd((pair.long || pair.short)!.unrealized_pnl + ((pair.long || pair.short)!.realized_pnl || 0)) }}
                  </Typography>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Unmatched summary footer -->
        <div :class="$style.pairFooter">
          <div :class="$style.pairStat">
            <Typography size="text-xs" color="tertiary">Total uPnL</Typography>
            <Typography size="text-md" weight="bold" :color="unmatchedTotals.unrealized >= 0 ? 'success' : 'error'">{{ formatUsd(unmatchedTotals.unrealized) }}</Typography>
          </div>
          <div :class="$style.pairStat">
            <Typography size="text-xs" color="tertiary">Total rPnL</Typography>
            <Typography size="text-md" weight="bold" :color="unmatchedTotals.realized >= 0 ? 'success' : 'error'">{{ formatUsd(unmatchedTotals.realized) }}</Typography>
          </div>
          <div :class="$style.pairStat">
            <Typography size="text-xs" color="tertiary">Total Funding</Typography>
            <Typography size="text-md" weight="bold" :color="unmatchedTotals.funding >= 0 ? 'success' : 'error'">{{ formatUsd(unmatchedTotals.funding) }}</Typography>
          </div>
          <div :class="$style.pairStat">
            <Typography size="text-xs" color="tertiary">Total PnL</Typography>
            <Typography size="text-lg" weight="bold" :color="unmatchedTotals.total >= 0 ? 'success' : 'error'">{{ formatUsd(unmatchedTotals.total) }}</Typography>
          </div>
        </div>
      </div>

      <div v-if="!pairsLoading && !pairs.length && !pairsError" :class="$style.empty">
        <Typography color="tertiary">No positions to pair</Typography>
      </div>
    </template>

    <!-- Loading state -->
    <div v-if="!data && activeTab === 'positions'" :class="$style.empty">
      <Typography color="secondary">Connecting to portfolio stream...</Typography>
    </div>
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

.headerSub {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-top: var(--space-1);
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
}

.dotOn {
  background: var(--color-success);
  box-shadow: 0 0 6px var(--color-success);
}

.dotOff {
  background: var(--color-error);
}

.totalPnl {
  text-align: right;
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
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
  min-height: var(--touch-target-min);
}

.tab:hover {
  color: var(--color-text-primary);
}

.tabActive {
  background: var(--color-white-2);
  color: var(--color-text-primary);
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.08);
}

.exchangeCards {
  display: flex;
  gap: var(--space-4);
  flex-wrap: wrap;
}

.exchangeCard {
  flex: 1;
  min-width: 200px;
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.ecHeader {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.ecStats {
  display: flex;
  gap: var(--space-6);
}

.ecStat {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.ecError {
  word-break: break-all;
}

.tableSection {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.tableSectionHeader {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.tableWrap {
  border-radius: var(--radius-lg);
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

.table tr:hover td {
  background: var(--color-white-4);
}

.tokenCell {
  display: flex;
  flex-direction: column;
  gap: 1px;
}

.empty {
  padding: var(--space-10) 0;
  text-align: center;
}

.pairsGrid {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}

.pairCard {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  overflow: hidden;
}

.pairHeader {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4) var(--space-5);
  border-bottom: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
}

.pairFooter {
  display: flex;
  gap: var(--space-8);
  padding: var(--space-4) var(--space-5);
  border-top: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  flex-wrap: wrap;
}

.pairStat {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

/* ===== MOBILE POSITION CARDS ===== */
.mobileCards {
  display: none;
  flex-direction: column;
  gap: var(--space-3);
}

.positionCard {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  overflow: hidden;
}

.cardHeader {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--space-4);
  border-bottom: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
}

.cardTitle {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.cardBody {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: var(--space-3);
  padding: var(--space-4);
}

.cardRow {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cardLabel {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.02em;
}

.cardFooter {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--space-3);
  padding: var(--space-4);
  border-top: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
}

.cardStat {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cardRate {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--space-3) var(--space-4);
  border-top: 1px solid var(--color-stroke-divider);
}

/* Mobile Pair Cards (hidden on desktop) */
.mobilePairCards {
  display: none;
  flex-direction: column;
  gap: var(--space-3);
  padding: var(--space-4);
}

/* ===== RESPONSIVE BREAKPOINTS ===== */

/* Tablet */
@media (max-width: 1024px) {
  .page {
    padding: 24px 20px;
  }

  .exchangeCards {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
  }

  .exchangeCard {
    min-width: auto;
  }
}

/* Mobile */
@media (max-width: 767px) {
  .page {
    padding: 16px;
    gap: var(--space-4);
  }

  .header {
    flex-direction: column;
    gap: var(--space-4);
  }

  .totalPnl {
    text-align: left;
  }

  .tabs {
    width: 100%;
  }

  .tab {
    flex: 1;
    text-align: center;
    padding: var(--space-3) var(--space-2);
  }

  .exchangeCards {
    grid-template-columns: 1fr;
  }

  /* Hide desktop table on mobile */
  .tableWrap {
    display: none;
  }

  /* Show mobile cards */
  .mobileCards {
    display: flex;
  }

  /* Show mobile pair cards */
  .mobilePairCards {
    display: flex;
  }

  .pairFooter {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: var(--space-4);
  }
}

/* Small mobile */
@media (max-width: 480px) {
  .cardBody {
    grid-template-columns: 1fr;
  }

  .cardFooter {
    grid-template-columns: 1fr;
    gap: var(--space-3);
  }

  .pairFooter {
    grid-template-columns: 1fr;
  }
}
</style>
