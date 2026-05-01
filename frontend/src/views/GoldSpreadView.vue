<script setup lang="ts">
/**
 * GoldSpreadView — PAXG vs XAUT spread monitor and convergence bot.
 *
 * Layout (top → bottom):
 *   1. Header: state chip + Start/Stop/Reset controls
 *   2. Live KPI strip: PAXG mid, XAUT mid, current spread, signal counter
 *   3. Spread chart (lightweight-charts) with two draggable threshold lines
 *   4. Config panel (entry/exit thresholds, quantity, simulation toggle)
 *   5. Position card (only when HOLDING)
 *   6. Activity log
 */
import { ref, computed, onMounted, onUnmounted, nextTick, watch } from 'vue'
import {
  createChart,
  LineSeries,
  ColorType,
  LineStyle,
  type IChartApi,
  type ISeriesApi,
  type IPriceLine,
  type LineData,
  type Time,
} from 'lightweight-charts'

import {
  startGoldSpread,
  stopGoldSpread,
  resetGoldSpread,
  updateGoldSpreadConfig,
  fetchGoldSpreadHistory,
  fetchOmsSpreadLatest,
} from '@/lib/gold-spread-api'
import { useGoldSpreadStream } from '@/composables/useGoldSpreadStream'
import type {
  GoldSpreadHistoryPoint,
  GoldSpreadRange,
  OmsGoldSpreadLatest,
} from '@/types/gold-spread'
import Button from '@/components/ui/Button.vue'
import Typography from '@/components/ui/Typography.vue'

// ── Live status stream ────────────────────────────────
const { data: status, connected, error: streamError } = useGoldSpreadStream(2000)

// ── OMS live polling (always-on fallback) ─────────────
// The OMS publishes a fresh PAXG/XAUT snapshot on every Variational poll
// (~1.2 s) regardless of bot state. We poll it at 5 s here so the chart
// and KPIs keep updating even when the user has not started the bot.
const omsLatest = ref<OmsGoldSpreadLatest | null>(null)
const omsError = ref<string | null>(null)
let omsPollInterval: ReturnType<typeof setInterval> | null = null

async function pollOms() {
  try {
    omsLatest.value = await fetchOmsSpreadLatest()
    omsError.value = null
  } catch (e) {
    omsError.value = e instanceof Error ? e.message : String(e)
  }
}

// ── UI state ──────────────────────────────────────────
const range = ref<GoldSpreadRange>('24h')
const historyLoading = ref(false)
const historyError = ref('')
const history = ref<GoldSpreadHistoryPoint[]>([])
const actionLoading = ref(false)
const actionError = ref('')

// Editable config — always in USD. The backend still supports a
// threshold_in_pct flag for forward compatibility, but the UI exposes only
// USD thresholds because the chart Y-axis plots the USD spread.
const editEntry = ref<number>(15)
const editExit = ref<number>(5)
const editQty = ref<number>(1)
const editSim = ref<boolean>(true)

// Per-threshold save status — drives the small "Saved" / error hint shown
// next to each input after a drag/blur commit.
const entrySaveState = ref<'idle' | 'saving' | 'saved' | 'error'>('idle')
const exitSaveState = ref<'idle' | 'saving' | 'saved' | 'error'>('idle')
let entrySaveTimer: ReturnType<typeof setTimeout> | null = null
let exitSaveTimer: ReturnType<typeof setTimeout> | null = null

function flashSaveState(which: 'entry' | 'exit', state: 'saved' | 'error') {
  if (which === 'entry') {
    entrySaveState.value = state
    if (entrySaveTimer) clearTimeout(entrySaveTimer)
    entrySaveTimer = setTimeout(() => { entrySaveState.value = 'idle' }, 1500)
  } else {
    exitSaveState.value = state
    if (exitSaveTimer) clearTimeout(exitSaveTimer)
    exitSaveTimer = setTimeout(() => { exitSaveState.value = 'idle' }, 1500)
  }
}

// Sync edit fields once status arrives
watch(
  () => status.value?.config,
  (cfg) => {
    if (!cfg) return
    editEntry.value = cfg.entry_spread
    editExit.value = cfg.exit_spread
    editQty.value = cfg.quantity
    editSim.value = cfg.simulation
    // If the backend somehow has threshold_in_pct=true (e.g. left over
    // from earlier dev), force it back to false so the chart stays aligned
    // with the user's expectations.
    if (cfg.threshold_in_pct) {
      updateGoldSpreadConfig({ threshold_in_pct: false }).catch(() => {})
    }
  },
  { immediate: true },
)

// ── Computed ──────────────────────────────────────────
const stateLabel = computed(() => status.value?.state || 'IDLE')
const stateColor = computed(() => {
  switch (status.value?.state) {
    case 'HOLDING':
      return 'var(--color-success)'
    case 'MONITORING':
      return 'var(--color-brand)'
    case 'ENTERING':
    case 'EXITING':
      return 'var(--color-warning)'
    case 'ERROR':
      return 'var(--color-error)'
    default:
      return 'var(--color-text-secondary)'
  }
})

