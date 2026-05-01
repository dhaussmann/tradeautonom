<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { fetchCommonTokensWithData, fetchAnalysis, type AnalysisExchange, type TokenTableRow } from '@/lib/defi-api'
import { fetchMarkets } from '@/lib/api'
import Typography from '@/components/ui/Typography.vue'
import Button from '@/components/ui/Button.vue'
import type { BotCreateRequest, SymbolMeta } from '@/types/bot'
import { useBotsStore } from '@/stores/bots'
import { fetchSymbolMeta } from '@/lib/oms-meta'
import {
  fetchTrackedWithTradeability,
  isTokenTradeableOn,
  type AnnotatedTrackedResponse,
} from '@/lib/oms-tradeability'
import { validateBotCreate, type ValidationResult } from '@/lib/bot-validation'

// RISEx (rise.trade) — fully on-chain perp DEX on RISE Chain.
// All RISEx markets are currently `post_only:true`, so the bot can only use
// RISEx as the maker leg. The maker-enforcement logic below pins
// `makerExchange` to 'risex' whenever 'risex' is selected as long or short.
const EXCHANGES = ['extended', 'variational', 'grvt', 'nado', 'risex'] as const
type ExKey = typeof EXCHANGES[number]

const MA_PERIODS = [
  { key: 'live', label: 'Live' },
  { key: '1d', label: '24h' },
  { key: '3d', label: '3d' },
  { key: '7d', label: '7d' },
  { key: '14d', label: '14d' },
  { key: '30d', label: '30d' },
] as const

const props = defineProps<{
  open: boolean
  prefill?: { token: string; longExchange: string; shortExchange: string }
}>()
const emit = defineEmits<{ close: []; create: [req: BotCreateRequest] }>()

const botsStore = useBotsStore()

// ── Wizard state ─────────────────────────────────────
const step = ref(1)
const submitting = ref(false)
const _prefilling = ref(false)
const error = ref<string | null>(null)

// Step 1: DEX pair + Token
const selectedExchanges = ref<ExKey[]>([])
const tokenRows = ref<TokenTableRow[]>([])
const tokensLoading = ref(false)
const tokenSearch = ref('')
const selectedToken = ref('')
// Maps asset (e.g. "MSTR") → actual instrument name (e.g. "MSTR_24_5-USD") per exchange
const instrumentMaps = ref<Record<string, Map<string, string>>>({})
const sortKey = ref<'symbol' | 'volume' | 'apr'>('volume')
const sortAsc = ref(false)
// OMS V2 tradeability map: hides tokens whose chosen exchange has a one-sided
// or stale order book (e.g. Nado/ARB-PERP — listed but unfillable).
// Null = not yet fetched / unavailable → fail-open (no filtering applied).
const tradeabilityLegs = ref<AnnotatedTrackedResponse['legs'] | null>(null)
const hiddenByTradeabilityCount = ref(0)

// Step 2: Direction
const analysisData = ref<AnalysisExchange[]>([])
const analysisLoading = ref(false)
const maTimeframe = ref<string>('live')
const longExchange = ref<ExKey>('extended')
const shortExchange = ref<ExKey>('grvt')
const directionManual = ref(false)

// Step 3: Quantity & Leverage
const quantity = ref(1)
const leverage = ref(5)

// Step 4: Execution
const twapChunks = ref(10)
const twapInterval = ref(10)
const minSpreadPct = ref(-0.5)
const maxSpreadPct = ref(0.5)
// Exit-Spread thresholds — separate from entry. Defaults are conservative
// (strict per Variante 2). User adjusts in BotDetailView later if needed.
const exitMinSpreadPct = ref(-0.5)
const exitMaxSpreadPct = ref(0.05)
const makerExchange = ref<ExKey>('extended')
const simulation = ref(false)

// Smart defaults: track whether user has manually edited spread fields.
// Once true, auto-update on maker change is suppressed.
const userTouchedSpread = ref(false)

// Meta & validation state
const makerMeta = ref<SymbolMeta | null>(null)
const takerMeta = ref<SymbolMeta | null>(null)
const metaLoading = ref(false)
const validationResult = ref<ValidationResult>({ ok: true })

// ── Helpers ──────────────────────────────────────────
// Empty string means "no SVG asset; use displayExchange() text badge instead".
// We keep RISEx as text for now since rise.trade brand assets are not yet
// available locally — the template falls back to a styled <span> below.
const DEX_LOGOS: Record<string, string> = {
  extended: '/extended-logo.svg',
  variational: '/variational-logo.svg',
  grvt: '/grvt-logo.svg',
  nado: '/nado-logo.svg',
  risex: '',
}

function displayExchange(ex: string): string {
  if (ex === 'grvt') return 'GRVT'
  if (ex === 'nado') return 'Nado'
  if (ex === 'risex') return 'RISEx'
  return ex.charAt(0).toUpperCase() + ex.slice(1)
}

function instrumentForExchange(token: string, exchange: string): string {
  // Prefer real instrument name from backend (handles Extended suffixes like _24_5)
  const map = instrumentMaps.value[exchange]
  if (map?.has(token)) return map.get(token)!
  // Fallback to predictable naming patterns
  if (exchange === 'extended') return `${token}-USD`
  if (exchange === 'grvt') return `${token}_USDT_Perp`
  if (exchange === 'variational') return `P-${token}-USDC-3600`
  if (exchange === 'nado') return `${token}-PERP`
  if (exchange === 'risex') return `${token}/USDC`
  return token
}

function getRateForExchange(exchange: string, period: string): number | null {
  const ex = analysisData.value.find(e => e.exchange === exchange)
  if (!ex) return null
  if (period === 'live') return ex.funding_rate_apr
  const ma = ex.ma?.[period]
  return ma ? ma.ma_apr : null
}

function formatApr(val: number | null): string {
  if (val === null) return '—'
  return `${(val * 100).toFixed(2)}%`
}

function aprColor(val: number | null): string {
  if (val === null) return 'var(--color-text-tertiary)'
  if (val * 100 > 5) return '#22c55e'
  if (val * 100 < -5) return '#ef4444'
  return 'var(--color-text-primary)'
}

