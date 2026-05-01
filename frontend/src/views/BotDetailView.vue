<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useBotStream } from '@/composables/useBotStream'
import { useBotsStore } from '@/stores/bots'
import { useAccountStore } from '@/stores/account'
import { updateBotConfig, adjustBotTimer, fetchBotFills } from '@/lib/api'
import { fetchMarketsBySymbol, type MarketEntry } from '@/lib/defi-api'
import type { FillEntry } from '@/types/bot'
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

// Spread popovers — Entry
const showSpreadPopover = ref(false)
const editSpreadValue = ref(0.5)
const showMinSpreadPopover = ref(false)
const editMinSpreadValue = ref(-0.5)
// Spread popovers — Exit (separate thresholds, applied during manual_exit)
const showExitMaxSpreadPopover = ref(false)
const editExitMaxSpreadValue = ref(0.05)
const showExitMinSpreadPopover = ref(false)
const editExitMinSpreadValue = ref(-0.5)

// Advanced settings panel
const showAdvancedPanel = ref(false)

// ── Filled Orders ─────────────────────────────────
// Strategy: load the FULL history once via REST on mount, then merge
// new fills from the SSE-streamed `status.fills` (last-50 tail) using
// a (action, chunk_index, ts) composite key. This keeps the bandwidth
// small while letting the user scroll through the entire trade history.
const allFills = ref<FillEntry[]>([])
const fillsLoaded = ref(false)
const fillsLoadError = ref<string | null>(null)

function fillKey(f: FillEntry): string {
  return `${f.action}:${f.chunk_index}:${f.ts}`
}

function mergeFills(incoming: FillEntry[]) {
  if (!incoming || incoming.length === 0) return
  const seen = new Set(allFills.value.map(fillKey))
  let added = 0
  for (const f of incoming) {
    const k = fillKey(f)
    if (!seen.has(k)) {
      allFills.value.push(f)
      seen.add(k)
      added++
    }
  }
  if (added > 0) {
    allFills.value.sort((a, b) => b.ts - a.ts)
  }
}

/**
 * Format a USD spread value with dynamic precision so it reads well
 * across the full range of asset prices the bot trades:
 *   - BTC fills can produce spreads of several dollars → 2 decimals
 *   - mid-range assets (ARB, UNI) sit around 0.01–0.5 → 4 decimals
 *   - very tight spreads on low-priced assets → 6 decimals
 * Sign is always rendered (+ for positive, − for negative) so the
 * carry direction is unambiguous.
 */
function formatSpreadUsd(v: number | null): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—'
  const abs = Math.abs(v)
  let decimals: number
  if (abs >= 1) decimals = 2
  else if (abs >= 0.01) decimals = 4
  else decimals = 6
  const sign = v >= 0 ? '+' : '−'
  return `${sign}$${abs.toFixed(decimals)}`
}

async function loadFillsHistory() {
  if (!botId.value) return
  fillsLoadError.value = null
  try {
    const resp = await fetchBotFills(botId.value, 0)
    allFills.value = (resp.fills ?? []).slice().sort((a, b) => b.ts - a.ts)
    fillsLoaded.value = true
  } catch (e) {
    fillsLoadError.value = e instanceof Error ? e.message : String(e)
    fillsLoaded.value = true
  }
}

// Watch SSE-streamed fills tail and merge any new ones into the full list.
watch(
  () => status.value?.fills,
  (incoming) => {
    if (!fillsLoaded.value) return
    if (incoming && incoming.length > 0) mergeFills(incoming as FillEntry[])
  },
  { deep: false },
)

// Local refs for numeric inputs (prevents SSE overwrite while editing)
const localSlippageBps = ref(10)
const localMinConsistency = ref(0.3)
const localDriftBps = ref(3)
const slippageFocused = ref(false)
const consistencyFocused = ref(false)
const driftBpsFocused = ref(false)

watch(() => status.value?.config?.fn_opt_max_slippage_bps, (v) => {
  if (!slippageFocused.value && v !== undefined) localSlippageBps.value = v as number
}, { immediate: true })
watch(() => status.value?.config?.fn_opt_min_funding_consistency, (v) => {
  if (!consistencyFocused.value && v !== undefined) localMinConsistency.value = v as number
}, { immediate: true })
watch(() => status.value?.config?.fn_opt_max_taker_drift_bps, (v) => {
  if (!driftBpsFocused.value && v !== undefined) localDriftBps.value = v as number
}, { immediate: true })

// ── Computed ────────────────────────────────────────
const isActive = computed(() => status.value && status.value.state !== 'IDLE')
const isIdle = computed(() => status.value?.state === 'IDLE')
const isPaused = computed(() => status.value?.is_paused === true)
const canStart = computed(() => status.value?.state === 'IDLE')
const canStop = computed(() => status.value?.is_running || status.value?.state === 'HOLDING')
const canPause = computed(() => status.value?.state === 'ENTERING' || status.value?.state === 'EXITING')
const canResume = computed(() => status.value?.state === 'PAUSED_ENTERING' || status.value?.state === 'PAUSED_EXITING')
// Hot-update fields (min/max spread, timeouts, chase rounds, etc.) can be
// edited only in IDLE/HOLDING/PAUSED. ENTERING/EXITING are blocked because
// the in-flight MakerTakerConfig is a snapshot — edits would not reach the
// running chunk loop. Backend rejects with [CONFIG-EDIT-REJECTED] in that case.
const canEditHotConfig = computed(() => {
  const s = status.value?.state
  return s === 'IDLE' || s === 'HOLDING' || s === 'PAUSED_ENTERING' || s === 'PAUSED_EXITING'
})
// Backwards-compatible alias used by Min/Max-Spread pills.
const canEditSpread = canEditHotConfig

// Current ChunkState (SPREAD_WAIT, MAKER_PLACE, MAKER_WAIT, TAKER_HEDGE,
// REPAIR, VERIFY, CHUNK_DONE, or null when idle). Used for the pause banner
// and other phase-aware UI hints.
const chunkPhase = computed(() => status.value?.execution?.chunk_state ?? null)