// Prefer the bot's snapshot when available (it carries signal/confirmation
// info), otherwise fall back to the OMS poll so KPIs stay live even when
// the bot is stopped.
const currentExecSpread = computed(() =>
  status.value?.spread?.exec_spread ?? omsLatest.value?.exec_spread ?? null,
)
const currentExitExecSpread = computed(() =>
  status.value?.spread?.exit_exec_spread ?? omsLatest.value?.exit_exec_spread ?? null,
)
const currentDirection = computed(() =>
  status.value?.spread?.direction ?? omsLatest.value?.direction ?? null,
)
const paxgMid = computed(() =>
  status.value?.spread?.paxg_mid ?? omsLatest.value?.paxg_mid ?? null,
)
const xautMid = computed(() =>
  status.value?.spread?.xaut_mid ?? omsLatest.value?.xaut_mid ?? null,
)

function fmt(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
}

/** Format a USD spread value with `$` prefix and a sign that makes
 * negative spreads (XAUT > PAXG) immediately visible. */
function fmtUsd(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  const sign = value < 0 ? '-' : ''
  const abs = Math.abs(value).toLocaleString(undefined, {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })
  return `${sign}$${abs}`
}

/** Human-readable label for a direction value. */
function directionLabel(dir: string | null | undefined): string {
  if (dir === 'paxg_premium') return 'PAXG Premium'
  if (dir === 'xaut_premium') return 'XAUT Premium'
  return ''
}

// ── Chart references ──────────────────────────────────
const chartContainer = ref<HTMLDivElement | null>(null)
let chart: IChartApi | null = null
let spreadSeries: ISeriesApi<'Line'> | null = null
let paxgSeries: ISeriesApi<'Line'> | null = null
let xautSeries: ISeriesApi<'Line'> | null = null
let entryPriceLine: IPriceLine | null = null
let exitPriceLine: IPriceLine | null = null
let resizeObserver: ResizeObserver | null = null

// Drag-line state
const dragging = ref<'entry' | 'exit' | null>(null)

// ── Chart rendering ───────────────────────────────────
function buildChart() {
  if (!chartContainer.value) return
  if (chart) {
    chart.remove()
    chart = null
    spreadSeries = paxgSeries = xautSeries = null
    entryPriceLine = exitPriceLine = null
  }

  chart = createChart(chartContainer.value, {
    width: chartContainer.value.clientWidth,
    height: 460,
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
      secondsVisible: false,
      borderColor: 'rgba(255,255,255,0.08)',
    },
    rightPriceScale: {
      borderColor: 'rgba(255,255,255,0.08)',
    },
  })

  // The dominant series is the **executable entry spread** — direction-
  // aware, computed from real bid/ask prices. The Y-axis is always USD;
  // the threshold inputs and drag-lines snap to this exact value.
  spreadSeries = chart.addSeries(LineSeries, {
    color: '#f59e0b', // amber/gold
    lineWidth: 2,
    title: 'Exec Spread USD',
    priceFormat: {
      type: 'custom',
      formatter: (price: number) => {
        const sign = price < 0 ? '-' : ''
        return `${sign}$${Math.abs(price).toFixed(2)}`
      },
      minMove: 0.01,
    },
  })

  paxgSeries = chart.addSeries(LineSeries, {
    color: '#fbbf24', // gold
    lineWidth: 1,
    title: 'PAXG Mid',
    priceScaleId: 'right_prices',
    visible: false, // hidden by default — toggle below
  })

  xautSeries = chart.addSeries(LineSeries, {
    color: '#94a3b8', // silver
    lineWidth: 1,
    title: 'XAUT Mid',
    priceScaleId: 'right_prices',
    visible: false,
  })

  installEntryExitLines()
}

function installEntryExitLines() {
  if (!spreadSeries) return
  if (entryPriceLine) {
    spreadSeries.removePriceLine(entryPriceLine)
    entryPriceLine = null
  }
  if (exitPriceLine) {
    spreadSeries.removePriceLine(exitPriceLine)
    exitPriceLine = null
  }
  entryPriceLine = spreadSeries.createPriceLine({
    price: editEntry.value,
    color: '#ef4444',
    lineWidth: 2,
    lineStyle: LineStyle.Dashed,
    axisLabelVisible: true,
    title: `Entry ≥ ${fmtUsd(editEntry.value)}`,
  })
  exitPriceLine = spreadSeries.createPriceLine({
    price: editExit.value,
    color: '#22c55e',
    lineWidth: 2,
    lineStyle: LineStyle.Dashed,
    axisLabelVisible: true,
    title: `Exit ≤ ${fmtUsd(editExit.value)}`,
  })
}

/**
 * Sync a draggable PriceLine to a new price *without* mutating the
 * `editEntry` / `editExit` ref — that ref is the source of truth and is
 * watched separately. Mutating it inside the drag-handler would ping-pong
 * with the `watch(editEntry, ...)` watcher.
 */