function livePrice(): number | null {
  for (const ex of analysisData.value) {
    if (ex.market_price) return ex.market_price
  }
  return null
}

// ── Computed ─────────────────────────────────────────
const filteredRows = computed(() => {
  let rows = tokenRows.value
  const q = tokenSearch.value.trim().toUpperCase()
  if (q) rows = rows.filter(r => r.symbol.includes(q))
  const dir = sortAsc.value ? 1 : -1
  return [...rows].sort((a, b) => {
    if (sortKey.value === 'symbol') return dir * a.symbol.localeCompare(b.symbol)
    if (sortKey.value === 'volume') return dir * (a.volume24h - b.volume24h)
    return dir * (a.aprSpread - b.aprSpread)
  })
})

function toggleSort(key: 'symbol' | 'volume' | 'apr') {
  if (sortKey.value === key) sortAsc.value = !sortAsc.value
  else { sortKey.value = key; sortAsc.value = key === 'symbol' }
}

function fmtVol(v: number): string {
  if (v >= 1e9) return `$${(v / 1e9).toFixed(1)}B`
  if (v >= 1e6) return `$${(v / 1e6).toFixed(1)}M`
  if (v >= 1e3) return `$${(v / 1e3).toFixed(1)}K`
  return `$${v.toFixed(0)}`
}

function suggestRole(row: TokenTableRow, exchange: string): 'LONG' | 'SHORT' | null {
  const exs = selectedExchanges.value
  if (exs.length !== 2) return null
  const a = row.perExchange[exs[0]]?.apr ?? 0
  const b = row.perExchange[exs[1]]?.apr ?? 0
  if (a === b) return null
  const highEx = a > b ? exs[0] : exs[1]
  return exchange === highEx ? 'SHORT' : 'LONG'
}

const suggestedDirection = computed(() => {
  const period = maTimeframe.value
  const rates: { exchange: ExKey; rate: number }[] = []
  for (const ex of selectedExchanges.value) {
    const r = getRateForExchange(ex, period)
    if (r !== null) rates.push({ exchange: ex, rate: r })
  }
  if (rates.length < 2) return null
  rates.sort((a, b) => b.rate - a.rate)
  // Highest funding → short (you collect), lowest → long (you pay less)
  return { shortEx: rates[0].exchange, longEx: rates[rates.length - 1].exchange }
})

const currentSpread = computed(() => {
  const longRate = getRateForExchange(longExchange.value, maTimeframe.value)
  const shortRate = getRateForExchange(shortExchange.value, maTimeframe.value)
  if (longRate === null || shortRate === null) return null
  return shortRate - longRate
})

const priceSpreadPct = computed(() => {
  const longEx = analysisData.value.find(e => e.exchange === longExchange.value)
  const shortEx = analysisData.value.find(e => e.exchange === shortExchange.value)
  if (!longEx?.market_price || !shortEx?.market_price || shortEx.market_price <= 0) return null
  return (longEx.market_price - shortEx.market_price) / shortEx.market_price * 100
})

const positionUsd = computed(() => {
  const p = livePrice()
  if (!p) return null
  return quantity.value * p
})

const botId = computed(() => {
  if (!selectedToken.value) return ''
  const base = selectedToken.value
  const existing = botsStore.bots.map(b => b.bot_id)
  if (!existing.includes(base)) return base
  let i = 2
  while (existing.includes(`${base}-${i}`)) i++
  return `${base}-${i}`
})

// ── Watchers ─────────────────────────────────────────
watch(() => props.open, async (v) => {
  if (v) {
    error.value = null
    analysisData.value = []
    maTimeframe.value = 'live'
    directionManual.value = false
    quantity.value = 1
    leverage.value = 5
    twapChunks.value = 10
    twapInterval.value = 10
    minSpreadPct.value = -0.5
    maxSpreadPct.value = 0.5
    exitMinSpreadPct.value = -0.5
    exitMaxSpreadPct.value = 0.05
    makerExchange.value = 'extended'
    simulation.value = false
    userTouchedSpread.value = false

    if (props.prefill) {
      // Pre-fill from Strategies page — skip Step 1, go to Step 2
      _prefilling.value = true
      const pf = props.prefill
      step.value = 2
      selectedExchanges.value = [pf.longExchange, pf.shortExchange] as ExKey[]
      selectedToken.value = pf.token
      longExchange.value = pf.longExchange as ExKey
      shortExchange.value = pf.shortExchange as ExKey
      directionManual.value = true
      tokenSearch.value = ''
      tokenRows.value = []
      // Load instrument maps for the two exchanges
      try {
        const marketResults = await Promise.all(
          selectedExchanges.value.map(ex => fetchMarkets(ex).catch(() => []))
        )
        const maps: Record<string, Map<string, string>> = {}
        selectedExchanges.value.forEach((ex, i) => {
          const m = new Map<string, string>()
          for (const mkt of marketResults[i]) {
            if (mkt.asset) {
              const key = mkt.asset.toUpperCase()
              m.set(key, mkt.symbol)
              // Also map base token without version suffix (e.g. HOOD_24_5 → HOOD)
              const base = key.replace(/_\d+_\d+$/, '')
              if (base !== key && !m.has(base)) m.set(base, mkt.symbol)
            }
          }
          maps[ex] = m
        })
        instrumentMaps.value = maps
      } catch { /* ignore */ }
      _prefilling.value = false
    } else {
      step.value = 1
      selectedToken.value = ''
      tokenSearch.value = ''
      selectedExchanges.value = []
      tokenRows.value = []
    }
  }
})

