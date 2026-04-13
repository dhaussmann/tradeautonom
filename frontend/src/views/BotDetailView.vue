<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useBotStream } from '@/composables/useBotStream'
import { useBotsStore } from '@/stores/bots'
import { useAccountStore } from '@/stores/account'
import { updateBotConfig, adjustBotTimer } from '@/lib/api'
import { fetchMarketsBySymbol, type MarketEntry } from '@/lib/defi-api'
import Typography from '@/components/ui/Typography.vue'
import Button from '@/components/ui/Button.vue'
import Chip from '@/components/ui/Chip.vue'
import StatusDot from '@/components/ui/StatusDot.vue'

const route = useRoute()
const router = useRouter()
const botsStore = useBotsStore()
const accountStore = useAccountStore()

const botId = ref<string | null>(route.params.botId as string)
const { data: status, connected } = useBotStream(botId)
const actionLoading = ref<string | null>(null)

// Timer editor
const showTimerPopover = ref(false)
const timerMinutes = ref(720)
const TIMER_MARKS = [
  { label: '5m', value: 5 },
  { label: '6h', value: 360 },
  { label: '12h', value: 720 },
  { label: '18h', value: 1080 },
  { label: '1d', value: 1440 },
  { label: '4d', value: 5760 },
  { label: '7d', value: 10080 },
]

const DEX_LOGOS: Record<string, string> = {
  extended: '/extended-logo.svg',
  variational: '/variational-logo.svg',
  grvt: '/grvt-logo.svg',
}

let accountPoll: ReturnType<typeof setInterval> | null = null
let positionPoll: ReturnType<typeof setInterval> | null = null
let fundingPoll: ReturnType<typeof setInterval> | null = null
let clockTick: ReturnType<typeof setInterval> | null = null

// Funding intervals per exchange (seconds)
const FUNDING_INTERVALS: Record<string, number> = {
  extended: 3600,    // 1 hour
  variational: 3600, // 1 hour
  grvt: 28800,       // 8 hours
}

// Reactive clock for countdown
const nowSeconds = ref(Math.floor(Date.now() / 1000))

// Live funding from external API (always available, even when idle)
const liveFunding = ref<Record<string, number>>({})

// Quantity popover
const showQtyPopover = ref(false)
const editQtyValue = ref(0)

// Leverage popover
const showLevPopover = ref(false)
const editLevValue = ref(0)

// Spread popovers
const showSpreadPopover = ref(false)
const editSpreadValue = ref(0.5)
const showMinSpreadPopover = ref(false)
const editMinSpreadValue = ref(-0.5)

// ── Computed ────────────────────────────────────────
const isActive = computed(() => status.value && status.value.state !== 'IDLE')
const isIdle = computed(() => status.value?.state === 'IDLE')
const isPaused = computed(() => status.value?.is_paused === true)
const canStart = computed(() => status.value?.state === 'IDLE')
const canStop = computed(() => status.value?.is_running || status.value?.state === 'HOLDING')
const canPause = computed(() => status.value?.state === 'ENTERING' || status.value?.state === 'EXITING')
const canResume = computed(() => status.value?.state === 'PAUSED_ENTERING' || status.value?.state === 'PAUSED_EXITING')
const canEditSpread = computed(() => {
  const s = status.value?.state
  return s === 'IDLE' || s === 'HOLDING' || s === 'PAUSED_ENTERING' || s === 'PAUSED_EXITING'
})

const stateLabel = computed(() => {
  switch (status.value?.state) {
    case 'ENTERING':        return 'Entering'
    case 'HOLDING':         return 'Holding'
    case 'EXITING':         return 'Exiting'
    case 'PAUSED_ENTERING': return 'Paused (Entering)'
    case 'PAUSED_EXITING':  return 'Paused (Exiting)'
    default:                return 'Stopped'
  }
})

const tokenName = computed(() => {
  if (!status.value) return botId.value || '—'
  // Bot ID is the normalized token name (e.g. 'SOL', 'BTC-2')
  const id = botId.value || ''
  const base = id.replace(/-\d+$/, '')
  return base.toUpperCase() || '—'
})

const rawQuantity = computed(() => status.value?.config.quantity ?? 0)

const midPrice = computed(() => {
  if (!status.value) return 0
  const prices = status.value.prices || {}
  for (const p of Object.values(prices)) {
    if (p.mid > 0) return p.mid
  }
  return 0
})

const editQtyUsd = computed(() => {
  if (midPrice.value > 0) return (editQtyValue.value * midPrice.value).toFixed(0)
  return '—'
})

const quantityPillLabel = computed(() => {
  if (!status.value) return '—'
  const qty = rawQuantity.value
  const tok = tokenName.value
  if (midPrice.value > 0) return `${qty} ${tok} / $${(qty * midPrice.value).toFixed(0)}`
  return `${qty} ${tok}`
})

const rawLeverage = computed(() => status.value?.leverage?.long ?? 0)
const rawMinSpread = computed(() => status.value?.config.min_spread_pct ?? -0.5)
const rawMaxSpread = computed(() => status.value?.config.max_spread_pct ?? 0.5)
const minSpreadPillLabel = computed(() => `↓ ${rawMinSpread.value}%`)
const spreadPillLabel = computed(() => `↑ ${rawMaxSpread.value}%`)