function syncEntryLineUi(v: number) {
  if (entryPriceLine) {
    entryPriceLine.applyOptions({
      price: v,
      title: `Entry ≥ ${fmtUsd(v)}`,
    })
  }
}

function syncExitLineUi(v: number) {
  if (exitPriceLine) {
    exitPriceLine.applyOptions({
      price: v,
      title: `Exit ≤ ${fmtUsd(v)}`,
    })
  }
}

// ── History loading ───────────────────────────────────
async function loadHistory() {
  historyLoading.value = true
  historyError.value = ''
  try {
    const resp = await fetchGoldSpreadHistory(range.value)
    history.value = resp.points
    paintHistory()
  } catch (e) {
    historyError.value = e instanceof Error ? e.message : String(e)
  } finally {
    historyLoading.value = false
  }
}

function paintHistory() {
  if (!chart || !spreadSeries) return
  const spreadData: LineData[] = []
  const paxgData: LineData[] = []
  const xautData: LineData[] = []
  // Dedupe by floor(ts/1000) — lightweight-charts requires unique strictly-
  // increasing timestamps and the AE 1m/5m buckets can occasionally land on
  // the same second when boundaries collide.
  const seen = new Set<number>()
  for (const p of history.value) {
    const t = Math.floor(p.ts / 1000)
    if (seen.has(t)) continue
    seen.add(t)
    const time = t as Time
    // Use exec_spread when available (new data); fall back to mid-spread
    // for old data points that were written before exec_spread was added.
    const execVal = p.exec_spread ?? p.spread
    spreadData.push({ time, value: execVal })
    if (p.paxg_mid) paxgData.push({ time, value: p.paxg_mid })
    if (p.xaut_mid) xautData.push({ time, value: p.xaut_mid })
  }
  spreadSeries.setData(spreadData)
  if (paxgSeries) paxgSeries.setData(paxgData)
  if (xautSeries) xautSeries.setData(xautData)
  chart?.timeScale().fitContent()
}

// Live overlay — append the latest live snapshot once per stream tick so
// the rightmost edge of the chart updates without a full reload.
function appendLiveTick() {
  if (!spreadSeries) return
  const botSnap = status.value?.spread
  // Bot wins when the bot is running — its timestamp is in epoch *seconds*.
  if (botSnap) {
    const t = Math.floor(botSnap.ts) as Time
    spreadSeries.update({ time: t, value: botSnap.exec_spread })
    if (paxgSeries) paxgSeries.update({ time: t, value: botSnap.paxg_mid })
    if (xautSeries) xautSeries.update({ time: t, value: botSnap.xaut_mid })
    return
  }
  // No bot snapshot → use OMS poll. Its `ts_ms` is in milliseconds so we
  // divide here to get the same epoch-second axis as the historical data.
  const oms = omsLatest.value
  if (!oms) return
  const t = Math.floor(oms.ts_ms / 1000) as Time
  spreadSeries.update({ time: t, value: oms.exec_spread })
  if (paxgSeries) paxgSeries.update({ time: t, value: oms.paxg_mid })
  if (xautSeries) xautSeries.update({ time: t, value: oms.xaut_mid })
}

watch(() => status.value?.spread?.ts, () => {
  appendLiveTick()
})

// Also append a live tick whenever the OMS poll comes back with a newer
// timestamp. This keeps the chart moving even when the bot is stopped.
watch(() => omsLatest.value?.ts_ms, () => {
  appendLiveTick()
})

watch(range, () => {
  loadHistory()
})

// ── Drag interaction on threshold lines ───────────────
/**
 * Pixel tolerance for hit-testing a threshold PriceLine. Generous enough
 * (16 px ≈ ~3 mm on a typical screen) that the user can grab the line
 * without pixel-perfect aim, especially while the line is also moving in
 * real time as live data ticks in.
 */
const DRAG_TOL_PX = 16
/** True while the cursor is hovering near a draggable line (drives cursor). */
const hovering = ref<'entry' | 'exit' | null>(null)

function detectLineAtY(y: number): 'entry' | 'exit' | null {
  if (!spreadSeries) return null
  const entryY = spreadSeries.priceToCoordinate(editEntry.value)
  const exitY = spreadSeries.priceToCoordinate(editExit.value)
  if (entryY !== null && Math.abs(y - entryY) <= DRAG_TOL_PX) return 'entry'
  if (exitY !== null && Math.abs(y - exitY) <= DRAG_TOL_PX) return 'exit'
  return null
}

/**
 * lightweight-charts has its own pan/scroll on mousedown. When we're
 * about to drag a threshold line we toggle those off so the chart
 * doesn't scroll out from under us mid-drag. Re-enabled when the cursor
 * leaves the hover zone *and* no drag is in progress.
 */
function setChartInteraction(enabled: boolean) {
  if (!chart) return
  chart.applyOptions({
    handleScroll: enabled,
    handleScale: enabled,
  })
}