async function loadCommonTokens() {
  tokensLoading.value = true
  tokenRows.value = []
  selectedToken.value = ''
  hiddenByTradeabilityCount.value = 0
  try {
    // Fetch token list, real instrument names, and OMS V2 tradeability in
    // parallel. The tradeability fetch is fail-open (returns null on error),
    // so a transient OMS hiccup does not block the bot-create flow.
    const trackedPromise = fetchTrackedWithTradeability().catch(() => null)
    const [tokens, ...marketResults] = await Promise.all([
      fetchCommonTokensWithData(selectedExchanges.value),
      ...selectedExchanges.value.map(ex => fetchMarkets(ex).catch(() => []))
    ])
    const tracked = await trackedPromise
    tradeabilityLegs.value = tracked?.legs ?? null

    // Filter out tokens whose either selected exchange is currently
    // non-tradeable (one-sided book, stale, or not in OMS legs map).
    // Fail-open: when tradeabilityLegs is null we keep the full list.
    const exchanges = selectedExchanges.value as readonly string[]
    let filtered: TokenTableRow[]
    if (tradeabilityLegs.value) {
      filtered = tokens.filter(row =>
        isTokenTradeableOn(tradeabilityLegs.value!, row.symbol, exchanges),
      )
      hiddenByTradeabilityCount.value = tokens.length - filtered.length
    } else {
      filtered = tokens
      hiddenByTradeabilityCount.value = 0
    }
    tokenRows.value = filtered

    // Build asset → instrument maps per exchange
    const maps: Record<string, Map<string, string>> = {}
    selectedExchanges.value.forEach((ex, i) => {
      const m = new Map<string, string>()
      for (const mkt of marketResults[i]) {
        if (mkt.asset) {
          const key = mkt.asset.toUpperCase()
          m.set(key, mkt.symbol)
          // Also map base token without version suffix (e.g. HOOD_24_5 → HOOD)
          const base = key.replace(/_\d+_\d+$/, '')
          if (base !== key && !m.has(base)) m.set(base, mkt.symbol)
        }
      }
      maps[ex] = m
    })
    instrumentMaps.value = maps
  } catch {
    tokenRows.value = []
  } finally {
    tokensLoading.value = false
  }
}

// Reload tokens when exactly 2 exchanges are selected
watch(selectedExchanges, (exs) => {
  if (_prefilling.value) return // prefill already handled
  if (exs.length === 2) loadCommonTokens()
  else { tokenRows.value = []; selectedToken.value = '' }
}, { deep: true })

// Load analysis when token is selected
watch(selectedToken, async (token) => {
  if (!token) { analysisData.value = []; return }
  analysisLoading.value = true
  try {
    const result = await fetchAnalysis(token)
    analysisData.value = result.exchanges.filter(e =>
      (EXCHANGES as readonly string[]).includes(e.exchange),
    )
  } catch {
    analysisData.value = []
  } finally {
    analysisLoading.value = false
  }
})

// Auto-apply suggested direction when timeframe or analysis changes
watch([suggestedDirection, maTimeframe], () => {
  if (directionManual.value) return
  const s = suggestedDirection.value
  if (s) {
    longExchange.value = s.longEx
    shortExchange.value = s.shortEx
  }
})

function toggleExchange(ex: ExKey) {
  const idx = selectedExchanges.value.indexOf(ex)
  if (idx >= 0) {
    selectedExchanges.value = selectedExchanges.value.filter(e => e !== ex)
  } else if (selectedExchanges.value.length < 2) {
    selectedExchanges.value = [...selectedExchanges.value, ex]
  }
}

// ── Validation ───────────────────────────────────────
const validation = computed((): ValidationResult => {
  if (step.value !== 4) return { ok: true }
  const price = livePrice()
  if (!price || price <= 0) return { ok: true } // Can't validate without price

  return validateBotCreate({
    makerExchange: makerExchange.value,
    longExchange: longExchange.value,
    shortExchange: shortExchange.value,
    longSymbol: instrumentForExchange(selectedToken.value, longExchange.value),
    shortSymbol: instrumentForExchange(selectedToken.value, shortExchange.value),
    quantity: quantity.value,
    numChunks: twapChunks.value,
    livePrice: price,
    makerMeta: makerMeta.value,
    takerMeta: takerMeta.value,
  })
})

// Update validationResult when validation changes (for template reactivity)
watch(validation, (v) => {
  validationResult.value = v
}, { immediate: true })

// ── Maker-Asymmetry Hint (Item 2) ────────────────────
// The cross-venue spread is defined as (long_price - short_price) / short_price.
// Whichever leg the user picks as maker determines which side of the window
// matters most for getting filled.
const makerSpreadHint = computed((): string => {
  if (makerExchange.value === longExchange.value) {
    return 'Maker = Long: expects long-ask to fall. Max-spread is the active lever — recommended ≥ +0.30 %.'
  }
  if (makerExchange.value === shortExchange.value) {
    return 'Maker = Short: expects short-bid to rise. Min-spread is the active lever — recommended ≤ −0.30 %.'
  }
  return 'Maker exchange is not one of the trading legs — please review configuration.'
})

// ── Smart Defaults for spread window (Item 3) ────────
// Aggressive asymmetry: ±0.10% / ±0.40% applied to BOTH entry and exit.
// Only auto-applied while userTouchedSpread === false. Once the user manually
// edits any of the four spread fields, auto-update is suppressed.
const SPREAD_DEFAULTS = {
  longMaker:  { entryMin: -0.10, entryMax:  0.40, exitMin: -0.10, exitMax:  0.40 },
  shortMaker: { entryMin: -0.40, entryMax:  0.10, exitMin: -0.40, exitMax:  0.10 },
  fallback:   { entryMin: -0.50, entryMax:  0.50, exitMin: -0.50, exitMax:  0.05 },
}

function applySmartDefaults() {
  if (userTouchedSpread.value) return
  let preset
  if (makerExchange.value === longExchange.value) preset = SPREAD_DEFAULTS.longMaker
  else if (makerExchange.value === shortExchange.value) preset = SPREAD_DEFAULTS.shortMaker
  else preset = SPREAD_DEFAULTS.fallback
  minSpreadPct.value = preset.entryMin
  maxSpreadPct.value = preset.entryMax
  exitMinSpreadPct.value = preset.exitMin
  exitMaxSpreadPct.value = preset.exitMax
}

// Re-apply defaults when maker / long / short selection changes.
watch([makerExchange, longExchange, shortExchange], () => {
  applySmartDefaults()
})