const leverageLabel = computed(() => {
  if (!status.value) return '—'
  const l = status.value.leverage?.long ?? 0
  return `${l}X`
})

const timerLabel = computed(() => {
  if (!status.value) return '—'
  const r = status.value.timer.remaining_s
  if (r == null) {
    const h = status.value.timer.duration_h || 0
    const m = status.value.timer.duration_m || 0
    const total = h * 60 + m
    return formatDuration(total)
  }
  return formatDuration(Math.round(r / 60))
})

const spreadApr = computed(() => {
  // Always compute from DeFi API (APR-normalised, consistent across exchanges).
  // Backend spread_annualised uses raw per-interval rates which are incompatible across exchanges.
  const longRate = fundingRateForExchange(longEx.value)
  const shortRate = fundingRateForExchange(shortEx.value)
  if (longRate !== null && shortRate !== null) return shortRate - longRate
  return 0
})

const spreadPct = computed(() => Math.min(Math.abs(spreadApr.value) * 100, 100))

// Per-exchange prices from SSE stream
const longPrice = computed(() => {
  if (!status.value) return null
  return status.value.prices?.[longEx.value] || null
})

const shortPrice = computed(() => {
  if (!status.value) return null
  return status.value.prices?.[shortEx.value] || null
})

const priceSpreadPct = computed(() => {
  const lp = longPrice.value
  const sp = shortPrice.value
  if (!lp || !sp) return null
  const longAsk = lp.best_ask
  const shortBid = sp.best_bid
  if (!longAsk || !shortBid || shortBid <= 0) return null
  return ((longAsk - shortBid) / shortBid) * 100
})

// Use config as primary source (always set), position as fallback
const longEx = computed(() => status.value?.config.long_exchange || status.value?.position.long_exchange || '')
const shortEx = computed(() => status.value?.config.short_exchange || status.value?.position.short_exchange || '')

// PnL from exchange-reported unrealized_pnl (more accurate than state machine entry prices)
const longPnl = computed(() => {
  const pos = positionForExchange(longEx.value)
  return pos ? Number(pos.unrealized_pnl) || 0 : 0
})
const shortPnl = computed(() => {
  const pos = positionForExchange(shortEx.value)
  return pos ? Number(pos.unrealized_pnl) || 0 : 0
})

function balanceForExchange(exchange: string): string {
  const acc = accountStore.accounts.find(a => a.exchange === exchange)
  if (!acc) return '—'
  return `$${Number(acc.equity).toFixed(2)}`
}

function fundingRateForExchange(exchange: string): number | null {
  // Always use DeFi API data (APR-normalised, consistent across all exchanges).
  // Stream data uses incompatible per-interval formats (GRVT 8h, Extended 1h, etc.).
  if (exchange in liveFunding.value) return liveFunding.value[exchange]
  return null
}

function positionForExchange(exchange: string): import('@/types/account').Position | null {
  if (!status.value) return null
  const instrument = exchange === longEx.value
    ? status.value.config.instrument_a
    : status.value.config.instrument_b
  if (!instrument) return null
  return accountStore.positions.find(
    p => p.exchange === exchange && p.instrument === instrument
  ) ?? null
}

function fundingPerHour(exchange: string): string {
  const rate = fundingRateForExchange(exchange)
  if (rate === null || midPrice.value <= 0 || rawQuantity.value <= 0) return '$0.00/hr'
  const posValue = rawQuantity.value * midPrice.value
  const hourlyRate = rate / 8760
  // Positive rate = longs pay shorts → long side earnings are negative, short side positive
  const isLong = exchange === longEx.value
  const perHour = isLong ? -(posValue * hourlyRate) : posValue * hourlyRate
  const sign = perHour >= 0 ? '+' : ''
  return `${sign}$${perHour.toFixed(4)}/hr`
}

function nextFundingCountdown(exchange: string): string {
  const interval = FUNDING_INTERVALS[exchange]
  if (!interval) return '—'
  const now = nowSeconds.value
  const next = Math.ceil(now / interval) * interval
  const diff = next - now
  if (diff <= 0) return '00:00:00'
  const h = Math.floor(diff / 3600)
  const m = Math.floor((diff % 3600) / 60)
  const s = diff % 60
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
}

async function loadLiveFunding() {
  const tok = (tokenName.value || '').toUpperCase()
  if (!tok || tok === '—') return
  try {
    const entries: MarketEntry[] = await fetchMarketsBySymbol(tok)
    const map: Record<string, number> = {}
    for (const e of entries) {
      map[e.exchange] = e.funding_rate_apr
    }
    liveFunding.value = map
  } catch { /* ignore */ }
}

// ── Formatters ──────────────────────────────────────
function formatDuration(totalMinutes: number): string {
  if (totalMinutes < 60) return `${totalMinutes}m`
  const h = Math.floor(totalMinutes / 60)
  const m = totalMinutes % 60
  if (h >= 24) {
    const d = Math.floor(h / 24)
    const rh = h % 24
    return rh > 0 ? `${d}d ${rh}h` : `${d}d`
  }
  return m > 0 ? `${h}h ${m}m` : `${h}h`
}