// ── Per-exchange auth-health banners ─────────────────
// The /account/health endpoint reports per-exchange auth state. Currently
// only Variational publishes a meaningful tracker (auth_status.ok=false on
// 401/403). When a bot uses Variational on either leg AND that exchange
// reports an auth failure, we surface a clear banner so the user knows to
// refresh the vr-token in Settings instead of staring at an empty position.
const usesVariational = computed(() => {
  return longEx.value === 'variational' || shortEx.value === 'variational'
})
const variationalHealth = computed(() => {
  return accountStore.health?.variational ?? null
})
const variationalAuthBroken = computed(() => {
  const h = variationalHealth.value
  return !!(h && h.tracked && h.ok === false && (h.last_status_code === 401 || h.last_status_code === 403))
})
const variationalAuthHint = computed(() => {
  return variationalHealth.value?.last_error ?? 'Variational authentication failed'
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
// Backend defaults are 0.05 (engine.py:68, config.py:99). The previous
// 0.5 fallback created a brief mismatch on initial SSE load before the
// real config arrived — harmless but confusing in the pill label.
const rawMaxSpread = computed(() => status.value?.config.max_spread_pct ?? 0.05)
const rawExitMinSpread = computed(() => status.value?.config.exit_min_spread_pct ?? -0.5)
const rawExitMaxSpread = computed(() => status.value?.config.exit_max_spread_pct ?? 0.05)
// Pills use ≥ / ≤ to make explicit that these are the range bounds of a
// closed interval [min, max] in which the bot is allowed to trade. The
// state-machine gate (state_machine.py:1609,1628) blocks placement when
// current spread < min or > max — both sides are hard limits, not "best"
// or "worst" hints.
const minSpreadPillLabel = computed(() => `Entry ≥ ${rawMinSpread.value}%`)
const spreadPillLabel = computed(() => `Entry ≤ ${rawMaxSpread.value}%`)
const exitMinSpreadPillLabel = computed(() => `Exit ≥ ${rawExitMinSpread.value}%`)
const exitMaxSpreadPillLabel = computed(() => `Exit ≤ ${rawExitMaxSpread.value}%`)

// Phase-aware tooltip hints — alle Pillen sind in PAUSED_*-States editierbar
// (laut Entscheidung B), aber ein Edit am "falschen" Set wirkt erst auf den
// nächsten Cycle. Tooltip macht das transparent.
const entrySpreadHint = computed(() => {
  if (status.value?.state === 'PAUSED_EXITING')
    return 'Greift erst beim nächsten Entry (nicht im aktuellen Exit)'
  return ''
})
const exitSpreadHint = computed(() => {
  if (status.value?.state === 'PAUSED_ENTERING')
    return 'Greift erst beim nächsten Exit (nicht im aktuellen Entry)'
  return ''
})



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

// OHI data from SSE
const longOhi = computed(() => status.value?.ohi?.long ?? null)
const shortOhi = computed(() => status.value?.ohi?.short ?? null)

// OHI sub-score expand state
const longOhiExpanded = ref(false)
const shortOhiExpanded = ref(false)

// Tooltip state
const activeTooltip = ref<string | null>(null)
function toggleTooltip(key: string, e: Event) {
  e.stopPropagation()
  activeTooltip.value = activeTooltip.value === key ? null : key
}
function closeTooltip() { activeTooltip.value = null }

const TOOLTIPS: Record<string, string> = {
  fn_opt_depth_spread: 'Zusätzliches VWAP-Gate vor jedem Chase-Round. Simuliert den realen Ausführungs-Spread über die aktuelle Orderbuch-Tiefe für die Chunk-Größe. Blockiert den Round, wenn der VWAP-Exec-Spread außerhalb des Min/Max-Spread-Fensters liegt, und wartet, bis das Buch tief genug wird. Der Limit-Preis bleibt BBO ± Offset-Ticks — Depth Spread verändert nur das Timing, nicht den Preis.',
  fn_opt_ohi_monitoring: 'Orderbook Health Index: bewertet die Qualität beider Orderbücher vor jedem Chunk (0–100%). Setzt sich zusammen aus Spread-Enge (40%), Tiefe in USD (30%) und Symmetrie Bid/Ask (30%). Unter dem Mindestwert wird der Chunk übersprungen.',
  fn_opt_funding_history: 'Prüft historische Funding-Rate-Konsistenz via fundingrate.de API. Ein niedriger Konsistenz-Score bedeutet das die Spread-Opportunität instabil war — der Bot blockt den Entry bis der Score über dem Threshold liegt.',
  fn_opt_dynamic_sizing: 'Berechnet die Positionsgröße automatisch aus verfügbarem Kapital, Liquidität beider Orderbücher und dem Max-Slippage-Budget. Verhindert zu große Orders die das Buch bewegen würden.',
  fn_opt_taker_drift_guard: 'Überwacht den Taker-Preis während der Maker-Order wartet. Wenn der Taker-Preis um mehr als N bps driftet wird die Maker-Order gecancelt und der Chunk neu gestartet — schützt vor nachteiligen Fills.',
  // Spread-window explainers — shown via the ⓘ button next to the popover
  // header so the popover itself stays compact (no multi-line subtitles
  // that would push the input off-screen on narrow layouts).
  spread_entry_min: 'Spread must not drop below this value. Acts as a safety floor against stale-book outliers (e.g. one venue freezes while the other moves). Default: −0.5 %.',
  spread_entry_max: 'Spread must not exceed this value. Caps the cost of opening the position — a positive spread means you pay to enter. Default: +0.05 %.',
  spread_exit_min: 'Spread must not drop below this value during exit. Safety floor against stale-book outliers while closing. Default: −0.5 %.',
  spread_exit_max: 'Spread must not exceed this value during exit. Caps the cost of closing the position. Default: +0.05 %.',
}

// Depth Spread Analysis from SSE
const depthAnalysis = computed(() => status.value?.depth_analysis ?? null)

// V4 funding data from SSE
const v4Data = computed(() => status.value?.funding_v4 ?? null)
const v4Score = computed(() => v4Data.value?.confidence_score ?? null)
const v4Consistency = computed(() => v4Data.value?.spread_consistency ?? null)
const v4PairFound = computed(() => v4Data.value?.pair_found ?? false)

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

  // Primary: exact instrument match — works for Extended/GRVT/Nado where the
  // symbol on a position is identical to the symbol in the bot config.
  const exact = accountStore.positions.find(
    p => p.exchange === exchange && p.instrument === instrument
  )
  if (exact) return exact

  // Fallback for Variational: position objects carry a `funding_interval_s`
  // captured when the position was opened, so a position opened during the
  // 1h-funding era still says "P-XRP-USDC-3600" even though the live tradable
  // instrument (and our bot config) is now "P-XRP-USDC-28800". Match by the
  // `underlying` token instead. The Variational client populates `underlying`
  // explicitly (e.g. "XRP", "DOGE") for exactly this case.
  if (exchange === 'variational' && instrument.startsWith('P-')) {
    const tokenMatch = instrument.match(/^P-([^-]+)-/)
    const token = tokenMatch?.[1]
    if (token) {
      const byUnderlying = accountStore.positions.find(
        p => p.exchange === exchange && p.underlying === token,
      )
      if (byUnderlying) return byUnderlying
    }
  }
  return null
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
  const wasOpen = showQtyPopover.value
  showLevPopover.value = false
  showSpreadPopover.value = false
  showMinSpreadPopover.value = false
  showExitMaxSpreadPopover.value = false
  showExitMinSpreadPopover.value = false
  showQtyPopover.value = !wasOpen
}

async function saveQty() {
  showQtyPopover.value = false
  if (editQtyValue.value > 0 && editQtyValue.value !== rawQuantity.value) {
    try { await updateBotConfig(botId.value!, { quantity: editQtyValue.value }) } catch { /* ignore */ }
  }
}

function openLevEditor() {
  editLevValue.value = rawLeverage.value || 1
  const wasOpen = showLevPopover.value
  showQtyPopover.value = false
  showSpreadPopover.value = false
  showMinSpreadPopover.value = false
  showExitMaxSpreadPopover.value = false
  showExitMinSpreadPopover.value = false
  showLevPopover.value = !wasOpen
}

async function saveLev() {
  showLevPopover.value = false
  if (editLevValue.value > 0 && editLevValue.value !== rawLeverage.value) {
    try { await updateBotConfig(botId.value!, { leverage_long: editLevValue.value, leverage_short: editLevValue.value }) } catch { /* ignore */ }
  }
}

// Helper to close all spread popovers (used when opening any one of them)
function _closeAllSpreadPopovers() {
  showSpreadPopover.value = false
  showMinSpreadPopover.value = false
  showExitMaxSpreadPopover.value = false
  showExitMinSpreadPopover.value = false
  showQtyPopover.value = false
  showLevPopover.value = false
}

function openMinSpreadEditor() {
  editMinSpreadValue.value = rawMinSpread.value
  const wasOpen = showMinSpreadPopover.value
  _closeAllSpreadPopovers()
  showMinSpreadPopover.value = !wasOpen
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
  const wasOpen = showSpreadPopover.value
  _closeAllSpreadPopovers()
  showSpreadPopover.value = !wasOpen
}

async function saveSpread() {
  showSpreadPopover.value = false
  const val = Number(editSpreadValue.value)
  // Accept any numeric value, including negatives — a negative max_spread_pct
  // means "bot only enters when short leg is at least |val|% more expensive
  // than long leg", e.g. -0.02 demands ≥ 2 bp favour before placing a maker.
  if (!isNaN(val)) {
    try { await updateBotConfig(botId.value!, { max_spread_pct: val }) } catch (e) { console.error('saveSpread failed:', e) }
  }
}

// ── Exit-Spread editors (separate from entry) ─────────────
function openExitMinSpreadEditor() {
  editExitMinSpreadValue.value = rawExitMinSpread.value
  const wasOpen = showExitMinSpreadPopover.value
  _closeAllSpreadPopovers()
  showExitMinSpreadPopover.value = !wasOpen
}

async function saveExitMinSpread() {
  showExitMinSpreadPopover.value = false
  const val = Number(editExitMinSpreadValue.value)
  if (!isNaN(val)) {
    try { await updateBotConfig(botId.value!, { exit_min_spread_pct: val }) } catch (e) { console.error('saveExitMinSpread failed:', e) }
  }
}

function openExitMaxSpreadEditor() {
  editExitMaxSpreadValue.value = rawExitMaxSpread.value
  const wasOpen = showExitMaxSpreadPopover.value
  _closeAllSpreadPopovers()
  showExitMaxSpreadPopover.value = !wasOpen
}

async function saveExitMaxSpread() {
  showExitMaxSpreadPopover.value = false
  const val = Number(editExitMaxSpreadValue.value)
  if (!isNaN(val)) {
    try { await updateBotConfig(botId.value!, { exit_max_spread_pct: val }) } catch (e) { console.error('saveExitMaxSpread failed:', e) }
  }
}

// ── Feature flag toggles ─────────────────────────────
async function toggleFlag(key: string) {
  if (!botId.value || !status.value) return
  const current = status.value.config?.[key] ?? false
  try { await updateBotConfig(botId.value, { [key]: !current }) } catch { /* ignore */ }
}

async function saveNumericFlag(key: string, value: number) {
  if (!botId.value) return
  try { await updateBotConfig(botId.value, { [key]: value }) } catch { /* ignore */ }
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

function handleBack() {
  // Go back to previous page if history exists, otherwise default to bots page
  if (window.history.length > 1) {
    router.back()
  } else {
    router.push('/bots')
  }
}

function openTimerEditor() {
  if (!status.value) return
  const h = status.value.timer.duration_h || 0
  const m = status.value.timer.duration_m || 0
  timerMinutes.value = h * 60 + m || 720
  const wasOpen = showTimerPopover.value
  showQtyPopover.value = false
  showLevPopover.value = false
  showSpreadPopover.value = false
  showMinSpreadPopover.value = false
  showExitMaxSpreadPopover.value = false
  showExitMinSpreadPopover.value = false
  showTimerPopover.value = !wasOpen
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

let healthPoll: ReturnType<typeof setInterval> | null = null

onMounted(async () => {
  if (!botId.value) { router.push('/'); return }
  document.addEventListener('click', closeTooltip)
  await accountStore.loadAccounts()
  await accountStore.loadPositions()
  // Load per-exchange auth health so the Variational "token expired/revoked"
  // banner can render. Polled every 15s along with positions.
  accountStore.loadHealth()
  accountPoll = setInterval(() => accountStore.loadAccounts(), 15000)
  positionPoll = setInterval(() => accountStore.loadPositions(), 15000)
  healthPoll = setInterval(() => accountStore.loadHealth(), 15000)
  // Load live funding immediately and poll every 30s
  loadLiveFunding()
  fundingPoll = setInterval(loadLiveFunding, 30000)
  // Reload when tokenName resolves (e.g. after SSE connects)
  watch(tokenName, () => loadLiveFunding())
  // Tick clock every second for funding countdown
  clockTick = setInterval(() => { nowSeconds.value = Math.floor(Date.now() / 1000) }, 1000)
  // Load full fill history once; SSE-streamed status.fills keeps the
  // tail in sync after this initial load.
  loadFillsHistory()
})

onUnmounted(() => {
  document.removeEventListener('click', closeTooltip)
  if (accountPoll) clearInterval(accountPoll)
  if (positionPoll) clearInterval(positionPoll)
  if (fundingPoll) clearInterval(fundingPoll)
  if (healthPoll) clearInterval(healthPoll)
  if (clockTick) clearInterval(clockTick)
})
</script>

<template>
  <div :class="$style.page">
    <!-- Back button -->
    <div :class="$style.back">
      <Button variant="ghost" size="sm" @click="handleBack">← Back</Button>
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
          <!-- Entry Min Spread pill + popover -->
          <div :class="[$style.pill, canEditSpread && $style.pillEditable]"
               :title="entrySpreadHint"
               @click="canEditSpread && openMinSpreadEditor()">
            {{ minSpreadPillLabel }}
            <div v-if="showMinSpreadPopover && canEditSpread" :class="$style.popover" @click.stop>
              <div :class="$style.popoverHeader">
                <Typography size="text-xs" weight="semibold" color="secondary">Entry Min Spread % (≥, safety floor)</Typography>
                <div :class="$style.tooltipWrap">
                  <button :class="$style.tooltipBtn" @click="toggleTooltip('spread_entry_min', $event)">?</button>
                  <div v-if="activeTooltip === 'spread_entry_min'" :class="$style.tooltipBox">{{ TOOLTIPS.spread_entry_min }}</div>
                </div>
              </div>
              <Typography v-if="entrySpreadHint" size="text-xs" color="warning">
                {{ entrySpreadHint }}
              </Typography>
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
          <!-- Entry Max Spread pill + popover -->
          <div :class="[$style.pill, canEditSpread && $style.pillEditable]"
               :title="entrySpreadHint"
               @click="canEditSpread && openSpreadEditor()">
            {{ spreadPillLabel }}
            <div v-if="showSpreadPopover && canEditSpread" :class="$style.popover" @click.stop>
              <div :class="$style.popoverHeader">
                <Typography size="text-xs" weight="semibold" color="secondary">Entry Max Spread % (≤, cost cap)</Typography>
                <div :class="$style.tooltipWrap">
                  <button :class="$style.tooltipBtn" @click="toggleTooltip('spread_entry_max', $event)">?</button>
                  <div v-if="activeTooltip === 'spread_entry_max'" :class="$style.tooltipBox">{{ TOOLTIPS.spread_entry_max }}</div>
                </div>
              </div>
              <Typography v-if="entrySpreadHint" size="text-xs" color="warning">
                {{ entrySpreadHint }}
              </Typography>
              <input
                v-model.number="editSpreadValue"
                :class="$style.popoverInput"
                type="number"
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
          <!-- Exit Min Spread pill + popover -->
          <div :class="[$style.pill, canEditSpread && $style.pillEditable]"
               :title="exitSpreadHint"
               @click="canEditSpread && openExitMinSpreadEditor()">
            {{ exitMinSpreadPillLabel }}
            <div v-if="showExitMinSpreadPopover && canEditSpread" :class="$style.popover" @click.stop>
              <div :class="$style.popoverHeader">
                <Typography size="text-xs" weight="semibold" color="secondary">Exit Min Spread % (≥, safety floor)</Typography>
                <div :class="$style.tooltipWrap">
                  <button :class="$style.tooltipBtn" @click="toggleTooltip('spread_exit_min', $event)">?</button>
                  <div v-if="activeTooltip === 'spread_exit_min'" :class="$style.tooltipBox">{{ TOOLTIPS.spread_exit_min }}</div>
                </div>
              </div>
              <Typography v-if="exitSpreadHint" size="text-xs" color="warning">
                {{ exitSpreadHint }}
              </Typography>
              <input
                v-model.number="editExitMinSpreadValue"
                :class="$style.popoverInput"
                type="number"
                step="0.1"
              />
              <div v-if="priceSpreadPct !== null" :class="$style.popoverUsd">
                <Typography size="text-xs" :style="{ color: priceSpreadPct >= editExitMinSpreadValue ? '#22c55e' : '#ef4444' }">
                  Current: {{ priceSpreadPct.toFixed(4) }}%
                </Typography>
              </div>
              <Button variant="solid" color="success" size="sm" @click="saveExitMinSpread">Apply</Button>
            </div>
          </div>
          <!-- Exit Max Spread pill + popover -->
          <div :class="[$style.pill, canEditSpread && $style.pillEditable]"
               :title="exitSpreadHint"
               @click="canEditSpread && openExitMaxSpreadEditor()">
            {{ exitMaxSpreadPillLabel }}
            <div v-if="showExitMaxSpreadPopover && canEditSpread" :class="$style.popover" @click.stop>
              <div :class="$style.popoverHeader">
                <Typography size="text-xs" weight="semibold" color="secondary">Exit Max Spread % (≤, cost cap)</Typography>
                <div :class="$style.tooltipWrap">
                  <button :class="$style.tooltipBtn" @click="toggleTooltip('spread_exit_max', $event)">?</button>
                  <div v-if="activeTooltip === 'spread_exit_max'" :class="$style.tooltipBox">{{ TOOLTIPS.spread_exit_max }}</div>
                </div>
              </div>
              <Typography v-if="exitSpreadHint" size="text-xs" color="warning">
                {{ exitSpreadHint }}
              </Typography>
              <input
                v-model.number="editExitMaxSpreadValue"
                :class="$style.popoverInput"
                type="number"
                step="0.01"
              />
              <div v-if="priceSpreadPct !== null" :class="$style.popoverUsd">
                <Typography size="text-xs" :style="{ color: priceSpreadPct <= editExitMaxSpreadValue ? '#22c55e' : '#ef4444' }">
                  Current: {{ priceSpreadPct.toFixed(4) }}%
                </Typography>
              </div>
              <Button variant="solid" color="success" size="sm" @click="saveExitMaxSpread">Apply</Button>
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

      <!-- ── Pause Hinweisbanner ── -->
      <div v-if="isPaused" :class="$style.pauseBanner">
        <Typography size="text-sm" weight="semibold">
          ⏸ Bot pausiert<span v-if="chunkPhase"> (Phase: {{ chunkPhase }})</span>
        </Typography>
        <Typography size="text-xs" color="secondary">
          Edits an Min/Max-Spread, Timeouts und Chase-Parametern werden bei
          <strong>Resume</strong> übernommen und in der laufenden Execution sofort
          wirksam. Andere Felder (Exchange, Symbol, Quantity, Leverage) erfordern
          Stop + Restart.
        </Typography>
      </div>

      <!-- ── Variational-Auth-Banner (zeigt nur wenn Bot Variational nutzt UND Auth failed) ── -->
      <div v-if="usesVariational && variationalAuthBroken" :class="$style.authErrorBanner">
        <Typography size="text-sm" weight="semibold">
          🔴 Variational: Auth-Fehler ({{ variationalHealth?.last_status_code ?? '?' }})
        </Typography>
        <Typography size="text-xs" color="secondary">
          {{ variationalAuthHint }}
        </Typography>
        <Typography size="text-xs" color="tertiary">
          → Geh in
          <RouterLink to="/settings"><strong>Settings</strong></RouterLink>
          und aktualisiere den <strong>vr-token</strong>. Nach dem Speichern Browser-Tab
          zu omni.variational.io schließen, damit Variational dein Token nicht
          erneut serverseitig rotiert.
        </Typography>
      </div>

      <!-- ── Hinweis: Edit gesperrt während aktiver Execution ── -->
      <div v-else-if="status?.state === 'ENTERING' || status?.state === 'EXITING'" :class="$style.execLockBanner">
        <Typography size="text-xs" color="secondary">
          ▶ Bot läuft ({{ stateLabel }}). Edits an Spread-Bounds, Timeouts etc.
          sind gesperrt — bitte erst <strong>Pause</strong> drücken. Live-Änderungen
          während laufender TWAP-Execution würden den aktuellen Chunk-Loop nicht
          erreichen (Snapshot-Mismatch).
        </Typography>
      </div>

      <!-- ── Advanced Settings Panel ── -->
      <div :class="$style.advancedToggle" @click="showAdvancedPanel = !showAdvancedPanel">
        <Typography size="text-xs" weight="semibold" color="secondary">
          {{ showAdvancedPanel ? '▾' : '▸' }} Advanced Settings
        </Typography>
      </div>
      <div v-if="showAdvancedPanel" :class="$style.advancedPanel">
        <!-- OMS Connection Status -->
        <div v-if="status?.data" :class="$style.omsStatus">
          <div :class="$style.flagHeader">
            <StatusDot :status="status.data.oms_active ? 'success' : 'neutral'" />
            <Typography size="text-sm" weight="semibold">
              OMS {{ status.data.oms_active ? 'Connected' : 'Disabled' }}
            </Typography>
          </div>
          <Typography v-if="status.data.oms_active" size="text-xs" color="tertiary">
            Polling from {{ status.data.oms_url }}
          </Typography>
          <Typography v-else size="text-xs" color="tertiary">
            Direct WS connections (no shared monitor)
          </Typography>
        </div>

        <div :class="$style.flagGrid">
          <!-- Depth-Aware Spread -->
          <div :class="$style.flagItem">
            <div :class="$style.flagHeader">
              <label :class="$style.flagToggle">
                <input type="checkbox" :checked="status.config?.fn_opt_depth_spread" @change="toggleFlag('fn_opt_depth_spread')" />
                <span :class="$style.flagSlider"></span>
              </label>
              <Typography size="text-sm" weight="semibold">Depth Spread</Typography>
              <div :class="$style.tooltipWrap">
                <button :class="$style.tooltipBtn" @click="toggleTooltip('fn_opt_depth_spread', $event)">?</button>
                <div v-if="activeTooltip === 'fn_opt_depth_spread'" :class="$style.tooltipBox">{{ TOOLTIPS.fn_opt_depth_spread }}</div>
              </div>
            </div>
            <Typography size="text-xs" color="tertiary">VWAP-Gate zusätzlich zum BBO-Gate — blockiert Round, wenn Exec-Spread außerhalb [min, max]</Typography>
          </div>
          <!-- Max Slippage — hidden when Depth Spread is ON.
               The field stays in config (used as an upper-bound safety cap
               inside the Depth Spread gate), but it's not the primary knob
               anymore when VWAP-Window is active. -->
          <div v-if="!status.config?.fn_opt_depth_spread" :class="$style.flagItem">
            <div :class="$style.flagHeader">
              <Typography size="text-sm" weight="semibold">Max Slippage</Typography>
              <div :class="$style.flagInput">
                <input
                  type="number"
                  v-model.number="localSlippageBps"
                  min="1" max="100" step="1"
                  :class="$style.miniInput"
                  @focus="slippageFocused = true"
                  @blur="slippageFocused = false; saveNumericFlag('fn_opt_max_slippage_bps', localSlippageBps)"
                  @keydown.enter="($event.target as HTMLInputElement).blur()"
                />
                <Typography size="text-xs" color="tertiary">bps</Typography>
              </div>
            </div>
            <Typography size="text-xs" color="tertiary">Oberer Sicherheits-Cap für Slippage über BBO hinaus (wird vom Depth-Spread-Gate zusätzlich geprüft)</Typography>
          </div>
          <!-- OHI Monitoring -->
          <div :class="$style.flagItem">
            <div :class="$style.flagHeader">
              <label :class="$style.flagToggle">
                <input type="checkbox" :checked="status.config?.fn_opt_ohi_monitoring" @change="toggleFlag('fn_opt_ohi_monitoring')" />
                <span :class="$style.flagSlider"></span>
              </label>
              <Typography size="text-sm" weight="semibold">OHI Monitoring</Typography>
              <div :class="$style.tooltipWrap">
                <button :class="$style.tooltipBtn" @click="toggleTooltip('fn_opt_ohi_monitoring', $event)">?</button>
                <div v-if="activeTooltip === 'fn_opt_ohi_monitoring'" :class="$style.tooltipBox">{{ TOOLTIPS.fn_opt_ohi_monitoring }}</div>
              </div>
            </div>
            <Typography size="text-xs" color="tertiary">Orderbuch-Gesundheit: Spread 40% + Tiefe 30% + Symmetrie 30%</Typography>
          </div>
          <!-- Funding History V4 -->
          <div :class="$style.flagItem">
            <div :class="$style.flagHeader">
              <label :class="$style.flagToggle">
                <input type="checkbox" :checked="status.config?.fn_opt_funding_history" @change="toggleFlag('fn_opt_funding_history')" />
                <span :class="$style.flagSlider"></span>
              </label>
              <Typography size="text-sm" weight="semibold">V4 Funding History</Typography>
              <div :class="$style.tooltipWrap">
                <button :class="$style.tooltipBtn" @click="toggleTooltip('fn_opt_funding_history', $event)">?</button>
                <div v-if="activeTooltip === 'fn_opt_funding_history'" :class="$style.tooltipBox">{{ TOOLTIPS.fn_opt_funding_history }}</div>
              </div>
            </div>
            <Typography size="text-xs" color="tertiary">Historische Spread-Konsistenz via fundingrate.de API</Typography>
          </div>
          <!-- Dynamic Sizing -->
          <div :class="$style.flagItem">
            <div :class="$style.flagHeader">
              <label :class="$style.flagToggle">
                <input type="checkbox" :checked="status.config?.fn_opt_dynamic_sizing" @change="toggleFlag('fn_opt_dynamic_sizing')" />
                <span :class="$style.flagSlider"></span>
              </label>
              <Typography size="text-sm" weight="semibold">Dynamic Sizing</Typography>
              <div :class="$style.tooltipWrap">
                <button :class="$style.tooltipBtn" @click="toggleTooltip('fn_opt_dynamic_sizing', $event)">?</button>
                <div v-if="activeTooltip === 'fn_opt_dynamic_sizing'" :class="$style.tooltipBox">{{ TOOLTIPS.fn_opt_dynamic_sizing }}</div>
              </div>
            </div>
            <Typography size="text-xs" color="tertiary">Positionsgröße automatisch aus Kapital + Liquidität berechnen</Typography>
          </div>
          <!-- Taker Drift Guard -->
          <div :class="$style.flagItem">
            <div :class="$style.flagHeader">
              <label :class="$style.flagToggle">
                <input type="checkbox" :checked="status.config?.fn_opt_taker_drift_guard" @change="toggleFlag('fn_opt_taker_drift_guard')" />
                <span :class="$style.flagSlider"></span>
              </label>
              <Typography size="text-sm" weight="semibold">Taker Drift Guard</Typography>
              <div :class="$style.tooltipWrap">
                <button :class="$style.tooltipBtn" @click="toggleTooltip('fn_opt_taker_drift_guard', $event)">?</button>
                <div v-if="activeTooltip === 'fn_opt_taker_drift_guard'" :class="$style.tooltipBox">{{ TOOLTIPS.fn_opt_taker_drift_guard }}</div>
              </div>
            </div>
            <Typography size="text-xs" color="tertiary">Maker canceln wenn Taker-Preis während Wait driftet</Typography>
          </div>
          <!-- Max Taker Drift -->
          <div :class="$style.flagItem">
            <div :class="$style.flagHeader">
              <Typography size="text-sm" weight="semibold">Max Drift</Typography>
              <div :class="$style.flagInput">
                <input
                  type="number"
                  v-model.number="localDriftBps"
                  min="1" max="50" step="1"
                  :class="$style.miniInput"
                  @focus="driftBpsFocused = true"
                  @blur="driftBpsFocused = false; saveNumericFlag('fn_opt_max_taker_drift_bps', localDriftBps)"
                  @keydown.enter="($event.target as HTMLInputElement).blur()"
                />
                <Typography size="text-xs" color="tertiary">bps</Typography>
              </div>
            </div>
            <Typography size="text-xs" color="tertiary">Max Taker-Preisdrift während Maker wartet (für Drift Guard)</Typography>
          </div>
          <!-- Min Funding Consistency -->
          <div :class="$style.flagItem">
            <div :class="$style.flagHeader">
              <Typography size="text-sm" weight="semibold">Min Consistency</Typography>
              <div :class="$style.flagInput">
                <input
                  type="number"
                  v-model.number="localMinConsistency"
                  min="0" max="1" step="0.05"
                  :class="$style.miniInput"
                  @focus="consistencyFocused = true"
                  @blur="consistencyFocused = false; saveNumericFlag('fn_opt_min_funding_consistency', localMinConsistency)"
                  @keydown.enter="($event.target as HTMLInputElement).blur()"
                />
              </div>
            </div>
            <Typography size="text-xs" color="tertiary">V4 Konsistenz-Schwellenwert (0–1, für V4 Funding History)</Typography>
          </div>
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
          <!-- OHI Bar (Long) -->
          <div v-if="longOhi && longOhi.ohi > 0" :class="$style.ohiBar">
            <div :class="[$style.ohiLabel, $style.ohiLabelClickable]" @click="longOhiExpanded = !longOhiExpanded">
              <Typography size="text-xs" color="tertiary">OHI {{ longOhiExpanded ? '▾' : '▸' }}</Typography>
              <Typography size="text-xs" :color="longOhi.ohi >= 0.7 ? 'success' : longOhi.ohi >= 0.4 ? 'primary' : 'warning'">
                {{ (longOhi.ohi * 100).toFixed(0) }}%
              </Typography>
            </div>
            <div :class="$style.ohiTrack">
              <div
                :class="$style.ohiFill"
                :style="{ width: (longOhi.ohi * 100) + '%', background: longOhi.ohi >= 0.7 ? '#22c55e' : longOhi.ohi >= 0.4 ? '#6366f1' : '#f59e0b' }"
              />
            </div>
            <div :class="$style.ohiMeta">
              <Typography size="text-xs" color="tertiary">{{ longOhi.spread_bps }}bps</Typography>
              <Typography size="text-xs" color="tertiary">${{ ((longOhi.depth_usd ?? 0) / 1000).toFixed(0) }}k depth</Typography>
            </div>
            <div v-if="longOhiExpanded" :class="$style.ohiSubScores">
              <div :class="$style.ohiSubRow">
                <Typography size="text-xs" color="tertiary">Spread</Typography>
                <Typography size="text-xs" :color="(longOhi.spread_score ?? 0) >= 0.7 ? 'success' : (longOhi.spread_score ?? 0) >= 0.4 ? 'primary' : 'warning'">{{ ((longOhi.spread_score ?? 0) * 100).toFixed(0) }}%</Typography>
              </div>
              <div :class="$style.ohiSubRow">
                <Typography size="text-xs" color="tertiary">Depth</Typography>
                <Typography size="text-xs" :color="(longOhi.depth_score ?? 0) >= 0.7 ? 'success' : (longOhi.depth_score ?? 0) >= 0.4 ? 'primary' : 'warning'">{{ ((longOhi.depth_score ?? 0) * 100).toFixed(0) }}%</Typography>
              </div>
              <div :class="$style.ohiSubRow">
                <Typography size="text-xs" color="tertiary">Symmetry</Typography>
                <Typography size="text-xs" :color="(longOhi.symmetry_score ?? 0) >= 0.7 ? 'success' : (longOhi.symmetry_score ?? 0) >= 0.4 ? 'primary' : 'warning'">{{ ((longOhi.symmetry_score ?? 0) * 100).toFixed(0) }}%</Typography>
              </div>
            </div>
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
                <Typography size="text-sm">{{ Math.abs(Number(positionForExchange(longEx)?.size ?? 0)) }}</Typography>
              </div>
              <div :class="$style.posRow">
                <Typography size="text-xs" color="tertiary">Value</Typography>
                <Typography size="text-sm">${{ (Math.abs(Number(positionForExchange(longEx)?.size ?? 0)) * Number(positionForExchange(longEx)?.mark_price ?? 0)).toFixed(2) }}</Typography>
              </div>
              <div :class="$style.posRow">
                <Typography size="text-xs" color="tertiary">Entry</Typography>
                <Typography size="text-sm">${{ Number(positionForExchange(longEx)?.entry_price ?? 0).toFixed(4) }}</Typography>
              </div>
              <div :class="$style.posRow">
                <Typography size="text-xs" color="tertiary">Liq.</Typography>
                <Typography size="text-sm">{{ Number(positionForExchange(longEx)?.est_liquidation_price ?? 0) ? '$' + Number(positionForExchange(longEx)?.est_liquidation_price ?? 0).toFixed(4) : '—' }}</Typography>
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
            <Typography v-if="v4Score !== null" size="text-xs" :color="v4Score >= 3 ? 'success' : v4Score >= 2 ? 'primary' : 'warning'">
              {{ v4Score }}/4 quant score
            </Typography>
            <Typography v-else size="text-xs" color="tertiary">—/4 quant score</Typography>
          </div>

          <!-- V4 Funding Details -->
          <div v-if="v4Data && v4PairFound" :class="$style.v4Details">
            <div :class="$style.v4Row">
              <Typography size="text-xs" color="tertiary">Spread APR (V4)</Typography>
              <Typography size="text-xs" :color="(v4Data.spread_apr ?? 0) >= 0 ? 'success' : 'error'">
                {{ ((v4Data.spread_apr ?? 0) * 100).toFixed(2) }}%
              </Typography>
            </div>
            <div :class="$style.v4Row">
              <Typography size="text-xs" color="tertiary">Consistency</Typography>
              <Typography size="text-xs" :color="(v4Consistency ?? 0) >= 0.5 ? 'success' : 'warning'">
                {{ (v4Consistency ?? 0).toFixed(2) }}
              </Typography>
            </div>
            <div :class="$style.v4Row">
              <Typography size="text-xs" color="tertiary">Vol. Depth</Typography>
              <Typography size="text-xs">{{ (v4Data.volume_depth ?? 0).toFixed(2) }}</Typography>
            </div>
            <div :class="$style.v4Row">
              <Typography size="text-xs" color="tertiary">Stability</Typography>
              <Typography size="text-xs">{{ (v4Data.rate_stability ?? 0).toFixed(2) }}</Typography>
            </div>
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

          <!-- Depth Spread Analysis -->
          <div v-if="depthAnalysis && depthAnalysis.slippage_bps != null" :class="[$style.depthWidget, !depthAnalysis.is_acceptable && $style.depthWidgetWarn]">
            <div :class="$style.depthHeader">
              <Typography size="text-xs" weight="semibold" color="secondary">Depth Spread</Typography>
              <Chip :variant="depthAnalysis.is_acceptable ? 'success' : 'warning'" size="sm">
                {{ Number(depthAnalysis.slippage_bps).toFixed(1) }}bps slip
              </Chip>
            </div>
            <div :class="$style.depthGrid">
              <div :class="$style.depthRow">
                <Typography size="text-xs" color="tertiary">BBO Spread</Typography>
                <Typography size="text-xs" :color="Number(depthAnalysis.bbo_spread_pct) <= 0 ? 'success' : 'primary'">{{ Number(depthAnalysis.bbo_spread_pct).toFixed(4) }}%</Typography>
              </div>
              <div :class="$style.depthRow">
                <Typography size="text-xs" color="tertiary">Exec Spread</Typography>
                <Typography size="text-xs" :color="Number(depthAnalysis.exec_spread_pct) <= 0 ? 'success' : 'primary'">{{ Number(depthAnalysis.exec_spread_pct).toFixed(4) }}%</Typography>
              </div>
              <div :class="$style.depthRow">
                <Typography size="text-xs" color="tertiary">Long VWAP</Typography>
                <Typography size="text-xs">${{ Number(depthAnalysis.long_fill_price).toFixed(4) }}</Typography>
              </div>
              <div :class="$style.depthRow">
                <Typography size="text-xs" color="tertiary">Short VWAP</Typography>
                <Typography size="text-xs">${{ Number(depthAnalysis.short_fill_price).toFixed(4) }}</Typography>
              </div>
            </div>
          </div>

          <!-- Mark Price -->
          <div :class="$style.pnlRow">
            <div :class="$style.pnlItem">
              <Typography size="text-xs" color="tertiary">Long Mark</Typography>
              <Typography size="text-sm">{{ longPrice && longPrice.mid != null ? '$' + Number(longPrice.mid).toFixed(4) : '—' }}</Typography>
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
              <Typography size="text-sm">{{ shortPrice && shortPrice.mid != null ? '$' + Number(shortPrice.mid).toFixed(4) : '—' }}</Typography>
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
          <!-- OHI Bar (Short) -->
          <div v-if="shortOhi && shortOhi.ohi > 0" :class="$style.ohiBar">
            <div :class="[$style.ohiLabel, $style.ohiLabelClickable]" @click="shortOhiExpanded = !shortOhiExpanded">
              <Typography size="text-xs" color="tertiary">OHI {{ shortOhiExpanded ? '▾' : '▸' }}</Typography>
              <Typography size="text-xs" :color="shortOhi.ohi >= 0.7 ? 'success' : shortOhi.ohi >= 0.4 ? 'primary' : 'warning'">
                {{ (shortOhi.ohi * 100).toFixed(0) }}%
              </Typography>
            </div>
            <div :class="$style.ohiTrack">
              <div
                :class="$style.ohiFill"
                :style="{ width: (shortOhi.ohi * 100) + '%', background: shortOhi.ohi >= 0.7 ? '#22c55e' : shortOhi.ohi >= 0.4 ? '#6366f1' : '#f59e0b' }"
              />
            </div>
            <div :class="$style.ohiMeta">
              <Typography size="text-xs" color="tertiary">{{ shortOhi.spread_bps }}bps</Typography>
              <Typography size="text-xs" color="tertiary">${{ ((shortOhi.depth_usd ?? 0) / 1000).toFixed(0) }}k depth</Typography>
            </div>
            <div v-if="shortOhiExpanded" :class="$style.ohiSubScores">
              <div :class="$style.ohiSubRow">
                <Typography size="text-xs" color="tertiary">Spread</Typography>
                <Typography size="text-xs" :color="(shortOhi.spread_score ?? 0) >= 0.7 ? 'success' : (shortOhi.spread_score ?? 0) >= 0.4 ? 'primary' : 'warning'">{{ ((shortOhi.spread_score ?? 0) * 100).toFixed(0) }}%</Typography>
              </div>
              <div :class="$style.ohiSubRow">
                <Typography size="text-xs" color="tertiary">Depth</Typography>
                <Typography size="text-xs" :color="(shortOhi.depth_score ?? 0) >= 0.7 ? 'success' : (shortOhi.depth_score ?? 0) >= 0.4 ? 'primary' : 'warning'">{{ ((shortOhi.depth_score ?? 0) * 100).toFixed(0) }}%</Typography>
              </div>
              <div :class="$style.ohiSubRow">
                <Typography size="text-xs" color="tertiary">Symmetry</Typography>
                <Typography size="text-xs" :color="(shortOhi.symmetry_score ?? 0) >= 0.7 ? 'success' : (shortOhi.symmetry_score ?? 0) >= 0.4 ? 'primary' : 'warning'">{{ ((shortOhi.symmetry_score ?? 0) * 100).toFixed(0) }}%</Typography>
              </div>
            </div>
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
                <Typography size="text-sm">{{ Math.abs(Number(positionForExchange(shortEx)?.size ?? 0)) }}</Typography>
              </div>
              <div :class="$style.posRow">
                <Typography size="text-xs" color="tertiary">Value</Typography>
                <Typography size="text-sm">${{ (Math.abs(Number(positionForExchange(shortEx)?.size ?? 0)) * Number(positionForExchange(shortEx)?.mark_price ?? 0)).toFixed(2) }}</Typography>
              </div>
              <div :class="$style.posRow">
                <Typography size="text-xs" color="tertiary">Entry</Typography>
                <Typography size="text-sm">${{ Number(positionForExchange(shortEx)?.entry_price ?? 0).toFixed(4) }}</Typography>
              </div>
              <div :class="$style.posRow">
                <Typography size="text-xs" color="tertiary">Liq.</Typography>
                <Typography size="text-sm">{{ Number(positionForExchange(shortEx)?.est_liquidation_price ?? 0) ? '$' + Number(positionForExchange(shortEx)?.est_liquidation_price ?? 0).toFixed(4) : '—' }}</Typography>
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

      <!-- ── Filled Orders ── -->
      <div :class="$style.fillsSection">
        <div :class="$style.fillsHeader">
          <Typography size="text-md" weight="semibold" color="secondary" as="h3">Filled Orders</Typography>
          <Typography size="text-xs" color="tertiary">
            {{ allFills.length }} fill{{ allFills.length === 1 ? '' : 's' }} total
          </Typography>
        </div>
        <div :class="$style.fillsContainer">
          <div v-if="!fillsLoaded" :class="$style.fillsEmpty">
            <Typography size="text-sm" color="tertiary">Loading…</Typography>
          </div>
          <div v-else-if="fillsLoadError" :class="$style.fillsEmpty">
            <Typography size="text-sm" color="error">{{ fillsLoadError }}</Typography>
          </div>
          <div v-else-if="!allFills.length" :class="$style.fillsEmpty">
            <Typography size="text-sm" color="tertiary">No filled orders yet</Typography>
          </div>
          <div
            v-for="fill in allFills"
            :key="`${fill.action}:${fill.chunk_index}:${fill.ts}`"
            :class="[$style.fillEntry, fill.error && $style['fillEntry--error']]"
          >
            <!-- Top row: timestamp + action chip + chunk index -->
            <div :class="$style.fillMeta">
              <Typography size="text-xs" color="tertiary" :class="$style.fillTime">
                {{ new Date(fill.ts * 1000).toLocaleString() }}
              </Typography>
              <Chip
                :variant="fill.action === 'ENTRY' ? 'info' : fill.action === 'EXIT' ? 'neutral' : 'neutral'"
                size="sm"
              >
                {{ fill.action }}
              </Chip>
              <Typography size="text-xs" color="tertiary">Chunk #{{ fill.chunk_index }}</Typography>
              <Typography v-if="fill.error" size="text-xs" color="error" :class="$style.fillErr">
                {{ fill.error }}
              </Typography>
            </div>
            <!-- 3-column row: long | spread | short -->
            <div :class="$style.fillRow">
              <!-- Long leg (left) -->
              <div :class="$style.fillLeg">
                <div :class="$style.fillLegHead">
                  <img v-if="DEX_LOGOS[fill.long_exchange]" :src="DEX_LOGOS[fill.long_exchange]" :class="$style.fillDexLogo" />
                  <Typography size="text-xs" weight="semibold" color="secondary">
                    LONG · {{ fill.long_exchange || '—' }}
                  </Typography>
                </div>
                <Typography size="text-sm">
                  {{ fill.long_qty ? Number(fill.long_qty).toFixed(6) : '—' }}
                  <span v-if="fill.long_price" :class="$style.fillAt">@</span>
                  <span v-if="fill.long_price">${{ Number(fill.long_price).toFixed(4) }}</span>
                </Typography>
              </div>
              <!-- Spread (centre) — USD per unit, dynamic precision so
                   high-priced assets (BTC) show $X.XX while low-priced
                   ones (ARB, sub-cent spreads) keep readable digits. -->
              <div :class="$style.fillSpread">
                <Typography size="text-xs" color="tertiary">Spread</Typography>
                <Typography
                  size="text-sm"
                  weight="semibold"
                  :style="{ color: fill.spread_usd === null ? 'inherit' : (fill.spread_usd < 0 ? '#22c55e' : fill.spread_usd > 0 ? '#ef4444' : 'inherit') }"
                >
                  {{ formatSpreadUsd(fill.spread_usd) }}
                </Typography>
              </div>
              <!-- Short leg (right) -->
              <div :class="[$style.fillLeg, $style.fillLegRight]">
                <div :class="$style.fillLegHead">
                  <img v-if="DEX_LOGOS[fill.short_exchange]" :src="DEX_LOGOS[fill.short_exchange]" :class="$style.fillDexLogo" />
                  <Typography size="text-xs" weight="semibold" color="secondary">
                    SHORT · {{ fill.short_exchange || '—' }}
                  </Typography>
                </div>
                <Typography size="text-sm">
                  {{ fill.short_qty ? Number(fill.short_qty).toFixed(6) : '—' }}
                  <span v-if="fill.short_price" :class="$style.fillAt">@</span>
                  <span v-if="fill.short_price">${{ Number(fill.short_price).toFixed(4) }}</span>
                </Typography>
              </div>
            </div>
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
              :variant="entry.cat === 'FILL' ? 'success' : entry.cat === 'RISK' ? 'warning' : entry.cat === 'ORDER' ? 'info' : entry.cat === 'CONFIG' ? 'info' : entry.cat === 'OHI' ? 'neutral' : entry.cat === 'SPREAD' ? 'neutral' : entry.extra?.level === 'error' ? 'error' : 'neutral'"
              size="sm"
              :class="[$style.logCat, entry.cat === 'CONFIG' && $style['logCat--config'], entry.cat === 'OHI' && $style['logCat--ohi'], entry.cat === 'SPREAD' && $style['logCat--spread']]"
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

/* Header row inside the popover — title text on the left, ⓘ tooltip
   button on the right. The tooltip box itself is `position: absolute`
   with z-index 200 so it can overflow the popover bounds without
   pushing the input field down or breaking the popover layout. */
.popoverHeader {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  position: relative;
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

/* ── Filled Orders ── */
.fillsSection {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.fillsHeader {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-3);
}

.fillsContainer {
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  max-height: 480px;
  overflow-y: auto;
}

.fillsEmpty {
  padding: var(--space-8);
  text-align: center;
}

.fillEntry {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-stroke-divider);
}
.fillEntry:last-child { border-bottom: none; }

.fillEntry--error { background: var(--color-error-bg); }

.fillMeta {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-wrap: wrap;
}

.fillTime {
  font-variant-numeric: tabular-nums;
}

.fillErr {
  margin-left: auto;
}

.fillRow {
  display: grid;
  grid-template-columns: 1fr auto 1fr;
  align-items: center;
  gap: var(--space-4);
}

.fillLeg {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}

.fillLegRight {
  align-items: flex-end;
  text-align: right;
}

.fillLegHead {
  display: flex;
  align-items: center;
  gap: 6px;
}

.fillDexLogo {
  width: 14px;
  height: 14px;
  object-fit: contain;
}

.fillAt {
  color: var(--color-text-tertiary);
  margin: 0 4px;
}

.fillSpread {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 2px;
  padding: 0 var(--space-3);
  border-left: 1px solid var(--color-stroke-divider);
  border-right: 1px solid var(--color-stroke-divider);
  font-variant-numeric: tabular-nums;
}

@media (max-width: 640px) {
  .fillRow {
    grid-template-columns: 1fr;
    gap: var(--space-2);
  }
  .fillLegRight {
    align-items: flex-start;
    text-align: left;
  }
  .fillSpread {
    border-left: none;
    border-right: none;
    border-top: 1px solid var(--color-stroke-divider);
    border-bottom: 1px solid var(--color-stroke-divider);
    padding: var(--space-1) 0;
    flex-direction: row;
    justify-content: center;
    gap: var(--space-2);
  }
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

/* ── Pause / Exec-Lock Banner ── */
.pauseBanner {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-3) var(--space-4);
  margin-bottom: var(--space-3);
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-warning);
  background: color-mix(in srgb, var(--color-warning) 10%, transparent);
}
.execLockBanner {
  padding: var(--space-2) var(--space-3);
  margin-bottom: var(--space-3);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
}

.authErrorBanner {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-3) var(--space-4);
  margin-bottom: var(--space-3);
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-error);
  background: color-mix(in srgb, var(--color-error) 12%, transparent);
}
.authErrorBanner a {
  color: var(--color-text-primary);
  text-decoration: underline;
}