watch(hovering, (val) => {
  if (val) {
    // Cursor is near a drag-line → freeze the chart so the line is the
    // only thing that responds to mousedown/drag.
    setChartInteraction(false)
  } else if (!dragging.value) {
    // No hover and no active drag → restore normal pan/zoom.
    setChartInteraction(true)
  }
})

function onMouseDown(ev: MouseEvent) {
  if (!chart || !spreadSeries || !chartContainer.value) return
  const rect = chartContainer.value.getBoundingClientRect()
  const y = ev.clientY - rect.top
  const which = detectLineAtY(y)
  if (which) {
    dragging.value = which
    // Belt-and-braces: even though the hovering watcher already disables
    // chart pan when the cursor enters the hit zone, lightweight-charts
    // listens for mousedown on its own canvas. Disable interaction
    // again synchronously here so a fast click can't slip through before
    // the watcher has fired.
    setChartInteraction(false)
    ev.preventDefault()
    ev.stopPropagation()
  }
}

function onMouseMove(ev: MouseEvent) {
  if (!chartContainer.value || !spreadSeries) return
  const rect = chartContainer.value.getBoundingClientRect()
  const y = ev.clientY - rect.top

  if (dragging.value) {
    // While dragging: update the underlying threshold value, the watcher
    // below will re-paint the line.
    const newPrice = spreadSeries.coordinateToPrice(y)
    if (newPrice === null) return
    const rounded = Number(newPrice.toFixed(4))
    if (dragging.value === 'entry') {
      editEntry.value = rounded
    } else {
      editExit.value = rounded
    }
    return
  }

  // Not dragging: update hover state for cursor styling.
  // Only check if the cursor is within the chart container.
  if (
    ev.clientX >= rect.left && ev.clientX <= rect.right &&
    ev.clientY >= rect.top && ev.clientY <= rect.bottom
  ) {
    hovering.value = detectLineAtY(y)
  } else if (hovering.value) {
    hovering.value = null
  }
}

async function onMouseUp() {
  if (!dragging.value) return
  const which = dragging.value
  dragging.value = null
  // After a drag, the cursor is usually still near the line — re-enable
  // chart pan/zoom only if we've fully left the hover zone, otherwise
  // leave it disabled so a quick re-grab still works.
  if (!hovering.value) {
    setChartInteraction(true)
  }
  // Commit the new threshold to the backend.
  if (which === 'entry') {
    entrySaveState.value = 'saving'
  } else {
    exitSaveState.value = 'saving'
  }
  try {
    if (which === 'entry') {
      await updateGoldSpreadConfig({ entry_spread: editEntry.value })
    } else {
      await updateGoldSpreadConfig({ exit_spread: editExit.value })
    }
    flashSaveState(which, 'saved')
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : String(e)
    flashSaveState(which, 'error')
  }
}

// Single source-of-truth watchers: any change to the editEntry/editExit
// refs (drag, numeric input, server sync) repaints the chart line.
watch(editEntry, (v) => syncEntryLineUi(v))
watch(editExit, (v) => syncExitLineUi(v))

async function commitEntryInput() {
  actionError.value = ''
  entrySaveState.value = 'saving'
  try {
    await updateGoldSpreadConfig({ entry_spread: editEntry.value })
    flashSaveState('entry', 'saved')
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : String(e)
    flashSaveState('entry', 'error')
  }
}

async function commitExitInput() {
  actionError.value = ''
  exitSaveState.value = 'saving'
  try {
    await updateGoldSpreadConfig({ exit_spread: editExit.value })
    flashSaveState('exit', 'saved')
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : String(e)
    flashSaveState('exit', 'error')
  }
}

async function commitQuantity() {
  actionError.value = ''
  try {
    await updateGoldSpreadConfig({ quantity: editQty.value })
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : String(e)
  }
}

/**
 * Commit a simulation-mode toggle. When the user is *enabling* live
 * trading (sim=False) we require a typed-in confirmation phrase before
 * sending the config update — and we revert the checkbox if the user
 * cancels. Re-enabling simulation (sim=True) is a one-click safety
 * action with no confirmation.
 */
async function commitSimulation() {
  actionError.value = ''
  // Going from simulation=true to simulation=false → require confirm.
  const previousSim = status.value?.config?.simulation ?? true
  if (previousSim && !editSim.value) {
    const confirmed = window.confirm(
      'LIVE TRADING WARNING\n\n' +
      'You are about to disable simulation mode. The Gold-Spread bot will ' +
      'place REAL IOC orders on Variational the next time the entry ' +
      'threshold is met.\n\n' +
      `Current settings:\n` +
      `  • Quantity per leg: ${editQty.value}\n` +
      `  • Entry threshold: $${editEntry.value}\n` +
      `  • Exit threshold:  $${editExit.value}\n` +
      `  • Max slippage:    ${status.value?.config?.max_slippage_pct ?? '—'}%\n` +
      `  • Max notional:    $${status.value?.config?.max_position_value_usd ?? '—'}\n\n` +
      'Continue?'
    )
    if (!confirmed) {
      // Revert local toggle without firing a backend update.
      editSim.value = true
      return
    }
  }
  try {
    await updateGoldSpreadConfig({ simulation: editSim.value })
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : String(e)
    // Roll back the toggle so the UI matches the actual backend state.
    editSim.value = previousSim
  }
}