function formatRate(val: number | null): string {
  if (val === null) return '—'
  return `${(val * 100).toFixed(2)}%`
}

function formatPnl(val: number | string | undefined): string {
  if (val === undefined || val === null) return '—'
  const n = Number(val) || 0
  const sign = n >= 0 ? '+' : ''
  return `${sign}$${n.toFixed(4)}`
}

// ── Popover edit actions ─────────────────────────────
function openQtyEditor() {
  editQtyValue.value = rawQuantity.value || 1
  showQtyPopover.value = !showQtyPopover.value
  showLevPopover.value = false
  showSpreadPopover.value = false
}

async function saveQty() {
  showQtyPopover.value = false
  if (editQtyValue.value > 0 && editQtyValue.value !== rawQuantity.value) {
    try { await updateBotConfig(botId.value!, { quantity: editQtyValue.value }) } catch { /* ignore */ }
  }
}

function openLevEditor() {
  editLevValue.value = rawLeverage.value || 1
  showLevPopover.value = !showLevPopover.value
  showQtyPopover.value = false
  showSpreadPopover.value = false
}

async function saveLev() {
  showLevPopover.value = false
  if (editLevValue.value > 0 && editLevValue.value !== rawLeverage.value) {
    try { await updateBotConfig(botId.value!, { leverage_long: editLevValue.value, leverage_short: editLevValue.value }) } catch { /* ignore */ }
  }
}

function openMinSpreadEditor() {
  editMinSpreadValue.value = rawMinSpread.value
  showMinSpreadPopover.value = !showMinSpreadPopover.value
  showSpreadPopover.value = false
  showQtyPopover.value = false
  showLevPopover.value = false
}

async function saveMinSpread() {
  showMinSpreadPopover.value = false
  const val = Number(editMinSpreadValue.value)
  if (!isNaN(val)) {
    try { await updateBotConfig(botId.value!, { min_spread_pct: val }) } catch (e) { console.error('saveMinSpread failed:', e) }
  }
}

function openSpreadEditor() {
  editSpreadValue.value = rawMaxSpread.value
  showSpreadPopover.value = !showSpreadPopover.value
  showMinSpreadPopover.value = false
  showQtyPopover.value = false
  showLevPopover.value = false
}

async function saveSpread() {
  showSpreadPopover.value = false
  const val = Number(editSpreadValue.value)
  if (!isNaN(val) && val >= 0) {
    try { await updateBotConfig(botId.value!, { max_spread_pct: val }) } catch (e) { console.error('saveSpread failed:', e) }
  }
}

// ── Actions ─────────────────────────────────────────
async function handleStart() {
  actionLoading.value = 'start'
  try { await botsStore.start(botId.value!) }
  finally { actionLoading.value = null }
}

async function handleStop() {
  actionLoading.value = 'stop'
  try { await botsStore.stop(botId.value!) }
  finally { actionLoading.value = null }
}

async function handleKill() {
  actionLoading.value = 'kill'
  try { await botsStore.kill(botId.value!) }
  finally { actionLoading.value = null }
}

async function handlePause() {
  actionLoading.value = 'pause'
  try { await botsStore.pause(botId.value!) }
  finally { actionLoading.value = null }
}

async function handleResume() {
  actionLoading.value = 'resume'
  try { await botsStore.resume(botId.value!) }
  finally { actionLoading.value = null }
}

function openTimerEditor() {
  if (!status.value) return
  const h = status.value.timer.duration_h || 0
  const m = status.value.timer.duration_m || 0
  timerMinutes.value = h * 60 + m || 720
  showTimerPopover.value = !showTimerPopover.value
  showQtyPopover.value = false
  showLevPopover.value = false
}

async function saveTimer() {
  const h = Math.floor(timerMinutes.value / 60)
  const m = timerMinutes.value % 60
  try {
    if (isIdle.value) {
      await updateBotConfig(botId.value!, { duration_h: h, duration_m: m })
    } else {
      await adjustBotTimer(botId.value!, h, m)
    }
  } catch { /* ignore */ }
  showTimerPopover.value = false
}

function setTimerPreset(val: number) {
  timerMinutes.value = val
}

onMounted(async () => {
  if (!botId.value) { router.push('/'); return }
  await accountStore.loadAccounts()
  await accountStore.loadPositions()
  accountPoll = setInterval(() => accountStore.loadAccounts(), 15000)
  positionPoll = setInterval(() => accountStore.loadPositions(), 15000)
  // Load live funding immediately and poll every 30s
  loadLiveFunding()
  fundingPoll = setInterval(loadLiveFunding, 30000)
  // Reload when tokenName resolves (e.g. after SSE connects)
  watch(tokenName, () => loadLiveFunding())
  // Tick clock every second for funding countdown
  clockTick = setInterval(() => { nowSeconds.value = Math.floor(Date.now() / 1000) }, 1000)
})