// ── post_only Enforcement for RISEx ──────────────────
// All RISEx markets are currently `post_only:true` (verified live against
// api.rise.trade/v1/markets). A RISEx leg can therefore only be the maker —
// it will reject any IOC/taker order. Whenever RISEx is selected as long or
// short we pin makerExchange to 'risex' and disable the other Maker options.
const risexLegPresent = computed(
  () => longExchange.value === 'risex' || shortExchange.value === 'risex',
)
watch(
  risexLegPresent,
  (active) => {
    if (active && makerExchange.value !== 'risex') {
      makerExchange.value = 'risex'
    }
    // No automatic revert when RISEx is dropped — user keeps last choice.
  },
  { immediate: true },
)
function isMakerOptionDisabled(ex: ExKey): boolean {
  // When a RISEx leg is in play, only RISEx can be the maker.
  if (risexLegPresent.value && ex !== 'risex') return true
  // RISEx as maker only makes sense when RISEx is actually one of the legs.
  if (!risexLegPresent.value && ex === 'risex') return true
  return false
}

function markSpreadDirty() {
  userTouchedSpread.value = true
}

// ── Meta fetching ────────────────────────────────────
async function loadMetaForCurrentPair() {
  if (!selectedToken.value || step.value < 4) return

  metaLoading.value = true
  try {
    const longSym = instrumentForExchange(selectedToken.value, longExchange.value)
    const shortSym = instrumentForExchange(selectedToken.value, shortExchange.value)

    // Determine which exchange is maker vs taker
    const takerEx = makerExchange.value === longExchange.value ? shortExchange.value : longExchange.value
    const takerSym = makerExchange.value === longExchange.value ? shortSym : longSym

    // Fetch both in parallel
    const [maker, taker] = await Promise.all([
      fetchSymbolMeta(makerExchange.value, makerExchange.value === longExchange.value ? longSym : shortSym),
      fetchSymbolMeta(takerEx, takerSym),
    ])

    makerMeta.value = maker
    takerMeta.value = taker
  } catch (e) {
    console.error('Failed to load meta:', e)
  } finally {
    metaLoading.value = false
  }
}

// Fetch meta when entering step 4 or when maker/token changes
watch([step, makerExchange, selectedToken, longExchange, shortExchange], () => {
  if (step.value === 4) {
    loadMetaForCurrentPair()
  }
}, { immediate: true })

// ── Navigation ───────────────────────────────────────
function canNext(): boolean {
  if (step.value === 1) return selectedExchanges.value.length === 2 && !!selectedToken.value
  if (step.value === 2) return analysisData.value.length >= 2
  if (step.value === 3) return quantity.value > 0
  if (step.value === 4) {
    if (twapChunks.value <= 0 || twapInterval.value <= 0) return false
    // Block if validation fails (but allow if we can't validate yet)
    if (!validation.value.ok) return false
    return true
  }
  return true
}

function next() {
  if (canNext() && step.value < 5) step.value++
}

function prev() {
  if (step.value > 1) step.value--
}

function swapDirection() {
  directionManual.value = true
  const tmp = longExchange.value
  longExchange.value = shortExchange.value
  shortExchange.value = tmp
}

function selectToken(token: string) {
  selectedToken.value = token
  directionManual.value = false
}