/* ── Advanced Settings Panel ── */
.advancedToggle {
  cursor: pointer;
  padding: var(--space-2) 0;
  user-select: none;
  opacity: 0.7;
  transition: opacity 0.15s;
}
.advancedToggle:hover { opacity: 1; }

.advancedPanel {
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: var(--space-4);
  margin-bottom: var(--space-3);
}

.omsStatus {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding-bottom: var(--space-3);
  margin-bottom: var(--space-3);
  border-bottom: 1px solid var(--color-stroke-divider);
}

.flagGrid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: var(--space-3);
}

.flagItem {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: var(--space-3);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-bg-primary);
}

.flagHeader {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.flagToggle {
  position: relative;
  display: inline-block;
  width: 32px;
  height: 18px;
  flex-shrink: 0;
}
.flagToggle input { opacity: 0; width: 0; height: 0; }
.flagSlider {
  position: absolute;
  inset: 0;
  border-radius: 9px;
  background: var(--color-white-8);
  transition: background 0.2s;
  cursor: pointer;
}
.flagSlider::before {
  content: '';
  position: absolute;
  left: 2px;
  top: 2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: white;
  transition: transform 0.2s;
}
.flagToggle input:checked + .flagSlider {
  background: var(--color-brand, #6366f1);
}
.flagToggle input:checked + .flagSlider::before {
  transform: translateX(14px);
}

.flagInput {
  display: flex;
  align-items: center;
  gap: 4px;
  margin-left: auto;
}

.miniInput {
  width: 60px;
  padding: 2px 6px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  color: var(--color-text-primary);
  font-size: 12px;
  text-align: center;
  outline: none;
}
.miniInput:focus { border-color: var(--color-brand, #6366f1); }

/* ── OHI Bar ── */
.ohiBar {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: var(--space-2) var(--space-3);
  border-top: 1px solid var(--color-stroke-divider);
}

.ohiLabel {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.ohiTrack {
  height: 4px;
  border-radius: 2px;
  background: var(--color-white-4);
  overflow: hidden;
}

.ohiFill {
  height: 100%;
  border-radius: 2px;
  transition: width 0.3s ease;
}

.ohiMeta {
  display: flex;
  justify-content: space-between;
}

/* ── Tooltip ── */
.tooltipWrap {
  position: relative;
  margin-left: auto;
}

.tooltipBtn {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  color: var(--color-text-tertiary);
  font-size: 10px;
  font-weight: 700;
  line-height: 1;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  padding: 0;
}
.tooltipBtn:hover {
  border-color: var(--color-brand, #6366f1);
  color: var(--color-brand, #6366f1);
}

.tooltipBox {
  position: absolute;
  top: calc(100% + 6px);
  right: 0;
  width: 260px;
  padding: 10px 12px;
  background: var(--color-bg-primary);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-md);
  box-shadow: 0 6px 24px rgba(0,0,0,0.45);
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text-secondary);
  z-index: 200;
  white-space: normal;
}

/* ── Activity Log Category Colors ── */
.logCat--config {
  background: rgba(99,102,241,0.15) !important;
  color: #818cf8 !important;
}
.logCat--ohi {
  background: rgba(34,197,94,0.12) !important;
  color: #4ade80 !important;
}
.logCat--spread {
  background: rgba(14,165,233,0.12) !important;
  color: #38bdf8 !important;
}

/* ── OHI Sub-Scores ── */
.ohiLabelClickable {
  cursor: pointer;
  user-select: none;
}
.ohiLabelClickable:hover { opacity: 0.8; }

.ohiSubScores {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding-top: var(--space-2);
  border-top: 1px solid var(--color-stroke-divider);
  margin-top: var(--space-1);
}

.ohiSubRow {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

/* ── Depth Spread Widget ── */
.depthWidget {
  width: 100%;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.depthWidgetWarn {
  border-color: rgba(245,158,11,0.4);
  background: rgba(245,158,11,0.05);
}

.depthHeader {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.depthGrid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 2px 12px;
}

.depthRow {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

/* ── V4 Details ── */
.v4Details {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 12px;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
}

.v4Row {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

/* ===== MOBILE RESPONSIVE BREAKPOINTS ===== */

@media (max-width: 1024px) {
  .page {
    padding: 20px 20px 40px;
  }

  .mainGrid {
    grid-template-columns: 1fr 1fr;
    gap: var(--space-4);
  }

  .centerPanel {
    grid-column: 1 / -1;
    order: -1;
    flex-direction: row;
    flex-wrap: wrap;
    justify-content: center;
    padding: var(--space-3);
  }

  .ringWrap {
    width: 120px;
    height: 120px;
  }

  .ringValue {
    font-size: 18px;
  }

  .flagGrid {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 767px) {
  .page {
    padding: 16px 16px 80px;
    gap: var(--space-4);
  }

  .topBar {
    flex-direction: column;
    gap: var(--space-3);
    padding: var(--space-3);
  }

  .pills {
    flex-wrap: wrap;
    justify-content: center;
    width: 100%;
  }

  .pill {
    padding: 6px 12px;
    font-size: 12px;
  }

  .controls {
    width: 100%;
    justify-content: center;
  }

  .mainGrid {
    grid-template-columns: 1fr;
    gap: var(--space-3);
  }

  .centerPanel {
    order: 0;
    min-width: auto;
  }

  .dexCard {
    padding: var(--space-4);
  }

  .dexHeader {
    flex-wrap: wrap;
  }

  .dexBalance {
    margin-left: 0;
    width: 100%;
    margin-top: var(--space-2);
    text-align: center;
  }

  .flagGrid {
    grid-template-columns: 1fr;
  }

  .logEntry {
    flex-wrap: wrap;
    gap: var(--space-2);
  }

  .logTime {
    width: auto;
  }

  .logCat {
    width: auto;
  }

  .popover,
  .timerPopover {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    width: 90%;
    max-width: 300px;
    z-index: 1000;
  }

  .tooltipBox {
    position: fixed;
    top: auto;
    bottom: 100px;
    left: 16px;
    right: 16px;
    width: auto;
    z-index: 1000;
  }
}

@media (max-width: 480px) {
  .ringWrap {
    width: 100px;
    height: 100px;
  }

  .ringValue {
    font-size: 16px;
  }

  .pnlRow {
    flex-direction: column;
    gap: var(--space-2);
  }

  .pnlItem {
    flex-direction: row;
    justify-content: space-between;
    width: 100%;
  }

  .dexStats {
    gap: var(--space-2);
  }

  .dexStat {
    font-size: 13px;
  }

  .posRow {
    padding: 4px 0;
  }

  .v4Details,
  .depthGrid {
    grid-template-columns: 1fr;
  }
}
</style>