onUnmounted(() => {
  if (accountPoll) clearInterval(accountPoll)
  if (positionPoll) clearInterval(positionPoll)
  if (fundingPoll) clearInterval(fundingPoll)
  if (clockTick) clearInterval(clockTick)
})
</script>

<template>
  <div :class="$style.page">
    <!-- Back button -->
    <div :class="$style.back">
      <Button variant="ghost" size="sm" @click="router.push('/')">← Back</Button>
    </div>

    <div v-if="!status" :class="$style.loading">
      <Typography color="secondary">Connecting to bot stream...</Typography>
    </div>

    <template v-else>
      <!-- ── Top Bar ── -->
      <div :class="$style.topBar">
        <div :class="$style.pills">
          <div :class="[$style.pill, $style.pillStatus]">
            <StatusDot :active="status.is_running" :color="isActive ? 'brand' : 'neutral'" />
            <span>{{ stateLabel }}</span>
            <Chip v-if="!connected" variant="error" size="sm">SSE</Chip>
          </div>
          <div :class="$style.pill">◆ {{ tokenName }}</div>
          <!-- Quantity pill + popover -->
          <div :class="[$style.pill, isIdle && $style.pillEditable]" @click="isIdle && openQtyEditor()">
            {{ quantityPillLabel }}
            <div v-if="showQtyPopover && isIdle" :class="$style.popover" @click.stop>
              <Typography size="text-xs" weight="semibold" color="secondary">Quantity ({{ tokenName }})</Typography>
              <input
                v-model.number="editQtyValue"
                :class="$style.popoverInput"
                type="number"
                min="0.01"
                step="0.1"
              />
              <div :class="$style.popoverUsd">
                <Typography size="text-xs" color="tertiary">≈ ${{ editQtyUsd }}</Typography>
              </div>
              <Button variant="solid" color="success" size="sm" @click="saveQty">Apply</Button>
            </div>
          </div>
          <!-- Leverage pill + popover -->
          <div :class="[$style.pill, isIdle && $style.pillEditable]" @click="isIdle && openLevEditor()">
            ↗ {{ leverageLabel }}
            <div v-if="showLevPopover && isIdle" :class="$style.popover" @click.stop>
              <Typography size="text-xs" weight="semibold" color="secondary">Leverage</Typography>
              <div :class="$style.popoverLevDisplay">
                <Typography size="text-h6" weight="semibold">{{ editLevValue }}X</Typography>
              </div>
              <input
                v-model.number="editLevValue"
                type="range"
                min="1"
                max="50"
                step="1"
                :class="$style.popoverSlider"
              />
              <div :class="$style.popoverLevLabels">
                <span>1X</span><span>10X</span><span>25X</span><span>50X</span>
              </div>
              <Button variant="solid" color="success" size="sm" @click="saveLev">Apply</Button>
            </div>
          </div>
          <!-- Min Spread pill + popover -->
          <div :class="[$style.pill, canEditSpread && $style.pillEditable]" @click="canEditSpread && openMinSpreadEditor()">
            {{ minSpreadPillLabel }}
            <div v-if="showMinSpreadPopover && canEditSpread" :class="$style.popover" @click.stop>
              <Typography size="text-xs" weight="semibold" color="secondary">Min Spread % (safety floor)</Typography>
              <input
                v-model.number="editMinSpreadValue"
                :class="$style.popoverInput"
                type="number"
                step="0.1"
              />
              <div v-if="priceSpreadPct !== null" :class="$style.popoverUsd">
                <Typography size="text-xs" :style="{ color: priceSpreadPct >= editMinSpreadValue ? '#22c55e' : '#ef4444' }">
                  Current: {{ priceSpreadPct.toFixed(4) }}%
                </Typography>
              </div>
              <Button variant="solid" color="success" size="sm" @click="saveMinSpread">Apply</Button>
            </div>
          </div>
          <!-- Max Spread pill + popover -->
          <div :class="[$style.pill, canEditSpread && $style.pillEditable]" @click="canEditSpread && openSpreadEditor()">
            {{ spreadPillLabel }}
            <div v-if="showSpreadPopover && canEditSpread" :class="$style.popover" @click.stop>
              <Typography size="text-xs" weight="semibold" color="secondary">Max Spread (%)</Typography>
              <input
                v-model.number="editSpreadValue"
                :class="$style.popoverInput"
                type="number"
                min="0"
                step="0.01"
              />
              <div v-if="priceSpreadPct !== null" :class="$style.popoverUsd">
                <Typography size="text-xs" :style="{ color: priceSpreadPct <= editSpreadValue ? '#22c55e' : '#ef4444' }">
                  Current: {{ priceSpreadPct.toFixed(4) }}%
                </Typography>
              </div>
              <Button variant="solid" color="success" size="sm" @click="saveSpread">Apply</Button>
            </div>
          </div>
          <div :class="[$style.pill, $style.pillTimer]" @click="openTimerEditor">
            ⏱ {{ timerLabel }}
            <!-- Timer Popover -->
            <div v-if="showTimerPopover" :class="$style.timerPopover" @click.stop>
              <input
                v-model.number="timerMinutes"
                :class="$style.timerInput"
                type="number"
                min="1"
                max="10080"
              />
              <Typography size="text-xs" color="tertiary">minutes</Typography>
              <div :class="$style.timerMarks">
                <button
                  v-for="m in TIMER_MARKS"
                  :key="m.value"
                  :class="[$style.timerMark, timerMinutes === m.value && $style.timerMarkActive]"
                  @click="setTimerPreset(m.value)"
                >{{ m.label }}</button>
              </div>
              <input
                v-model.number="timerMinutes"
                type="range"
                min="5"
                max="10080"
                step="5"
                :class="$style.timerSlider"
              />
              <Button variant="solid" color="success" size="sm" @click="saveTimer">Apply</Button>
            </div>
          </div>
        </div>

        <div :class="$style.controls">
          <Button v-if="canStart" variant="solid" color="success" size="md" :loading="actionLoading === 'start'" @click="handleStart">▶ Start</Button>
          <Button v-if="canPause" variant="outline" color="warning" size="md" :loading="actionLoading === 'pause'" @click="handlePause">⏸ Pause</Button>
          <Button v-if="canResume" variant="solid" color="success" size="md" :loading="actionLoading === 'resume'" @click="handleResume">▶ Resume</Button>
          <Button v-if="canStop" variant="outline" color="warning" size="md" :loading="actionLoading === 'stop'" @click="handleStop">Stop</Button>
          <Button v-if="isActive || isPaused" variant="outline" color="error" size="md" :loading="actionLoading === 'kill'" @click="handleKill">Kill</Button>
        </div>
      </div>

      <!-- ── 3-Column Layout ── -->
      <div :class="$style.mainGrid">
        <!-- Left: Long Exchange -->
        <div :class="[$style.dexCard, $style.dexCardLong]">
          <div :class="$style.dexHeader">
            <img v-if="DEX_LOGOS[longEx]" :src="DEX_LOGOS[longEx]" :class="$style.dexLogo" />
            <span v-else :class="$style.dexName">{{ longEx }}</span>
            <Chip variant="long" size="sm">Long</Chip>
            <Chip variant="neutral" size="sm">{{ botId }}</Chip>
            <span :class="$style.dexBalance">Balance {{ balanceForExchange(longEx) }}</span>
          </div>
          <div :class="$style.dexStats">
            <div :class="$style.dexStat">
              <span :class="$style.statIcon">◉</span>
              <span :style="{ color: (fundingRateForExchange(longEx) ?? 0) >= 0 ? '#ef4444' : '#22c55e' }">
                {{ formatRate(fundingRateForExchange(longEx)) }}
              </span>
            </div>
            <div :class="$style.dexStat">
              <span :class="$style.statIcon">💰</span>
              <span>{{ fundingPerHour(longEx) }}</span>
            </div>
            <div :class="$style.dexStat">
              <span :class="$style.statIcon">⏳</span>
              <span>{{ nextFundingCountdown(longEx) }}</span>
            </div>
          </div>
          <div v-if="isActive" :class="$style.posTable">
            <template v-if="positionForExchange(longEx)">
              <div :class="$style.posRow">
                <Typography size="text-xs" color="tertiary">Size</Typography>
                <Typography size="text-sm">{{ Math.abs(Number(positionForExchange(longEx)!.size)) }}</Typography>
              </div>
              <div :class="$style.posRow">
                <Typography size="text-xs" color="tertiary">Value</Typography>
                <Typography size="text-sm">${{ (Math.abs(Number(positionForExchange(longEx)!.size)) * Number(positionForExchange(longEx)!.mark_price)).toFixed(2) }}</Typography>
              </div>
              <div :class="$style.posRow">
                <Typography size="text-xs" color="tertiary">Entry</Typography>
                <Typography size="text-sm">${{ Number(positionForExchange(longEx)!.entry_price).toFixed(4) }}</Typography>
              </div>
              <div :class="$style.posRow">
                <Typography size="text-xs" color="tertiary">Liq.</Typography>
                <Typography size="text-sm">{{ Number(positionForExchange(longEx)!.est_liquidation_price) ? '$' + Number(positionForExchange(longEx)!.est_liquidation_price).toFixed(4) : '—' }}</Typography>
              </div>
            </template>
            <template v-else>
              <div :class="$style.posRow"><Typography size="text-xs" color="tertiary">Size</Typography><Typography size="text-sm" color="tertiary">—</Typography></div>
              <div :class="$style.posRow"><Typography size="text-xs" color="tertiary">Value</Typography><Typography size="text-sm" color="tertiary">—</Typography></div>
              <div :class="$style.posRow"><Typography size="text-xs" color="tertiary">Entry</Typography><Typography size="text-sm" color="tertiary">—</Typography></div>
              <div :class="$style.posRow"><Typography size="text-xs" color="tertiary">Liq.</Typography><Typography size="text-sm" color="tertiary">—</Typography></div>
            </template>
          </div>
        </div>

        <!-- Center: Spread Ring -->
        <div :class="$style.centerPanel">
          <div :class="$style.ringWrap">
            <svg viewBox="0 0 120 120" :class="$style.ringSvg">
              <circle cx="60" cy="60" r="52" fill="none" stroke="var(--color-stroke-divider)" stroke-width="8" />
              <circle
                cx="60" cy="60" r="52"
                fill="none"
                :stroke="spreadApr >= 0 ? '#22c55e' : '#ef4444'"
                stroke-width="8"
                stroke-linecap="round"
                :stroke-dasharray="`${spreadPct * 3.267} 326.7`"
                :stroke-dashoffset="0"
                transform="rotate(-90 60 60)"
              />
            </svg>
            <div :class="$style.ringCenter">
              <span
                :class="$style.ringValue"
                :style="{ color: spreadApr >= 0 ? '#22c55e' : '#ef4444' }"
              >{{ (spreadApr * 100).toFixed(1) }}%</span>
              <span :class="$style.ringLabel">SPREAD APR</span>
            </div>
          </div>

          <div :class="$style.quantScore">
            <Typography size="text-xs" color="tertiary">—/4 quant score</Typography>
          </div>

          <!-- PnL summary -->
          <div :class="$style.pnlRow">
            <div :class="$style.pnlItem">
              <Typography size="text-xs" color="tertiary">Long PnL</Typography>
              <Typography size="text-sm" :color="longPnl >= 0 ? 'success' : 'error'">
                {{ formatPnl(longPnl) }}
              </Typography>
            </div>
            <div :class="$style.pnlItem">
              <Typography size="text-xs" color="tertiary">Short PnL</Typography>
              <Typography size="text-sm" :color="shortPnl >= 0 ? 'success' : 'error'">
                {{ formatPnl(shortPnl) }}
              </Typography>
            </div>
            <div :class="$style.pnlItem">
              <Typography size="text-xs" color="tertiary">Total</Typography>
              <Typography size="text-md" weight="semibold" :color="(longPnl + shortPnl) >= 0 ? 'success' : 'error'">
                {{ formatPnl(longPnl + shortPnl) }}
              </Typography>
            </div>
          </div>

          <!-- Execution progress -->
          <div :class="$style.execRow">
            <Typography size="text-xs" color="tertiary">
              Chunks {{ status.execution.chunks_completed }}/{{ status.execution.total_chunks }}
            </Typography>
            <div :class="$style.execBar">
              <div
                :class="$style.execFill"
                :style="{ width: status.execution.total_chunks > 0
                  ? (status.execution.chunks_completed / status.execution.total_chunks * 100) + '%'
                  : '0%' }"
              />
            </div>
          </div>

          <!-- Mark Price -->
          <div :class="$style.pnlRow">
            <div :class="$style.pnlItem">
              <Typography size="text-xs" color="tertiary">Long Mark</Typography>
              <Typography size="text-sm">{{ longPrice ? '$' + longPrice.mid.toFixed(4) : '—' }}</Typography>
            </div>
            <div :class="$style.pnlItem">
              <Typography size="text-xs" color="tertiary">Spread</Typography>
              <Typography
                size="text-sm"
                weight="semibold"
                :color="priceSpreadPct !== null && priceSpreadPct >= 0 ? 'success' : 'error'"
              >{{ priceSpreadPct !== null ? priceSpreadPct.toFixed(4) + '%' : '—' }}</Typography>
            </div>
            <div :class="$style.pnlItem">
              <Typography size="text-xs" color="tertiary">Short Mark</Typography>
              <Typography size="text-sm">{{ shortPrice ? '$' + shortPrice.mid.toFixed(4) : '—' }}</Typography>
            </div>
          </div>
        </div>

        <!-- Right: Short Exchange -->
        <div :class="[$style.dexCard, $style.dexCardShort]">
          <div :class="$style.dexHeader">
            <img v-if="DEX_LOGOS[shortEx]" :src="DEX_LOGOS[shortEx]" :class="$style.dexLogo" />
            <span v-else :class="$style.dexName">{{ shortEx }}</span>
            <Chip variant="short" size="sm">Short</Chip>
            <Chip variant="neutral" size="sm">{{ botId }}</Chip>
            <span :class="$style.dexBalance">Balance {{ balanceForExchange(shortEx) }}</span>
          </div>
          <div :class="$style.dexStats">
            <div :class="$style.dexStat">
              <span :class="$style.statIcon">◉</span>
              <span :style="{ color: (fundingRateForExchange(shortEx) ?? 0) >= 0 ? '#22c55e' : '#ef4444' }">
                {{ formatRate(fundingRateForExchange(shortEx)) }}
              </span>
            </div>
            <div :class="$style.dexStat">
              <span :class="$style.statIcon">💰</span>
              <span>{{ fundingPerHour(shortEx) }}</span>
            </div>
            <div :class="$style.dexStat">
              <span :class="$style.statIcon">⏳</span>
              <span>{{ nextFundingCountdown(shortEx) }}</span>
            </div>
          </div>
          <div v-if="isActive" :class="$style.posTable">
            <template v-if="positionForExchange(shortEx)">
              <div :class="$style.posRow">
                <Typography size="text-xs" color="tertiary">Size</Typography>
                <Typography size="text-sm">{{ Math.abs(Number(positionForExchange(shortEx)!.size)) }}</Typography>
              </div>
              <div :class="$style.posRow">
                <Typography size="text-xs" color="tertiary">Value</Typography>
                <Typography size="text-sm">${{ (Math.abs(Number(positionForExchange(shortEx)!.size)) * Number(positionForExchange(shortEx)!.mark_price)).toFixed(2) }}</Typography>
              </div>
              <div :class="$style.posRow">
                <Typography size="text-xs" color="tertiary">Entry</Typography>
                <Typography size="text-sm">${{ Number(positionForExchange(shortEx)!.entry_price).toFixed(4) }}</Typography>
              </div>
              <div :class="$style.posRow">
                <Typography size="text-xs" color="tertiary">Liq.</Typography>
                <Typography size="text-sm">{{ Number(positionForExchange(shortEx)!.est_liquidation_price) ? '$' + Number(positionForExchange(shortEx)!.est_liquidation_price).toFixed(4) : '—' }}</Typography>
              </div>
            </template>
            <template v-else>
              <div :class="$style.posRow"><Typography size="text-xs" color="tertiary">Size</Typography><Typography size="text-sm" color="tertiary">—</Typography></div>
              <div :class="$style.posRow"><Typography size="text-xs" color="tertiary">Value</Typography><Typography size="text-sm" color="tertiary">—</Typography></div>
              <div :class="$style.posRow"><Typography size="text-xs" color="tertiary">Entry</Typography><Typography size="text-sm" color="tertiary">—</Typography></div>
              <div :class="$style.posRow"><Typography size="text-xs" color="tertiary">Liq.</Typography><Typography size="text-sm" color="tertiary">—</Typography></div>
            </template>
          </div>
        </div>
      </div>

      <!-- ── Activity Log ── -->
      <div :class="$style.logSection">
        <Typography size="text-md" weight="semibold" color="secondary" as="h3">Activity Log</Typography>
        <div :class="$style.logContainer">
          <div v-if="!status.activity_log.length" :class="$style.logEmpty">
            <Typography size="text-sm" color="tertiary">No activity yet</Typography>
          </div>
          <div
            v-for="entry in [...status.activity_log].reverse().slice(0, 50)"
            :key="entry.seq"
            :class="[$style.logEntry, entry.extra?.level === 'error' && $style['logEntry--error'], entry.extra?.level === 'warn' && $style['logEntry--warn']]"
          >
            <Typography size="text-xs" color="tertiary" :class="$style.logTime">
              {{ new Date(entry.ts * 1000).toLocaleTimeString() }}
            </Typography>
            <Chip
              :variant="entry.cat === 'FILL' ? 'success' : entry.cat === 'RISK' ? 'warning' : entry.cat === 'ORDER' ? 'info' : entry.extra?.level === 'error' ? 'error' : 'neutral'"
              size="sm"
              :class="$style.logCat"
            >
              {{ entry.cat }}
            </Chip>
            <Typography size="text-sm" :color="entry.extra?.level === 'error' ? 'error' : 'primary'">
              {{ entry.msg }}
            </Typography>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<style module>