async function submit() {
  submitting.value = true
  error.value = null
  try {
    const req: BotCreateRequest = {
      bot_id: botId.value,
      long_exchange: longExchange.value,
      short_exchange: shortExchange.value,
      instrument_a: instrumentForExchange(selectedToken.value, longExchange.value),
      instrument_b: instrumentForExchange(selectedToken.value, shortExchange.value),
      quantity: quantity.value,
      twap_num_chunks: twapChunks.value,
      twap_interval_s: twapInterval.value,
      maker_exchange: makerExchange.value,
      simulation: simulation.value,
      leverage_long: leverage.value,
      leverage_short: leverage.value,
      min_spread_pct: minSpreadPct.value,
      max_spread_pct: maxSpreadPct.value,
      exit_min_spread_pct: exitMinSpreadPct.value,
      exit_max_spread_pct: exitMaxSpreadPct.value,
    }
    emit('create', req)
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to create bot'
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <Teleport to="body">
    <div v-if="open" :class="$style.overlay" @click.self="emit('close')">
      <div :class="$style.modal">
        <!-- Header -->
        <div :class="$style.header">
          <Typography size="text-h6" weight="semibold">Create Bot</Typography>
          <button :class="$style.closeBtn" @click="emit('close')">✕</button>
        </div>

        <!-- Stepper -->
        <div :class="$style.stepper">
          <div v-for="s in 5" :key="s" :class="[$style.stepDot, s === step && $style.stepActive, s < step && $style.stepDone]">
            <span>{{ s }}</span>
          </div>
        </div>

        <div :class="$style.body">
          <!-- ── Step 1: Exchanges + Token ── -->
          <template v-if="step === 1">
            <Typography size="text-md" weight="semibold">Select Exchanges</Typography>
            <Typography size="text-xs" color="tertiary">Pick 2 DEXs for the funding arbitrage</Typography>
            <div :class="$style.dexRow">
              <button
                v-for="ex in EXCHANGES"
                :key="ex"
                :class="[
                  $style.dexBtn,
                  selectedExchanges.includes(ex) && $style.dexSelected,
                  !selectedExchanges.includes(ex) && selectedExchanges.length === 2 && $style.dexDimmed,
                ]"
                @click="toggleExchange(ex)"
              >
                <img v-if="DEX_LOGOS[ex]" :src="DEX_LOGOS[ex]" :alt="displayExchange(ex)" :class="$style.dexLogo" />
                <span v-else :class="$style.dexBadge">{{ displayExchange(ex) }}</span>
              </button>
            </div>

            <template v-if="selectedExchanges.length === 2">
              <input
                v-model="tokenSearch"
                :class="$style.input"
                type="text"
                placeholder="Search token..."
                spellcheck="false"
                style="margin-top: 4px"
              />
              <div v-if="tokensLoading" :class="$style.loadingText">
                <Typography size="text-sm" color="secondary">Loading tokens...</Typography>
              </div>
              <div v-else-if="hiddenByTradeabilityCount > 0" :class="$style.loadingText">
                <Typography size="text-xs" color="tertiary">
                  {{ hiddenByTradeabilityCount }} token(s) hidden — order book unavailable on selected exchanges
                </Typography>
              </div>
              <div v-if="!tokensLoading" :class="$style.tokenTable">
                <table :class="$style.ttable">
                  <thead>
                    <tr>
                      <th :class="$style.thSort" @click="toggleSort('symbol')">Market {{ sortKey === 'symbol' ? (sortAsc ? '▲' : '▼') : '' }}</th>
                      <th :class="[$style.thSort, $style.thRight]" @click="toggleSort('volume')">Volume {{ sortKey === 'volume' ? (sortAsc ? '▲' : '▼') : '' }}</th>
                      <th :class="[$style.thSort, $style.thRight]" @click="toggleSort('apr')" title="Funding-arb spread: max(APR) − min(APR) across the two selected exchanges">Spread {{ sortKey === 'apr' ? (sortAsc ? '▲' : '▼') : '' }}</th>
                      <th v-for="ex in selectedExchanges" :key="ex" :class="$style.thExchange">
                        <img v-if="DEX_LOGOS[ex]" :src="DEX_LOGOS[ex]" :alt="displayExchange(ex)" style="height: 16px" />
                        <span v-else :class="$style.dexBadgeSmall">{{ displayExchange(ex) }}</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr
                      v-for="row in filteredRows"
                      :key="row.symbol"
                      :class="[$style.trow, selectedToken === row.symbol && $style.trowSelected]"
                      @click="selectToken(row.symbol)"
                    >
                      <td :class="$style.tcell"><strong>{{ row.symbol }}</strong></td>
                      <td :class="[$style.tcell, $style.tcRight]">{{ fmtVol(row.volume24h) }}</td>
                      <td :class="[$style.tcell, $style.tcRight]" :style="{ color: row.aprSpread > 0 ? '#22c55e' : 'var(--color-text-tertiary)' }">
                        {{ (row.aprSpread * 100).toFixed(2) }}%
                      </td>
                      <td v-for="ex in selectedExchanges" :key="ex" :class="[$style.tcell, $style.tcExchange]">
                        <span :style="{ color: (row.perExchange[ex]?.apr ?? 0) >= 0 ? '#22c55e' : '#ef4444' }">
                          {{ row.perExchange[ex] ? (row.perExchange[ex].apr * 100).toFixed(2) + '%' : '—' }}
                        </span>
                        <span v-if="suggestRole(row, ex) === 'SHORT'" :class="[$style.roleBadge, $style.roleShort]">SHORT</span>
                        <span v-else-if="suggestRole(row, ex) === 'LONG'" :class="[$style.roleBadge, $style.roleLong]">LONG</span>
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>
              <div v-if="selectedToken" :class="$style.selectedInfo">
                <Typography size="text-sm" color="secondary">
                  Selected: <strong>{{ selectedToken }}</strong> — Bot ID: <strong>{{ botId }}</strong>
                </Typography>
              </div>
            </template>
          </template>

          <!-- ── Step 2: Direction ── -->
          <template v-if="step === 2">
            <Typography size="text-md" weight="semibold">Direction — {{ selectedToken }}</Typography>
            <Typography size="text-xs" color="tertiary">Funding rates determine long/short assignment</Typography>

            <!-- Timeframe selector -->
            <div :class="$style.tfRow">
              <button
                v-for="tf in MA_PERIODS"
                :key="tf.key"
                :class="[$style.tfBtn, maTimeframe === tf.key && $style.tfActive]"
                @click="maTimeframe = tf.key"
              >{{ tf.label }}</button>
            </div>

            <div v-if="analysisLoading" :class="$style.loadingText">
              <Typography size="text-sm" color="secondary">Loading funding rates...</Typography>
            </div>
            <template v-else-if="analysisData.length">
              <!-- Funding table -->
              <div :class="$style.fundingTable">
                <div :class="[$style.ftRow, $style.ftHeader]">
                  <div :class="$style.ftCell"><Typography size="text-xs" color="tertiary">Exchange</Typography></div>
                  <div :class="[$style.ftCell, $style.ftRight]"><Typography size="text-xs" color="tertiary">Funding APR</Typography></div>
                  <div :class="[$style.ftCell, $style.ftRight]"><Typography size="text-xs" color="tertiary">Price</Typography></div>
                  <div :class="[$style.ftCell, $style.ftCenter]"><Typography size="text-xs" color="tertiary">Role</Typography></div>
                </div>
                <div
                  v-for="ex in selectedExchanges"
                  :key="ex"
                  :class="$style.ftRow"
                >
                  <div :class="$style.ftCell">
                    <Typography size="text-sm" weight="medium">{{ displayExchange(ex) }}</Typography>
                  </div>
                  <div :class="[$style.ftCell, $style.ftRight]">
                    <Typography size="text-sm" :style="{ color: aprColor(getRateForExchange(ex, maTimeframe)) }">
                      {{ formatApr(getRateForExchange(ex, maTimeframe)) }}
                    </Typography>
                  </div>
                  <div :class="[$style.ftCell, $style.ftRight]">
                    <Typography size="text-sm">
                      {{ analysisData.find(a => a.exchange === ex)?.market_price
                        ? '$' + analysisData.find(a => a.exchange === ex)!.market_price!.toLocaleString(undefined, { maximumFractionDigits: 2 })
                        : '—' }}
                    </Typography>
                  </div>
                  <div :class="[$style.ftCell, $style.ftCenter]">
                    <span v-if="ex === longExchange" :class="[$style.roleBadge, $style.roleLong]">LONG</span>
                    <span v-else-if="ex === shortExchange" :class="[$style.roleBadge, $style.roleShort]">SHORT</span>
                    <span v-else :class="$style.roleBadge">—</span>
                  </div>
                </div>
              </div>

              <!-- Spread info -->
              <div :class="$style.spreadCard">
                <div :class="$style.spreadRow">
                  <Typography size="text-xs" color="tertiary">Spread (Short − Long)</Typography>
                  <Typography size="text-sm" weight="semibold" :style="{ color: currentSpread !== null && currentSpread >= 0 ? '#22c55e' : '#ef4444' }">
                    {{ currentSpread !== null ? (currentSpread >= 0 ? '+' : '') + (currentSpread * 100).toFixed(2) + '%' : '—' }}
                  </Typography>
                </div>
              </div>

              <Button variant="outline" size="sm" @click="swapDirection">Swap Long / Short</Button>
            </template>
          </template>

          <!-- ── Step 3: Quantity & Leverage ── -->
          <template v-if="step === 3">
            <Typography size="text-md" weight="semibold">Quantity &amp; Leverage</Typography>
            <div :class="$style.row">
              <div :class="$style.field" style="flex: 2">
                <label :class="$style.label">Quantity ({{ selectedToken }})</label>
                <input v-model.number="quantity" :class="$style.input" type="number" step="0.1" min="0" />
              </div>
              <div :class="$style.field" style="flex: 1">
                <label :class="$style.label">Leverage</label>
                <input v-model.number="leverage" :class="$style.input" type="number" step="1" min="1" max="100" />
              </div>
            </div>
            <div v-if="livePrice()" :class="$style.priceInfo">
              <div :class="$style.priceRow">
                <Typography size="text-xs" color="tertiary">Token Price</Typography>
                <Typography size="text-sm">${{ livePrice()!.toLocaleString(undefined, { maximumFractionDigits: 2 }) }}</Typography>
              </div>
              <div :class="$style.priceRow">
                <Typography size="text-xs" color="tertiary">Position Size</Typography>
                <Typography size="text-sm" weight="semibold">${{ positionUsd !== null ? positionUsd.toLocaleString(undefined, { maximumFractionDigits: 2 }) : '—' }}</Typography>
              </div>
              <div :class="$style.priceRow">
                <Typography size="text-xs" color="tertiary">Margin Required (~)</Typography>
                <Typography size="text-sm">${{ positionUsd !== null ? (positionUsd / leverage).toLocaleString(undefined, { maximumFractionDigits: 2 }) : '—' }}</Typography>
              </div>
            </div>
          </template>

          <!-- ── Step 4: Execution ── -->
          <template v-if="step === 4">
            <Typography size="text-md" weight="semibold">Execution Settings</Typography>
            <div :class="$style.row">
              <div :class="$style.field">
                <label :class="$style.label">Chunk Count</label>
                <input v-model.number="twapChunks" :class="$style.input" type="number" step="1" min="1" />
              </div>
              <div :class="$style.field">
                <label :class="$style.label">Chunk Interval (s)</label>
                <input v-model.number="twapInterval" :class="$style.input" type="number" step="1" min="1" />
              </div>
            </div>
            <Typography size="text-xs" color="tertiary">Entry Spread Window</Typography>
            <div :class="$style.row">
              <div :class="$style.field">
                <label :class="$style.label">Entry Min Spread % (≥, safety floor)</label>
                <div :class="$style.inputWithHint">
                  <input v-model.number="minSpreadPct" @input="markSpreadDirty" :class="$style.input" type="number" step="0.1" />
                  <Typography v-if="priceSpreadPct !== null" size="text-xs" :style="{ color: priceSpreadPct >= minSpreadPct ? '#22c55e' : '#ef4444' }">
                    Current: {{ priceSpreadPct.toFixed(4) }}%
                  </Typography>
                </div>
              </div>
              <div :class="$style.field">
                <label :class="$style.label">Entry Max Spread % (≤, cost cap)</label>
                <div :class="$style.inputWithHint">
                  <input v-model.number="maxSpreadPct" @input="markSpreadDirty" :class="$style.input" type="number" step="0.01" />
                  <Typography v-if="priceSpreadPct !== null" size="text-xs" :style="{ color: priceSpreadPct <= maxSpreadPct ? '#22c55e' : '#ef4444' }">
                    Current: {{ priceSpreadPct.toFixed(4) }}%
                  </Typography>
                </div>
              </div>
            </div>
            <Typography size="text-xs" color="tertiary">
              Exit Spread Window (separate — applies during Stop / graceful_stop)
            </Typography>
            <div :class="$style.row">
              <div :class="$style.field">
                <label :class="$style.label">Exit Min Spread % (≥, safety floor)</label>
                <input v-model.number="exitMinSpreadPct" @input="markSpreadDirty" :class="$style.input" type="number" step="0.1" />
              </div>
              <div :class="$style.field">
                <label :class="$style.label">Exit Max Spread % (≤, cost cap)</label>
                <input v-model.number="exitMaxSpreadPct" @input="markSpreadDirty" :class="$style.input" type="number" step="0.01" />
              </div>
            </div>
            <!-- Maker-Asymmetry Hint (Item 2) -->
            <div :class="$style.makerHint">
              <Typography size="text-xs" color="secondary">{{ makerSpreadHint }}</Typography>
            </div>
            <div :class="$style.row">
              <div :class="$style.field">
                <label :class="$style.label">Maker Exchange</label>
                <select v-model="makerExchange" :class="$style.input">
                  <option
                    v-for="ex in EXCHANGES"
                    :key="ex"
                    :value="ex"
                    :disabled="isMakerOptionDisabled(ex)"
                  >{{ displayExchange(ex) }}{{ isMakerOptionDisabled(ex) && ex === 'risex' ? ' (only when RISEx is a leg)' : '' }}{{ isMakerOptionDisabled(ex) && ex !== 'risex' && risexLegPresent ? ' (post-only — RISEx must be maker)' : '' }}</option>
                </select>
              </div>
              <div :class="$style.field">
                <label :class="$style.label">Simulation</label>
                <div :class="$style.toggle">
                  <input type="checkbox" v-model="simulation" />
                  <Typography size="text-sm" color="secondary">{{ simulation ? 'Yes' : 'No' }}</Typography>
                </div>
              </div>
            </div>

            <!-- Validation Messages -->
            <div v-if="metaLoading" :class="$style.validationInfo">
              <Typography size="text-xs" color="secondary">Loading exchange constraints...</Typography>
            </div>
            <div v-else-if="!validationResult.ok" :class="$style.validationError">
              <Typography size="text-sm" color="error" weight="medium">{{ validationResult.message }}</Typography>
            </div>
            <div v-else-if="validationResult.warnings?.length" :class="$style.validationWarning">
              <Typography v-for="(warning, idx) in validationResult.warnings" :key="idx" size="text-xs" color="secondary">
                ⚠️ {{ warning }}
              </Typography>
            </div>

            <!-- Size Summary -->
            <div v-if="positionUsd && positionUsd > 0" :class="$style.sizeSummary">
              <Typography size="text-xs" color="tertiary">
                Position: {{ quantity }} {{ selectedToken }} ≈ ${{ positionUsd.toLocaleString(undefined, { maximumFractionDigits: 2 }) }}
                <span v-if="twapChunks > 0">| {{ twapChunks }} chunks ≈ ${{ (positionUsd / twapChunks).toFixed(2) }} each</span>
              </Typography>
            </div>
          </template>

          <!-- ── Step 5: Summary ── -->
          <template v-if="step === 5">
            <Typography size="text-md" weight="semibold">Summary</Typography>
            <div :class="$style.summaryGrid">
              <div :class="$style.summaryRow">
                <Typography size="text-xs" color="tertiary">Bot ID</Typography>
                <Typography size="text-sm" weight="medium">{{ botId }}</Typography>
              </div>
              <div :class="$style.summaryRow">
                <Typography size="text-xs" color="tertiary">Token</Typography>
                <Typography size="text-sm" weight="medium">{{ selectedToken }}</Typography>
              </div>
              <div :class="$style.summaryRow">
                <Typography size="text-xs" color="tertiary">Long</Typography>
                <Typography size="text-sm">{{ displayExchange(longExchange) }} — {{ instrumentForExchange(selectedToken, longExchange) }}</Typography>
              </div>
              <div :class="$style.summaryRow">
                <Typography size="text-xs" color="tertiary">Short</Typography>
                <Typography size="text-sm">{{ displayExchange(shortExchange) }} — {{ instrumentForExchange(selectedToken, shortExchange) }}</Typography>
              </div>
              <div :class="$style.summaryRow">
                <Typography size="text-xs" color="tertiary">Funding Spread</Typography>
                <Typography size="text-sm" weight="semibold" :style="{ color: currentSpread !== null && currentSpread >= 0 ? '#22c55e' : '#ef4444' }">
                  {{ currentSpread !== null ? (currentSpread >= 0 ? '+' : '') + (currentSpread * 100).toFixed(2) + '% APR' : '—' }}
                </Typography>
              </div>
              <div :class="$style.summaryRow">
                <Typography size="text-xs" color="tertiary">Quantity</Typography>
                <Typography size="text-sm">{{ quantity }} {{ selectedToken }}{{ positionUsd !== null ? ' ≈ $' + positionUsd.toLocaleString(undefined, { maximumFractionDigits: 2 }) : '' }}</Typography>
              </div>
              <div :class="$style.summaryRow">
                <Typography size="text-xs" color="tertiary">Leverage</Typography>
                <Typography size="text-sm">{{ leverage }}x</Typography>
              </div>
              <div :class="$style.summaryRow">
                <Typography size="text-xs" color="tertiary">Chunks / Interval</Typography>
                <Typography size="text-sm">{{ twapChunks }} / {{ twapInterval }}s</Typography>
              </div>
              <div :class="$style.summaryRow">
                <Typography size="text-xs" color="tertiary">Maker</Typography>
                <Typography size="text-sm">{{ displayExchange(makerExchange) }}</Typography>
              </div>
              <div v-if="simulation" :class="$style.summaryRow">
                <Typography size="text-xs" color="tertiary">Mode</Typography>
                <Typography size="text-sm" color="error">Simulation</Typography>
              </div>
            </div>
          </template>

          <!-- Error -->
          <div v-if="error" :class="$style.error">
            <Typography size="text-sm" color="error">{{ error }}</Typography>
          </div>
        </div>

        <!-- Footer -->
        <div :class="$style.footer">
          <Button v-if="step > 1" variant="outline" size="md" @click="prev">Back</Button>
          <div style="flex:1"></div>
          <Button variant="outline" size="md" @click="emit('close')">Cancel</Button>
          <Button
            v-if="step < 5"
            variant="solid"
            size="md"
            :disabled="!canNext()"
            @click="next"
          >Next</Button>
          <Button
            v-if="step === 5"
            variant="solid"
            color="success"
            size="md"
            :loading="submitting"
            @click="submit"
          >Create Bot</Button>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style module>
.overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
  animation: fade-in var(--duration-lg) var(--ease-out-1);
}