// ── Bot actions ───────────────────────────────────────
async function doStart() {
  actionLoading.value = true
  actionError.value = ''
  try {
    await startGoldSpread()
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : String(e)
  } finally {
    actionLoading.value = false
  }
}

async function doStop() {
  actionLoading.value = true
  actionError.value = ''
  try {
    await stopGoldSpread()
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : String(e)
  } finally {
    actionLoading.value = false
  }
}

async function doReset() {
  actionLoading.value = true
  actionError.value = ''
  try {
    await resetGoldSpread()
  } catch (e) {
    actionError.value = e instanceof Error ? e.message : String(e)
  } finally {
    actionLoading.value = false
  }
}

// ── Lifecycle ─────────────────────────────────────────
onMounted(async () => {
  await nextTick()
  buildChart()
  await loadHistory()

  // Kick off the OMS poll immediately and at a 5 s cadence. Independent
  // of the bot's SSE stream so the chart keeps updating when the bot is
  // stopped.
  pollOms()
  omsPollInterval = setInterval(pollOms, 5000)

  if (chartContainer.value) {
    resizeObserver = new ResizeObserver(() => {
      if (chart && chartContainer.value) {
        chart.applyOptions({ width: chartContainer.value.clientWidth })
      }
    })
    resizeObserver.observe(chartContainer.value)
    // Use `capture: true` so we get the mousedown before lightweight-
    // charts' own canvas listener does. That lets us call stopPropagation
    // and prevent the chart's pan kick-in when we're about to drag a
    // threshold line.
    chartContainer.value.addEventListener('mousedown', onMouseDown, true)
    window.addEventListener('mousemove', onMouseMove)
    window.addEventListener('mouseup', onMouseUp)
  }
})

onUnmounted(() => {
  resizeObserver?.disconnect()
  if (chartContainer.value) {
    chartContainer.value.removeEventListener('mousedown', onMouseDown, true)
  }
  window.removeEventListener('mousemove', onMouseMove)
  window.removeEventListener('mouseup', onMouseUp)
  if (entrySaveTimer) clearTimeout(entrySaveTimer)
  if (exitSaveTimer) clearTimeout(exitSaveTimer)
  if (omsPollInterval) clearInterval(omsPollInterval)
  if (chart) chart.remove()
})

/** CSS cursor for the chart container. Reflects drag/hover state. */
const chartCursor = computed(() => {
  if (dragging.value) return 'grabbing'
  if (hovering.value) return 'grab'
  return 'crosshair'
})
</script>