.page {
  padding: 30px 40px 60px;
  max-width: 1200px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}

.back { margin-bottom: var(--space-1); }

.loading {
  padding: var(--space-16) 0;
  text-align: center;
}

/* ── Top Bar ── */
.topBar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  border-radius: var(--radius-xl);
  background: var(--color-white-2);
  border: 1px solid var(--color-stroke-divider);
}

.pills {
  display: flex;
  align-items: center;
  gap: 8px;
}

.pill {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px 16px;
  border-radius: 999px;
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text-primary);
  white-space: nowrap;
  cursor: default;
}

.pillStatus {
  gap: 8px;
}

.pillEditable {
  cursor: pointer;
  position: relative;
}
.pillEditable:hover {
  border-color: var(--color-brand, #6366f1);
}

/* ── Shared Popover (qty, lev) ── */
.popover {
  position: absolute;
  top: calc(100% + 8px);
  left: 50%;
  transform: translateX(-50%);
  width: 240px;
  padding: 16px;
  background: var(--color-bg-primary);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-lg);
  box-shadow: 0 8px 32px rgba(0,0,0,0.4);
  z-index: 100;
  display: flex;
  flex-direction: column;
  gap: 10px;
  cursor: default;
}

.popoverInput {
  width: 100%;
  padding: 8px 12px;
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  color: var(--color-text-primary);
  font-size: 16px;
  text-align: center;
  outline: none;
}
.popoverInput:focus { border-color: var(--color-brand, #6366f1); }

.popoverUsd {
  text-align: center;
}

.popoverSlider {
  width: 100%;
  accent-color: var(--color-brand, #6366f1);
}

.popoverLevDisplay {
  text-align: center;
}

.popoverLevLabels {
  display: flex;
  justify-content: space-between;
  font-size: 10px;
  color: var(--color-text-tertiary);
}

.pillTimer {
  cursor: pointer;
  position: relative;
}
.pillTimer:hover {
  border-color: var(--color-brand, #6366f1);
}

.controls {
  display: flex;
  gap: var(--space-2);
}

/* ── Timer Popover ── */
.timerPopover {
  position: absolute;
  top: calc(100% + 8px);
  left: 50%;
  transform: translateX(-50%);
  width: 280px;
  padding: 16px;
  background: var(--color-bg-primary);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-lg);
  box-shadow: 0 8px 32px rgba(0,0,0,0.4);
  z-index: 100;
  display: flex;
  flex-direction: column;
  gap: 10px;
  cursor: default;
}

.timerInput {
  width: 100%;
  padding: 8px 12px;
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  color: var(--color-text-primary);
  font-size: 16px;
  text-align: center;
  outline: none;
}
.timerInput:focus { border-color: var(--color-brand, #6366f1); }

.timerMarks {
  display: flex;
  justify-content: space-between;
  gap: 4px;
}

.timerMark {
  flex: 1;
  padding: 4px 0;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-stroke-divider);
  background: transparent;
  color: var(--color-text-tertiary);
  font-size: 11px;
  cursor: pointer;
  transition: all 0.1s;
}
.timerMark:hover { border-color: var(--color-text-secondary); color: var(--color-text-primary); }
.timerMarkActive {
  border-color: var(--color-brand, #6366f1);
  background: rgba(99,102,241,0.1);
  color: var(--color-brand, #6366f1);
}

.timerSlider {
  width: 100%;
  accent-color: var(--color-brand, #6366f1);
}

/* ── 3-Column Main Grid ── */
.mainGrid {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  gap: var(--space-5);
  align-items: start;
}

/* ── DEX Cards ── */
.dexCard {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  padding: var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.dexCardLong {
  background: linear-gradient(135deg, rgba(34,197,94,0.06) 0%, var(--color-white-2) 100%);
  border-color: rgba(34,197,94,0.15);
}

.dexCardShort {
  background: linear-gradient(135deg, rgba(239,68,68,0.06) 0%, var(--color-white-2) 100%);
  border-color: rgba(239,68,68,0.15);
}

.dexHeader {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.dexLogo {
  height: 22px;
  width: auto;
}

.dexName {
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text-primary);
}

.dexBalance {
  margin-left: auto;
  font-size: 12px;
  font-weight: 500;
  padding: 4px 10px;
  border-radius: var(--radius-md);
  background: rgba(34,197,94,0.1);
  color: #22c55e;
}


.dexStats {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.dexStat {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  font-weight: 500;
}

.statIcon {
  font-size: 12px;
  opacity: 0.6;
}

.statPlaceholder {
  color: var(--color-text-tertiary);
}

.posTable {
  display: flex;
  flex-direction: column;
  gap: 0;
  border-top: 1px solid var(--color-stroke-divider);
  padding-top: var(--space-3);
  margin-top: var(--space-1);
}

.posRow {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 3px 0;
}

/* ── Center Panel ── */
.centerPanel {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-4);
  padding: var(--space-4) var(--space-5);
  min-width: 220px;
}

.ringWrap {
  position: relative;
  width: 160px;
  height: 160px;
}

.ringSvg {
  width: 100%;
  height: 100%;
}

.ringCenter {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}

.ringValue {
  font-size: 22px;
  font-weight: 700;
  line-height: 1;
}

.ringLabel {
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.05em;
  color: var(--color-text-tertiary);
  margin-top: 4px;
}

.quantScore {
  text-align: center;
}

.pnlRow {
  display: flex;
  gap: var(--space-4);
  width: 100%;
}

.pnlItem {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  white-space: nowrap;
  min-width: 0;
}

.execRow {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.execBar {
  width: 100%;
  height: 4px;
  border-radius: 2px;
  background: var(--color-stroke-divider);
  overflow: hidden;
}

.execFill {
  height: 100%;
  border-radius: 2px;
  background: var(--color-brand, #6366f1);
  transition: width 0.3s ease;
}

/* ── Price Spread ── */
.priceSpread {
  width: 100%;
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding-top: var(--space-2);
  border-top: 1px solid var(--color-stroke-divider);
}

.priceRow {
  display: flex;
  gap: var(--space-4);
  width: 100%;
}

.priceCol {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
}

.priceValues {
  display: flex;
  align-items: center;
  gap: 3px;
  font-size: 12px;
  font-weight: 500;
  font-variant-numeric: tabular-nums;
}

.priceBid { color: #22c55e; }
.priceAsk { color: #ef4444; }
.priceSep { color: var(--color-text-tertiary); font-size: 10px; }

.priceSpreadValue {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
}

/* ── Activity Log ── */
.logSection {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.logContainer {
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  max-height: 400px;
  overflow-y: auto;
}

.logEmpty {
  padding: var(--space-8);
  text-align: center;
}

.logEntry {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-4);
  border-bottom: 1px solid var(--color-stroke-divider);
  font-size: var(--text-sm);
}
.logEntry:last-child { border-bottom: none; }

.logEntry--error { background: var(--color-error-bg); }
.logEntry--warn  { background: var(--color-warning-bg); }

.logTime {
  flex-shrink: 0;
  width: 75px;
}

.logCat {
  flex-shrink: 0;
  width: 60px;
  justify-content: center;
  text-align: center;
}
</style>