.modal {
  background: var(--color-bg-secondary);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-xl);
  width: 620px;
  max-width: 90vw;
  max-height: 90vh;
  overflow-y: auto;
  animation: fade-in var(--duration-lg) var(--ease-out-2);
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--space-5) var(--space-6);
  border-bottom: 1px solid var(--color-stroke-divider);
}

.closeBtn {
  color: var(--color-text-tertiary);
  font-size: 18px;
  cursor: pointer;
  background: none;
  border: none;
  padding: var(--space-1);
}
.closeBtn:hover { color: var(--color-text-primary); }

/* Stepper */
.stepper {
  display: flex;
  justify-content: center;
  gap: var(--space-3);
  padding: var(--space-4) var(--space-6);
  border-bottom: 1px solid var(--color-stroke-divider);
}

.stepDot {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 600;
  border: 2px solid var(--color-stroke-divider);
  color: var(--color-text-tertiary);
  transition: all 0.15s;
}
.stepActive {
  border-color: var(--color-brand, #6366f1);
  color: var(--color-brand, #6366f1);
  background: rgba(99, 102, 241, 0.08);
}
.stepDone {
  border-color: #22c55e;
  background: #22c55e;
  color: #fff;
}

.body {
  padding: var(--space-5) var(--space-6);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
  min-height: 280px;
}

.row {
  display: flex;
  gap: var(--space-4);
}
.row > .field { flex: 1; }

.field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.label {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  font-weight: 500;
}

.input {
  height: 40px;
  padding: 0 var(--space-3);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  color: var(--color-text-primary);
  font-size: var(--text-md);
  outline: none;
}
.input:focus { border-color: var(--color-brand, #6366f1); }

.inputWithHint {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.toggle {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  height: 40px;
}

.error {
  padding: var(--space-2) var(--space-3);
  background: var(--color-error-bg);
  border: 1px solid var(--color-error-stroke);
  border-radius: var(--radius-sm);
}

.loadingText { padding: var(--space-4) 0; text-align: center; }

/* Step 1: DEX selection */
.dexRow {
  display: flex;
  gap: 8px;
}

.dexBtn {
  flex: 1;
  padding: 12px 16px;
  border-radius: var(--radius-md);
  border: 2px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  cursor: pointer;
  transition: all 0.15s;
  display: flex;
  align-items: center;
  justify-content: center;
}
.dexBtn:hover { border-color: var(--color-text-secondary); }
.dexSelected {
  border-color: var(--color-brand, #6366f1);
  background: rgba(99, 102, 241, 0.1);
}
.dexDimmed {
  opacity: 0.3;
  cursor: default;
}
.dexDimmed:hover { border-color: var(--color-stroke-divider); }

.dexLogo {
  height: 24px;
  width: auto;
}

/* Text-only fallback for exchanges without an SVG asset (e.g. RISEx).
   Sized to match the 24px logo height so the DEX picker stays visually
   consistent. dexBadgeSmall mirrors the 16px variant used in the table
   header. */
.dexBadge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 24px;
  padding: 0 10px;
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.5px;
  color: var(--color-text-primary);
  background: var(--color-white-8, rgba(255, 255, 255, 0.08));
  border-radius: 4px;
}
.dexBadgeSmall {
  display: inline-flex;
  align-items: center;
  height: 16px;
  padding: 0 6px;
  font-size: 10px;
  font-weight: 600;
  color: var(--color-text-secondary);
  background: var(--color-white-8, rgba(255, 255, 255, 0.08));
  border-radius: 3px;
}

/* Step 1: Token table */
.tokenTable {
  max-height: 320px;
  overflow-y: auto;
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
}

.ttable {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.ttable thead { position: sticky; top: 0; z-index: 1; }

.ttable th {
  padding: 8px 10px;
  text-align: left;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.03em;
  background: var(--color-bg-secondary);
  border-bottom: 1px solid var(--color-stroke-divider);
  white-space: nowrap;
}

.thSort { cursor: pointer; user-select: none; }
.thSort:hover { color: var(--color-text-primary); }
.thRight { text-align: right; }
.thExchange { text-align: center; }

.trow {
  cursor: pointer;
  transition: background 0.1s;
}
.trow:hover { background: var(--color-white-4); }
.trowSelected {
  background: rgba(99, 102, 241, 0.08);
}
.trowSelected:hover { background: rgba(99, 102, 241, 0.12); }

.tcell {
  padding: 8px 10px;
  border-bottom: 1px solid var(--color-stroke-divider);
  color: var(--color-text-primary);
  white-space: nowrap;
}
.tcRight { text-align: right; }
.tcExchange { text-align: center; }
.tcExchange span { vertical-align: middle; }
.tcExchange .roleBadge { margin-left: 4px; }

.selectedInfo {
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  background: var(--color-white-2);
  border: 1px solid var(--color-stroke-divider);
}

/* Step 2: Timeframe row */
.tfRow {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.tfBtn {
  padding: 4px 12px;
  border-radius: var(--radius-sm);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.1s;
}
.tfBtn:hover { border-color: var(--color-text-secondary); }
.tfActive {
  border-color: var(--color-brand, #6366f1);
  background: rgba(99, 102, 241, 0.1);
  color: var(--color-brand, #6366f1);
}

/* Funding table */
.fundingTable {
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  overflow: hidden;
}

.ftRow {
  display: flex;
  align-items: center;
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-stroke-divider);
}
.ftRow:last-child { border-bottom: none; }
.ftHeader { background: var(--color-bg-secondary); }

.ftCell { flex: 1; }
.ftRight { text-align: right; }
.ftCenter { text-align: center; }

.roleBadge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-tertiary);
}
.roleLong { background: rgba(34, 197, 94, 0.12); color: #22c55e; }
.roleShort { background: rgba(239, 68, 68, 0.12); color: #ef4444; }

.spreadCard {
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
}
.spreadRow {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

/* Step 3: Price info */
.priceInfo {
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.priceRow {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

/* Step 5: Summary */
.summaryGrid {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
}
.summaryRow {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 2px 0;
}

.footer {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-5) var(--space-6);
  border-top: 1px solid var(--color-stroke-divider);
}

/* Validation messages */
.validationError {
  padding: var(--space-3);
  border-radius: var(--radius-md);
  background: rgba(239, 68, 68, 0.08);
  border: 1px solid rgba(239, 68, 68, 0.3);
}

.validationWarning {
  padding: var(--space-3);
  border-radius: var(--radius-md);
  background: rgba(234, 179, 8, 0.08);
  border: 1px solid rgba(234, 179, 8, 0.3);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.validationInfo {
  padding: var(--space-2);
  border-radius: var(--radius-md);
  background: var(--color-white-2);
  border: 1px solid var(--color-stroke-divider);
}

.sizeSummary {
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  background: var(--color-white-2);
  border: 1px solid var(--color-stroke-divider);
}

.makerHint {
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-sm);
  background: rgba(99, 102, 241, 0.06);
  border: 1px solid rgba(99, 102, 241, 0.2);
  margin-top: calc(-1 * var(--space-1));
}
</style>
