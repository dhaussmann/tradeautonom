<script setup lang="ts">
import { ref, watch, onMounted } from 'vue'
import { fetchMarketsBySymbol, type MarketEntry } from '@/lib/defi-api'
import { useBotsStore } from '@/stores/bots'
import Typography from '@/components/ui/Typography.vue'

const botsStore = useBotsStore()

interface FundingEntry {
  token: string
  longExchange: string
  shortExchange: string
  longRate: number | null
  shortRate: number | null
  spread: number | null
}

const entries = ref<FundingEntry[]>([])
const loading = ref(false)

function extractToken(symbol: string, exchange: string): string {
  let s = symbol
  if (exchange === 'variational') {
    s = s.replace(/^P-/, '')
    s = s.replace(/-USDC.*$/, '')
  } else if (exchange === 'extended') {
    s = s.replace(/-USD$/, '')
  } else if (exchange === 'grvt') {
    s = s.replace(/_USDT_Perp$/, '')
  }
  return s.toUpperCase()
}

function displayExchange(ex: string): string {
  if (ex === 'grvt') return 'GRVT'
  return ex.charAt(0).toUpperCase() + ex.slice(1)
}

async function loadFunding() {
  const bots = botsStore.bots
  if (!bots.length) { entries.value = []; return }

  // Deduplicate tokens
  const seen = new Set<string>()
  const requests: { token: string; longExchange: string; shortExchange: string }[] = []
  for (const bot of bots) {
    const token = extractToken(bot.instrument_a, bot.long_exchange)
    if (!token || seen.has(token)) continue
    seen.add(token)
    requests.push({ token, longExchange: bot.long_exchange, shortExchange: bot.short_exchange })
  }

  loading.value = true
  const results: FundingEntry[] = []
  await Promise.allSettled(requests.map(async (req) => {
    try {
      const data: MarketEntry[] = await fetchMarketsBySymbol(req.token)
      const longEx = data.find(e => e.exchange === req.longExchange)
      const shortEx = data.find(e => e.exchange === req.shortExchange)
      const longRate = longEx?.funding_rate_apr ?? null
      const shortRate = shortEx?.funding_rate_apr ?? null
      results.push({
        token: req.token,
        longExchange: req.longExchange,
        shortExchange: req.shortExchange,
        longRate,
        shortRate,
        spread: longRate !== null && shortRate !== null ? longRate - shortRate : null,
      })
    } catch {
      results.push({
        token: req.token,
        longExchange: req.longExchange,
        shortExchange: req.shortExchange,
        longRate: null,
        shortRate: null,
        spread: null,
      })
    }
  }))
  entries.value = results
  loading.value = false
}

watch(() => botsStore.bots.length, loadFunding)
onMounted(loadFunding)
</script>

<template>
  <div v-if="botsStore.bots.length > 0" :class="$style.widget">
    <Typography size="text-sm" weight="semibold" color="secondary">Funding Rates (annualised)</Typography>
    <div v-if="loading && !entries.length" :class="$style.loadingText">
      <Typography size="text-xs" color="tertiary">Loading...</Typography>
    </div>
    <div v-else :class="$style.list">
      <div v-for="e in entries" :key="e.token" :class="$style.entry">
        <Typography size="text-sm" weight="medium">{{ e.token }}</Typography>
        <div :class="$style.rates">
          <span :class="$style.rate">
            <Typography size="text-xs" color="tertiary">{{ displayExchange(e.longExchange) }}</Typography>
            <Typography size="text-sm" :style="{ color: (e.longRate ?? 0) >= 0 ? '#22c55e' : '#ef4444' }">
              {{ e.longRate !== null ? (e.longRate * 100).toFixed(1) + '%' : '—' }}
            </Typography>
          </span>
          <span :class="$style.rate">
            <Typography size="text-xs" color="tertiary">{{ displayExchange(e.shortExchange) }}</Typography>
            <Typography size="text-sm" :style="{ color: (e.shortRate ?? 0) >= 0 ? '#22c55e' : '#ef4444' }">
              {{ e.shortRate !== null ? (e.shortRate * 100).toFixed(1) + '%' : '—' }}
            </Typography>
          </span>
          <span :class="$style.rate">
            <Typography size="text-xs" color="tertiary">Spread</Typography>
            <Typography size="text-sm" weight="semibold" :style="{ color: (e.spread ?? 0) >= 0 ? '#22c55e' : '#ef4444' }">
              {{ e.spread !== null ? (e.spread >= 0 ? '+' : '') + (e.spread * 100).toFixed(1) + '%' : '—' }}
            </Typography>
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<style module>
.widget {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: var(--space-4) var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.loadingText {
  padding: var(--space-2) 0;
}

.list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.entry {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--color-stroke-divider);
}
.entry:last-child { border-bottom: none; }

.rates {
  display: flex;
  gap: var(--space-5);
}

.rate {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 1px;
  min-width: 70px;
}
</style>