<template>
  <div :class="$style.View">
    <!-- Header -->
    <div :class="$style.Header">
      <div :class="$style.HeaderLeft">
        <Typography size="text-h5" weight="semibold">Gold Spread Bot</Typography>
        <span :class="$style.Subtitle">PAXG vs XAUT · Variational</span>
      </div>
      <div :class="$style.HeaderRight">
        <span
          :class="$style.StateChip"
          :style="{ borderColor: stateColor, color: stateColor }"
        >
          <span :class="$style.Dot" :style="{ background: stateColor }" />
          {{ stateLabel }}
        </span>
        <Button
          v-if="!status?.running"
          variant="solid"
          color="success"
          :loading="actionLoading"
          @click="doStart"
        >
          Start
        </Button>
        <Button
          v-else
          variant="outline"
          color="warning"
          :loading="actionLoading"
          @click="doStop"
        >
          Stop
        </Button>
        <Button variant="ghost" :loading="actionLoading" @click="doReset">
          Reset
        </Button>
      </div>
    </div>

    <!-- KPI strip -->
    <div :class="$style.KpiStrip">
      <div :class="$style.Kpi">
        <span :class="$style.KpiLabel">PAXG Mid</span>
        <span :class="$style.KpiValue">${{ fmt(paxgMid, 2) }}</span>
      </div>
      <div :class="$style.Kpi">
        <span :class="$style.KpiLabel">XAUT Mid</span>
        <span :class="$style.KpiValue">${{ fmt(xautMid, 2) }}</span>
      </div>
      <div :class="[$style.Kpi, $style.KpiAccent]">
        <span :class="$style.KpiLabel">Exec Spread (USD)</span>
        <span :class="$style.KpiValue">{{ fmtUsd(currentExecSpread, 2) }}</span>
        <span :class="$style.KpiSub">
          <span v-if="currentDirection" :class="$style.DirectionTag">
            {{ directionLabel(currentDirection) }}
          </span>
          Exit {{ fmtUsd(currentExitExecSpread, 2) }}
        </span>
      </div>
      <div :class="$style.Kpi">
        <span :class="$style.KpiLabel">Last Signal</span>
        <span :class="$style.KpiValue">
          {{ status?.last_signal || 'NONE' }}
          <span v-if="status && status.signal_count > 0" :class="$style.SignalCount">
            ({{ status.signal_count }}/{{ status.config.signal_confirmations }})
          </span>
        </span>
      </div>
      <div :class="$style.Kpi">
        <span :class="$style.KpiLabel">Source</span>
        <span :class="$style.KpiValue">
          <template v-if="connected && status?.spread">Bot Stream</template>
          <template v-else-if="omsLatest">OMS Live</template>
          <template v-else>—</template>
        </span>
        <span v-if="streamError && !omsLatest" :class="$style.SmallError">
          {{ streamError }}
        </span>
        <span v-else-if="omsError" :class="$style.SmallError">
          OMS: {{ omsError }}
        </span>
      </div>
    </div>

    <!-- Range selector -->
    <div :class="$style.RangeBar">
      <span :class="$style.RangeLabel">Range:</span>
      <button
        v-for="r in (['1h', '24h', '7d', '30d', 'all'] as GoldSpreadRange[])"
        :key="r"
        :class="[$style.RangeBtn, range === r && $style.RangeBtnActive]"
        @click="range = r"
      >
        {{ r.toUpperCase() }}
      </button>
      <span v-if="historyLoading" :class="$style.HistoryHint">Loading history…</span>
      <span v-if="historyError" :class="$style.HistoryError">{{ historyError }}</span>
    </div>

    <!-- Chart -->
    <div :class="$style.ChartCard">
      <div
        ref="chartContainer"
        :class="$style.Chart"
        :style="{ cursor: chartCursor }"
      />
      <div :class="$style.ChartHint">
        Drag the
        <span :class="$style.LegendEntry">● Entry</span>
        and
        <span :class="$style.LegendExit">● Exit</span>
        lines to set thresholds. Chart shows the
        <strong>executable cross-token spread</strong> — the better of
        <code>PAXG Bid − XAUT Ask</code> and <code>XAUT Bid − PAXG Ask</code>,
        i.e. the real USD value you capture when opening.
      </div>
    </div>

    <!-- Config + Position grid -->
    <div :class="$style.Grid">
      <!-- Config -->
      <div :class="$style.Card">
        <div :class="$style.CardHeader">Config</div>
        <div :class="$style.Form">
          <label :class="[$style.Field, $style.FieldEntry]">
            <span :class="$style.FieldLabel">
              <span :class="$style.DotEntry" />
              Entry threshold (USD)
              <span
                v-if="entrySaveState !== 'idle'"
                :class="[
                  $style.SaveBadge,
                  entrySaveState === 'saved' && $style.SaveBadgeOk,
                  entrySaveState === 'error' && $style.SaveBadgeErr,
                ]"
              >
                {{
                  entrySaveState === 'saving' ? 'Saving…' :
                  entrySaveState === 'saved' ? 'Saved' : 'Error'
                }}
              </span>
            </span>
            <span :class="$style.InputWrap">
              <span :class="$style.InputPrefix">$</span>
              <input
                v-model.number="editEntry"
                type="number"
                step="0.1"
                :class="[$style.Input, $style.InputWithPrefix]"
                @blur="commitEntryInput"
              />
            </span>
            <span :class="$style.FieldHint">
              Open position when spread ≥ {{ fmtUsd(editEntry) }}
            </span>
          </label>
          <label :class="[$style.Field, $style.FieldExit]">
            <span :class="$style.FieldLabel">
              <span :class="$style.DotExit" />
              Exit threshold (USD)
              <span
                v-if="exitSaveState !== 'idle'"
                :class="[
                  $style.SaveBadge,
                  exitSaveState === 'saved' && $style.SaveBadgeOk,
                  exitSaveState === 'error' && $style.SaveBadgeErr,
                ]"
              >
                {{
                  exitSaveState === 'saving' ? 'Saving…' :
                  exitSaveState === 'saved' ? 'Saved' : 'Error'
                }}
              </span>
            </span>
            <span :class="$style.InputWrap">
              <span :class="$style.InputPrefix">$</span>
              <input
                v-model.number="editExit"
                type="number"
                step="0.1"
                :class="[$style.Input, $style.InputWithPrefix]"
                @blur="commitExitInput"
              />
            </span>
            <span :class="$style.FieldHint">
              Close position when spread ≤ {{ fmtUsd(editExit) }}
            </span>
          </label>
          <label :class="$style.Field">
            <span :class="$style.FieldLabel">Quantity (per leg)</span>
            <input
              v-model.number="editQty"
              type="number"
              step="0.1"
              min="0"
              :class="$style.Input"
              @blur="commitQuantity"
            />
            <span :class="$style.FieldHint">
              Notional ≈ ${{ fmt((editQty || 0) * (paxgMid ?? 0), 2) }}
              · max ${{ fmt(status?.config?.max_position_value_usd ?? 10000, 0) }}
            </span>
          </label>
          <label
            :class="[
              $style.FieldInline,
              !editSim && $style.LiveTradingToggle,
            ]"
          >
            <input
              v-model="editSim"
              type="checkbox"
              @change="commitSimulation"
            />
            <span>
              Simulation mode (paper trade)
              <span v-if="!editSim" :class="$style.LiveBadge">LIVE TRADING</span>
            </span>
          </label>
        </div>
        <div v-if="actionError" :class="$style.ErrorBox">
          {{ actionError }}
        </div>
      </div>

      <!-- Position -->
      <div :class="$style.Card">
        <div :class="$style.CardHeader">Position</div>
        <div v-if="!status?.position" :class="$style.Placeholder">
          No open position. Bot is in <strong>{{ stateLabel }}</strong> state.
        </div>
        <div v-else :class="$style.PositionGrid">
          <div :class="$style.PosLeg">
            <Typography size="text-sm" color="secondary">
              SHORT {{ status.position.short_token }}
            </Typography>
            <div :class="$style.PosValue">
              {{ fmt(status.position.short_qty, 4) }}
              <small>@ ${{ fmt(status.position.short_entry_price, 2) }}</small>
            </div>
          </div>
          <div :class="$style.PosLeg">
            <Typography size="text-sm" color="secondary">
              LONG {{ status.position.long_token }}
            </Typography>
            <div :class="$style.PosValue">
              {{ fmt(status.position.long_qty, 4) }}
              <small>@ ${{ fmt(status.position.long_entry_price, 2) }}</small>
            </div>
          </div>
          <div :class="$style.PosLeg">
            <Typography size="text-sm" color="secondary">Entry Spread</Typography>
            <div :class="$style.PosValue">
              {{ fmtUsd(status.position.entry_spread, 2) }}
              <small>{{ directionLabel(status.position.direction) }}</small>
            </div>
          </div>
          <div :class="$style.PosLeg">
            <Typography size="text-sm" color="secondary">Mode</Typography>
            <div :class="$style.PosValue">
              <template v-if="status.position.simulation">Simulation</template>
              <span v-else :class="$style.LiveBadge">LIVE</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- Activity log -->
    <div :class="$style.Card">
      <div :class="$style.CardHeader">Activity</div>
      <div :class="$style.ActivityList">
        <div
          v-for="(entry, idx) in (status?.activity ?? []).slice().reverse().slice(0, 50)"
          :key="idx"
          :class="$style.ActivityRow"
        >
          <span :class="$style.ActivityTime">
            {{ new Date(entry.timestamp * 1000).toLocaleTimeString() }}
          </span>
          <span :class="$style.ActivityEvent">{{ entry.event }}</span>
          <span :class="$style.ActivityMsg">{{ entry.message }}</span>
        </div>
        <div v-if="!status?.activity?.length" :class="$style.Placeholder">
          No activity yet.
        </div>
      </div>
    </div>
  </div>
