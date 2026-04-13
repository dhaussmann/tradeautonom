<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { useAccountStore } from '@/stores/account'
import Typography from '@/components/ui/Typography.vue'
import Chip from '@/components/ui/Chip.vue'

const accountStore = useAccountStore()
let pollInterval: ReturnType<typeof setInterval> | null = null

function formatUsd(val: number | string | undefined): string {
  if (val === undefined || val === null) return '—'
  const n = Number(val) || 0
  const sign = n >= 0 ? '+' : ''
  return `${sign}$${n.toFixed(2)}`
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

onMounted(async () => {
  await Promise.all([
    accountStore.loadAccounts(),
    accountStore.loadPositions(),
  ])
  pollInterval = setInterval(() => {
    accountStore.loadAccounts()
    accountStore.loadPositions()
  }, 10000)
})

onUnmounted(() => {
  if (pollInterval) clearInterval(pollInterval)
})
</script>

<template>
  <div :class="$style.page">
    <Typography size="text-h5" weight="semibold" font="bricolage">Account & Positions</Typography>

    <!-- Account Summary Cards -->
    <div v-if="accountStore.accounts.length" :class="$style.accounts">
      <div
        v-for="acc in accountStore.accounts"
        :key="acc.exchange"
        :class="$style.accountCard"
      >
        <div :class="$style.accountHeader">
          <Typography size="text-lg" weight="semibold" :style="{ color: exchangeColor(acc.exchange) }">
            {{ acc.exchange.toUpperCase() }}
          </Typography>
        </div>
        <div :class="$style.accountStats">
          <div :class="$style.stat">
            <Typography size="text-sm" color="tertiary">Equity</Typography>
            <Typography size="text-h6" weight="medium">${{ acc.equity != null ? Number(acc.equity).toFixed(2) : '—' }}</Typography>
          </div>
          <div :class="$style.stat">
            <Typography size="text-sm" color="tertiary">uPnL</Typography>
            <Typography
              size="text-h6"
              weight="medium"
              :color="Number(acc.unrealized_pnl || 0) >= 0 ? 'success' : 'error'"
            >{{ formatUsd(acc.unrealized_pnl) }}</Typography>
          </div>
        </div>
      </div>
    </div>

    <!-- Positions Table -->
    <div :class="$style.tableSection">
      <Typography size="text-md" weight="semibold" color="secondary" as="h3">Open Positions</Typography>

      <div v-if="accountStore.loading && !accountStore.positions.length" :class="$style.empty">
        <Typography color="secondary">Loading...</Typography>
      </div>
      <div v-else-if="!accountStore.positions.length" :class="$style.empty">
        <Typography color="tertiary">No open positions</Typography>
      </div>

      <div v-else :class="$style.tableWrap">
        <table :class="$style.table">
          <thead>
            <tr>
              <th>Exchange</th>
              <th>Instrument</th>
              <th>Side</th>
              <th>Size</th>
              <th>Entry</th>
              <th>Mark</th>
              <th>uPnL</th>
              <th>Leverage</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(pos, i) in accountStore.positions" :key="i">
              <td>
                <Typography size="text-sm" :style="{ color: exchangeColor(pos.exchange) }">
                  {{ pos.exchange }}
                </Typography>
              </td>
              <td>
                <Typography size="text-sm">{{ pos.instrument }}</Typography>
              </td>
              <td>
                <Chip :variant="Number(pos.size) > 0 || pos.side === 'LONG' ? 'long' : 'short'" size="sm">
                  {{ Number(pos.size) > 0 || pos.side === 'LONG' ? 'LONG' : 'SHORT' }}
                </Chip>
              </td>
              <td>
                <Typography size="text-sm">{{ Math.abs(Number(pos.size)) }}</Typography>
              </td>
              <td>
                <Typography size="text-sm" color="secondary">${{ pos.entry_price != null ? Number(pos.entry_price).toFixed(4) : '—' }}</Typography>
              </td>
              <td>
                <Typography size="text-sm" color="secondary">${{ pos.mark_price != null ? Number(pos.mark_price).toFixed(4) : '—' }}</Typography>
              </td>
              <td>
                <Typography
                  size="text-sm"
                  :color="Number(pos.unrealized_pnl || 0) >= 0 ? 'success' : 'error'"
                >{{ formatUsd(pos.unrealized_pnl) }}</Typography>
              </td>
              <td>
                <Typography size="text-sm" color="secondary">{{ pos.leverage || '—' }}×</Typography>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Error -->
    <div v-if="accountStore.error" :class="$style.error">
      <Typography size="text-sm" color="error">{{ accountStore.error }}</Typography>
    </div>
  </div>
</template>

<style module>
.page {
  padding: 50px 40px;
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

.accounts {
  display: flex;
  gap: var(--space-5);
}

.accountCard {
  flex: 1;
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.accountHeader {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.accountStats {
  display: flex;
  gap: var(--space-6);
}

.stat {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.tableSection {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.tableWrap {
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  overflow: hidden;
}

.table {
  width: 100%;
  border-collapse: collapse;
}

.table th {
  text-align: left;
  padding: var(--space-3) var(--space-4);
  font-size: var(--text-sm);
  font-weight: 500;
  color: var(--color-text-tertiary);
  border-bottom: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
}

.table td {
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-stroke-divider);
}

.table tr:last-child td {
  border-bottom: none;
}

.table tr:hover td {
  background: var(--color-white-4);
}

.empty {
  padding: var(--space-10) 0;
  text-align: center;
}

.error {
  padding: var(--space-3) var(--space-4);
  background: var(--color-error-bg);
  border: 1px solid var(--color-error-stroke);
  border-radius: var(--radius-md);
}
</style>