</template>

<style module>
.View {
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
  padding: var(--space-6);
  font-family: var(--font-inter);
  color: var(--color-text-primary);
  max-width: 1400px;
  margin: 0 auto;
}

.Header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-3);
}

.HeaderLeft {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.Subtitle {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.HeaderRight {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.StateChip {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  padding: 6px var(--space-3);
  border-radius: var(--radius-round);
  border: 1px solid;
  font-size: var(--text-sm);
  font-weight: 500;
  letter-spacing: 0.04em;
}

.Dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
}

.KpiStrip {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: var(--space-3);
  background: var(--color-white-2);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
}

@media (max-width: 900px) {
  .KpiStrip {
    grid-template-columns: repeat(3, minmax(0, 1fr));
  }
}
@media (max-width: 480px) {
  .KpiStrip {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

.Kpi {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.KpiLabel {
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.KpiValue {
  font-size: var(--text-h6);
  font-weight: 600;
  font-family: var(--font-bricolage);
  font-variant-numeric: tabular-nums;
}

/* The Spread KPI is the primary signal — give it a slight gold accent
 * so it stands out from the supporting price KPIs. */
.KpiAccent .KpiValue {
  color: #f59e0b;
}

.KpiSub {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.DirectionTag {
  display: inline-flex;
  align-items: center;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  background: rgba(245, 158, 11, 0.15);
  border: 1px solid rgba(245, 158, 11, 0.3);
  color: #f59e0b;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-weight: 600;
}

/* Visual cue when simulation is OFF — used both on the toggle row and
 * on the position-panel mode cell. Red, capitalised, bordered. */
.LiveBadge {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  margin-left: 6px;
  border-radius: var(--radius-sm);
  background: var(--color-error-bg);
  border: 1px solid var(--color-error-stroke);
  color: var(--color-error-light);
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  font-weight: 700;
}

.LiveTradingToggle {
  background: var(--color-error-bg);
  border: 1px solid var(--color-error-stroke);
  border-radius: var(--radius-md);
  padding: var(--space-2) var(--space-3);
  color: var(--color-error-light);
}

.SignalCount {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  margin-left: var(--space-1);
}

.SmallError {
  font-size: var(--text-xs);
  color: var(--color-error);
}

.RangeBar {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.RangeLabel {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.RangeBtn {
  background: transparent;
  border: 1px solid var(--color-stroke-divider);
  color: var(--color-text-secondary);
  padding: 6px var(--space-3);
  font-size: var(--text-sm);
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--duration-md) var(--ease-out-1);
}

.RangeBtn:hover {
  background: var(--color-white-4);
  color: var(--color-text-primary);
}

.RangeBtnActive {
  background: var(--color-text-primary);
  color: var(--color-text-dark);
  border-color: var(--color-text-primary);
}

.HistoryHint {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.HistoryError {
  font-size: var(--text-sm);
  color: var(--color-error);
}

.ChartCard {
  background: var(--color-white-2);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.Chart {
  width: 100%;
  height: 460px;
  /* cursor is set inline via :style binding (chartCursor computed) */
}

.ChartHint {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.ChartHint strong {
  color: var(--color-text-primary);
  font-weight: 600;
}

.LegendEntry {
  color: #ef4444;
  font-weight: 600;
}

.LegendExit {
  color: #22c55e;
  font-weight: 600;
}

.Grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-4);
}

@media (max-width: 900px) {
  .Grid {
    grid-template-columns: 1fr;
  }
}

.Card {
  background: var(--color-white-2);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.CardHeader {
  font-size: var(--text-md);
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--color-text-secondary);
}

.Form {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.Field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.FieldLabel {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.FieldHint {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
}

/* Threshold rows: a left-edge stripe in the matching chart-line color
 * makes the visual link between the two inputs and their drag-lines
 * unambiguous. */
.FieldEntry {
  border-left: 3px solid #ef4444;
  padding-left: var(--space-3);
  margin-left: -3px;
}

.FieldExit {
  border-left: 3px solid #22c55e;
  padding-left: var(--space-3);
  margin-left: -3px;
}

.DotEntry,
.DotExit {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  display: inline-block;
}

.DotEntry { background: #ef4444; }
.DotExit  { background: #22c55e; }

.FieldInline {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
}

.InputWrap {
  position: relative;
  display: flex;
  align-items: center;
}

.InputPrefix {
  position: absolute;
  left: var(--space-3);
  font-size: var(--text-md);
  color: var(--color-text-secondary);
  font-weight: 500;
  pointer-events: none;
}

.Input {
  background: var(--color-white-2);
  border: 1px solid var(--color-stroke-divider);
  color: var(--color-text-primary);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  font-size: var(--text-md);
  font-family: var(--font-inter);
  outline: none;
  transition: border-color var(--duration-md) var(--ease-out-1);
  width: 100%;
  box-sizing: border-box;
  font-variant-numeric: tabular-nums;
}

.InputWithPrefix {
  padding-left: 24px;
}

.Input:focus {
  border-color: var(--color-text-primary);
}

/* Inline save indicator next to the threshold inputs. */
.SaveBadge {
  margin-left: auto;
  font-size: var(--text-xs);
  padding: 2px var(--space-2);
  border-radius: var(--radius-sm);
  background: var(--color-white-4);
  color: var(--color-text-secondary);
  letter-spacing: 0.04em;
  text-transform: uppercase;
}

.SaveBadgeOk {
  background: var(--color-success-bg);
  color: var(--color-success-light);
  border: 1px solid var(--color-success-stroke);
}

.SaveBadgeErr {
  background: var(--color-error-bg);
  color: var(--color-error-light);
  border: 1px solid var(--color-error-stroke);
}

.ErrorBox {
  background: var(--color-error-bg);
  border: 1px solid var(--color-error-stroke);
  color: var(--color-error-light);
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
}

.Placeholder {
  font-size: var(--text-sm);
  color: var(--color-text-secondary);
  padding: var(--space-3) 0;
}

.PositionGrid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
}

.PosLeg {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-3);
  background: var(--color-white-2);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-md);
}

.PosValue {
  font-size: var(--text-lg);
  font-weight: 600;
  font-family: var(--font-bricolage);
}

.PosValue small {
  display: block;
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
  font-weight: 400;
  font-family: var(--font-inter);
}

.ActivityList {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  max-height: 320px;
  overflow-y: auto;
}

.ActivityRow {
  display: grid;
  grid-template-columns: 80px 140px 1fr;
  gap: var(--space-3);
  font-size: var(--text-sm);
  padding: var(--space-1) 0;
  border-bottom: 1px solid var(--color-white-4);
}

.ActivityTime {
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
}

.ActivityEvent {
  color: var(--color-brand);
  font-weight: 500;
  font-size: var(--text-xs);
  text-transform: uppercase;
  letter-spacing: 0.06em;
}

.ActivityMsg {
  color: var(--color-text-secondary);
}
</style>
