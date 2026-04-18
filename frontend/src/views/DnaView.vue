<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import {
  fetchDnaStatus, fetchDnaPreflight, startDna, stopDna, updateDnaConfig, resetDna, closeDnaPosition, deleteDnaPosition,
  type DNAStatus, type PreflightResult, type PreflightExchangeCheck, type PreflightOmsCheck,
} from '@/lib/api'
import Typography from '@/components/ui/Typography.vue'

// ── State ────────────────────────────────────────────
const loading = ref(true)
const error = ref('')
const status = ref<DNAStatus | null>(null)
const actionLoading = ref(false)
let pollInterval: ReturnType<typeof setInterval> | null = null
let preflightInterval: ReturnType<typeof setInterval> | null = null

// Preflight
const preflight = ref<PreflightResult | null>(null)
const preflightLoading = ref(false)
const preflightError = ref(false)

// Config editing
const editingConfig = ref(false)
const editForm = ref({
  position_size_usd: 1000,
  max_positions: 3,
  spread_mode: 'delta_neutral' as string,
  custom_min_spread_pct: 0.05,
  exit_mode: 'direct' as string,
  exit_min_hold_minutes: 5.0,
  exit_min_hold_hours: 8.0,
  exit_min_hold_days: 7.0,
  exit_threshold_pct: 0.0001,
  simulation: true,
  exchanges: ['extended', 'grvt', 'nado'] as string[],
})

const AVAILABLE_EXCHANGES = ['extended', 'grvt', 'nado']
const SPREAD_MODES = [
  { value: 'delta_neutral', label: 'Delta-Neutral (Full Fees)' },
  { value: 'half_neutral', label: 'Half-Neutral (50% Fees)' },
  { value: 'custom', label: 'Custom (Manual %)' },
]
const EXIT_MODES = [
  { value: 'direct', label: 'Direct (Minutes)' },
  { value: 'hours', label: 'Hours' },
  { value: 'days', label: 'Days' },
  { value: 'manual', label: 'Manual' },
]


// ── Lifecycle ────────────────────────────────────────

async function loadData() {
  try {
    status.value = await fetchDnaStatus()
    error.value = ''
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to load DNA status'
  } finally {
    loading.value = false
  }
}

async function loadPreflight() {
  if (status.value?.running) return // no need when running
  preflightLoading.value = true
  try {
    preflight.value = await fetchDnaPreflight()
    preflightError.value = false
  } catch {
    preflight.value = null
    preflightError.value = true
  } finally {
    preflightLoading.value = false
  }
}

onMounted(async () => {
  await loadData()
  pollInterval = setInterval(loadData, 2000)
  await loadPreflight()
  preflightInterval = setInterval(loadPreflight, 10000)
})

onUnmounted(() => {
  if (pollInterval) clearInterval(pollInterval)
  if (preflightInterval) clearInterval(preflightInterval)
})

// ── Actions ──────────────────────────────────────────

async function handleStart() {
  actionLoading.value = true
  try {
    await startDna()
    await loadData()
    preflight.value = null // clear preflight when running
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to start'
  } finally {
    actionLoading.value = false
  }
}

const canStart = computed(() => {
  if (!preflight.value) return true // allow if preflight not loaded yet (graceful)
  return preflight.value.can_start
})

function preflightCheckLabel(name: string): string {
  if (name === 'oms') return 'OMS'
  if (name === 'grvt') return 'GRVT'
  if (name === 'nado') return 'Nado'
  return name.charAt(0).toUpperCase() + name.slice(1)
}

function isOmsCheck(check: PreflightExchangeCheck | PreflightOmsCheck): check is PreflightOmsCheck {
  return 'health' in check
}

function exchangeCheckOk(check: PreflightExchangeCheck): boolean {
  return check.positions && check.balance !== false && check.signer_ok !== false
}

function omsCheckOk(check: PreflightOmsCheck): boolean {
  return check.health && Object.values(check.books).every(Boolean)
}

async function handleStop() {
  actionLoading.value = true
  try {
    await stopDna()
    await loadData()
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to stop'
  } finally {
    actionLoading.value = false
  }
}

async function handleReset() {
  if (!confirm('Reset DNA Bot? All positions and activity log will be cleared.')) return
  actionLoading.value = true
  try {
    await resetDna()
    await loadData()
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to reset'
  } finally {
    actionLoading.value = false
  }
}

function toggleExchange(ex: string) {
  const idx = editForm.value.exchanges.indexOf(ex)
  if (idx >= 0) {
    if (editForm.value.exchanges.length > 1) editForm.value.exchanges.splice(idx, 1)
  } else {
    editForm.value.exchanges.push(ex)
  }
}

function openConfigEditor() {
  if (status.value) {
    editForm.value = {
      position_size_usd: status.value.config.position_size_usd,
      max_positions: status.value.config.max_positions,
      spread_mode: status.value.config.spread_mode || 'delta_neutral',
      custom_min_spread_pct: +((status.value.config.custom_min_spread_bps ?? 5.0) / 100).toFixed(4),
      exit_mode: status.value.config.exit_mode || 'direct',
      exit_min_hold_minutes: status.value.config.exit_min_hold_minutes ?? 5.0,
      exit_min_hold_hours: status.value.config.exit_min_hold_hours ?? 8.0,
      exit_min_hold_days: status.value.config.exit_min_hold_days ?? 7.0,
      exit_threshold_pct: +((status.value.config.exit_threshold_bps ?? 0.01) / 100).toFixed(6),
      simulation: status.value.config.simulation,
      exchanges: [...status.value.config.exchanges],
    }
  }
  editingConfig.value = true
}

async function saveConfig() {
  actionLoading.value = true
  try {
    const payload = {
      ...editForm.value,
      custom_min_spread_bps: +(editForm.value.custom_min_spread_pct * 100).toFixed(2),
      exit_threshold_bps: +(editForm.value.exit_threshold_pct * 100).toFixed(4),
    }
    delete (payload as any).custom_min_spread_pct
    delete (payload as any).exit_threshold_pct
    await updateDnaConfig(payload)
    await loadData()
    editingConfig.value = false
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to update config'
  } finally {
    actionLoading.value = false
  }
}

// ── Computed ─────────────────────────────────────────

const isRunning = computed(() => status.value?.running ?? false)
const openPositions = computed(() => status.value?.positions?.details ?? [])
const tradeHistory = computed(() => status.value?.trade_history ?? [])
const activityLog = computed(() => {
  const log = status.value?.activity_log ?? []
  return [...log].reverse()
})

// ── Formatting ───────────────────────────────────────

function displayExchange(ex: string): string {
  if (ex === 'grvt') return 'GRVT'
  if (ex === 'nado') return 'Nado'
  return ex.charAt(0).toUpperCase() + ex.slice(1)
}

function formatUsd(val: number): string {
  if (val >= 1_000_000) return `$${(val / 1_000_000).toFixed(1)}M`
  if (val >= 1_000) return `$${(val / 1_000).toFixed(1)}K`
  return `$${val.toFixed(2)}`
}

function formatPrice(val: number): string {
  if (val >= 10000) return val.toFixed(0)
  if (val >= 100) return val.toFixed(2)
  if (val >= 1) return val.toFixed(3)
  return val.toFixed(6)
}

function formatQty(val: number): string {
  if (val >= 1_000) return `${(val / 1_000).toFixed(1)}K`
  if (val >= 1) return val.toFixed(4)
  return val.toFixed(6)
}

function bpsToPercent(bps: number): string {
  return (bps / 100).toFixed(3)
}

function formatTimestamp(epoch: number): string {
  return new Date(epoch * 1000).toLocaleTimeString()
}

async function handleClosePosition(positionId: string, token: string) {
  if (!confirm(`Close position ${positionId} (${token})? Both legs will be market-closed simultaneously.`)) return
  actionLoading.value = true
  try {
    await closeDnaPosition(positionId)
    await loadData()
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to close position'
  } finally {
    actionLoading.value = false
  }
}

async function handleDeleteTrade(positionId: string, token: string) {
  if (!confirm(`Remove trade ${positionId} (${token}) from history?`)) return
  actionLoading.value = true
  try {
    await deleteDnaPosition(positionId)
    await loadData()
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Failed to delete trade'
  } finally {
    actionLoading.value = false
  }
}

function exitModeLabel(mode: string): string {
  return EXIT_MODES.find(m => m.value === mode)?.label || mode
}

function holdDuration(openedAt: number, minHoldS: number): string {
  const held = Date.now() / 1000 - openedAt
  const ready = held >= minHoldS
  const heldStr = held < 3600 ? `${Math.floor(held / 60)}m` : held < 86400 ? `${(held / 3600).toFixed(1)}h` : `${(held / 86400).toFixed(1)}d`
  return ready ? `${heldStr} ✓` : heldStr
}

function formatDuration(openedAt: number, closedAt: number | null): string {
  if (!closedAt) return '-'
  const s = closedAt - openedAt
  if (s < 60) return `${Math.floor(s)}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${(s / 3600).toFixed(1)}h`
  return `${(s / 86400).toFixed(1)}d`
}

function formatDate(epoch: number): string {
  const d = new Date(epoch * 1000)
  return d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' }) + ' ' + d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
}

function eventColor(event: string): string {
  if (event === 'position_opened') return '#22c55e'
  if (event === 'position_closed') return '#8b5cf6'
  if (event === 'position_closing') return '#f59e0b'
  if (event === 'position_close_failed') return '#ef4444'
  if (event === 'entry_failed' || event === 'entry_partial_unwind') return '#ef4444'
  if (event === 'signal') return '#3b82f6'
  if (event === 'started') return '#22c55e'
  if (event === 'stopped') return '#f59e0b'
  if (event === 'exit_monitor') return '#06b6d4'
  if (event === 'ws_connected') return '#06b6d4'
  return 'var(--color-text-tertiary)'
}

function eventLabel(event: string): string {
  const labels: Record<string, string> = {
    started: 'STARTED',
    stopped: 'STOPPED',
    ws_connected: 'WS CONNECTED',
    exit_monitor: 'EXIT MON',
    signal: 'SIGNAL',
    position_opened: 'OPENED',
    position_closing: 'CLOSING',
    position_closed: 'CLOSED',
    position_close_failed: 'CLOSE FAIL',
    entry_failed: 'FAILED',
    entry_partial_unwind: 'UNWIND',
  }
  return labels[event] || event.toUpperCase()
}

</script>

<template>
  <div :class="$style.page">
    <!-- ── Header ── -->
    <div :class="$style.header">
      <div :class="$style.headerLeft">
        <Typography size="text-h5" weight="semibold" font="bricolage">DNA Bot</Typography>
        <Typography size="text-sm" color="tertiary">
          Delta-Neutral Arbitrage — automated cross-exchange position builder
        </Typography>
      </div>
      <div :class="$style.headerRight">
        <span v-if="status" :class="[$style.statusPill, isRunning ? $style.statusRunning : $style.statusStopped]">
          {{ isRunning ? 'Running' : 'Stopped' }}
        </span>
        <span v-if="status?.config?.simulation" :class="$style.simPill">SIM</span>
        <button
          v-if="status && !isRunning"
          :class="[$style.btn, $style.btnStart]"
          :disabled="actionLoading || !canStart"
          :title="!canStart ? 'Pre-flight checks failed — fix connectivity issues first' : ''"
          @click="handleStart"
        >Start</button>
        <button
          v-if="status && isRunning"
          :class="[$style.btn, $style.btnStop]"
          :disabled="actionLoading"
          @click="handleStop"
        >Stop</button>
        <button
          v-if="status && !isRunning"
          :class="[$style.btn, $style.btnConfig]"
          @click="openConfigEditor"
        >Config</button>
        <button
          v-if="status && !isRunning"
          :class="[$style.btn, $style.btnReset]"
          :disabled="actionLoading"
          @click="handleReset"
        >Reset</button>
      </div>
    </div>

    <!-- ── Error ── -->
    <div v-if="error" :class="$style.errorBox">
      <Typography size="text-sm" color="error">{{ error }}</Typography>
    </div>

    <!-- ── Loading ── -->
    <div v-if="loading" :class="$style.empty">
      <Typography color="secondary">Loading DNA bot status...</Typography>
    </div>

    <template v-else-if="status">
      <!-- ── Config Editor Modal ── -->
      <div v-if="editingConfig" :class="$style.configOverlay" @click.self="editingConfig = false">
        <div :class="$style.configModal">
          <Typography size="text-lg" weight="semibold">Bot Configuration</Typography>
          <div :class="$style.configGrid">
            <div :class="$style.configField">
              <label :class="$style.configLabel">Position Size (USD)</label>
              <input v-model.number="editForm.position_size_usd" type="number" :class="$style.configInput" />
            </div>
            <div :class="$style.configField">
              <label :class="$style.configLabel">Max Positions</label>
              <input v-model.number="editForm.max_positions" type="number" min="1" max="20" :class="$style.configInput" />
            </div>
            <div :class="$style.configField">
              <label :class="$style.configLabel">Spread Mode</label>
              <select v-model="editForm.spread_mode" :class="$style.configInput">
                <option v-for="m in SPREAD_MODES" :key="m.value" :value="m.value">{{ m.label }}</option>
              </select>
            </div>
            <div v-if="editForm.spread_mode === 'custom'" :class="$style.configField">
              <label :class="$style.configLabel">Min Spread (%)</label>
              <input v-model.number="editForm.custom_min_spread_pct" type="number" step="0.001" min="0" :class="$style.configInput" />
            </div>
            <div :class="$style.configDivider">
              <Typography size="text-sm" weight="semibold" color="tertiary">Exit Settings</Typography>
            </div>
            <div :class="$style.configField">
              <label :class="$style.configLabel">Exit Mode</label>
              <select v-model="editForm.exit_mode" :class="$style.configInput">
                <option v-for="m in EXIT_MODES" :key="m.value" :value="m.value">{{ m.label }}</option>
              </select>
            </div>
            <div v-if="editForm.exit_mode === 'direct'" :class="$style.configField">
              <label :class="$style.configLabel">Min Hold (Minutes)</label>
              <input v-model.number="editForm.exit_min_hold_minutes" type="number" step="1" min="0" :class="$style.configInput" />
            </div>
            <div v-if="editForm.exit_mode === 'hours'" :class="$style.configField">
              <label :class="$style.configLabel">Min Hold (Hours)</label>
              <input v-model.number="editForm.exit_min_hold_hours" type="number" step="0.5" min="0" :class="$style.configInput" />
            </div>
            <div v-if="editForm.exit_mode === 'days'" :class="$style.configField">
              <label :class="$style.configLabel">Min Hold (Days)</label>
              <input v-model.number="editForm.exit_min_hold_days" type="number" step="1" min="1" :class="$style.configInput" />
            </div>
            <div v-if="editForm.exit_mode !== 'manual'" :class="$style.configField">
              <label :class="$style.configLabel">Exit Threshold (%)</label>
              <input v-model.number="editForm.exit_threshold_pct" type="number" step="0.0001" min="0" :class="$style.configInput" />
            </div>
            <div :class="$style.configField">
              <label :class="$style.configLabel">Exchanges</label>
              <div :class="$style.checkboxGroup">
                <label
                  v-for="ex in AVAILABLE_EXCHANGES"
                  :key="ex"
                  :class="$style.checkboxLabel"
                >
                  <input
                    type="checkbox"
                    :checked="editForm.exchanges.includes(ex)"
                    @change="toggleExchange(ex)"
                    :class="$style.checkbox"
                  />
                  {{ displayExchange(ex) }}
                </label>
              </div>
            </div>
            <div :class="$style.configField">
              <label :class="$style.configLabel">Simulation Mode</label>
              <div :class="$style.toggleWrap">
                <button
                  :class="[$style.toggle, editForm.simulation && $style.toggleOn]"
                  @click="editForm.simulation = !editForm.simulation"
                >
                  <span :class="$style.toggleDot" />
                </button>
                <span :class="$style.toggleLabel">{{ editForm.simulation ? 'On (Paper Trade)' : 'Off (Live)' }}</span>
              </div>
            </div>
          </div>
          <div :class="$style.configActions">
            <button :class="[$style.btn, $style.btnConfig]" @click="editingConfig = false">Cancel</button>
            <button :class="[$style.btn, $style.btnStart]" :disabled="actionLoading" @click="saveConfig">Save</button>
          </div>
        </div>
      </div>

      <!-- ── Stat cards ── -->
      <div :class="$style.statRow">
        <div :class="$style.statCard">
          <div :class="$style.statLabel">
            <span :class="$style.statIcon">⇄</span>
            <Typography size="text-xs" color="tertiary">Open Positions</Typography>
          </div>
          <Typography size="text-h5" weight="bold">
            {{ status.positions.open }} / {{ status.positions.max }}
          </Typography>
        </div>
        <div :class="$style.statCard">
          <div :class="$style.statLabel">
            <span :class="$style.statIcon">$</span>
            <Typography size="text-xs" color="tertiary">Total Notional</Typography>
          </div>
          <Typography size="text-h5" weight="bold">{{ formatUsd(status.positions.total_notional_usd) }}</Typography>
        </div>
        <div :class="$style.statCard">
          <div :class="$style.statLabel">
            <span :class="$style.statIcon">◎</span>
            <Typography size="text-xs" color="tertiary">Position Size</Typography>
          </div>
          <Typography size="text-h5" weight="bold">{{ formatUsd(status.config.position_size_usd) }}</Typography>
        </div>
        <div :class="$style.statCard">
          <div :class="$style.statLabel">
            <span :class="[$style.statIcon, status.config.simulation ? $style.statIconYellow : $style.statIconGreen]">
              {{ status.config.simulation ? '⊘' : '●' }}
            </span>
            <Typography size="text-xs" color="tertiary">Mode</Typography>
          </div>
          <Typography size="text-h5" weight="bold">
            {{ status.config.simulation ? 'Simulation' : 'Live' }}
          </Typography>
        </div>
        <div :class="$style.statCard">
          <div :class="$style.statLabel">
            <span :class="$style.statIcon">⇄</span>
            <Typography size="text-xs" color="tertiary">Spread</Typography>
          </div>
          <Typography size="text-h5" weight="bold">
            {{ SPREAD_MODES.find(m => m.value === status!.config.spread_mode)?.label || status!.config.spread_mode || 'Delta-Neutral' }}
          </Typography>
          <Typography v-if="status.config.spread_mode === 'custom'" size="text-xs" color="tertiary">
            Min {{ ((status.config.custom_min_spread_bps ?? 5) / 100).toFixed(3) }}%
          </Typography>
          <Typography v-else-if="status.config.spread_mode === 'half_neutral'" size="text-xs" color="tertiary">
            50% of fee threshold
          </Typography>
          <Typography v-else size="text-xs" color="tertiary">
            100% of fee threshold
          </Typography>
        </div>
        <div :class="$style.statCard">
          <div :class="$style.statLabel">
            <span :class="$style.statIcon">↩</span>
            <Typography size="text-xs" color="tertiary">Exit</Typography>
          </div>
          <Typography size="text-h5" weight="bold">
            {{ exitModeLabel(status.config.exit_mode || 'direct') }}
          </Typography>
          <Typography v-if="status.config.exit_mode !== 'manual'" size="text-xs" color="tertiary">
            {{ status.config.exit_mode === 'direct' ? `${status.config.exit_min_hold_minutes ?? 5}min` : status.config.exit_mode === 'hours' ? `${status.config.exit_min_hold_hours ?? 8}h` : `${status.config.exit_min_hold_days ?? 7}d` }}
            / {{ ((status.config.exit_threshold_bps ?? 0.01) / 100).toFixed(4) }}%
          </Typography>
        </div>
      </div>

      <!-- ── Pre-Flight Checks ── -->
      <div v-if="!isRunning && (preflight || preflightLoading)" :class="$style.preflightBar">
        <div :class="$style.preflightHeader">
          <Typography size="text-xs" weight="semibold" color="tertiary">Pre-Flight Checks</Typography>
          <span v-if="preflight" :class="[$style.preflightPill, preflight.ok ? $style.preflightOk : preflight.can_start ? $style.preflightWarn : $style.preflightFail]">
            {{ preflight.ok ? 'All OK' : preflight.can_start ? 'Partial' : 'Failed' }}
          </span>
          <span v-else-if="preflightLoading" :class="[$style.preflightPill, $style.preflightLoading]">Checking...</span>
        </div>
        <div v-if="preflight" :class="$style.preflightChecks">
          <template v-for="(check, name) in preflight.checks" :key="name">
            <div v-if="isOmsCheck(check)" :class="$style.preflightItem" :title="check.error || ''">
              <span :class="[$style.preflightDot, omsCheckOk(check) ? $style.dotOk : $style.dotFail]" />
              <span :class="$style.preflightName">{{ preflightCheckLabel(String(name)) }}</span>
              <span :class="$style.preflightDetail">
                health {{ check.health ? '\u2713' : '\u2717' }}
                <template v-for="(ok, exch) in check.books" :key="exch">
                  &middot; {{ exch }} {{ ok ? '\u2713' : '\u2717' }}
                </template>
                <template v-if="check.feeds"> &middot; {{ check.feeds }} feeds</template>
              </span>
            </div>
            <div v-else :class="$style.preflightItem" :title="check.error || ''">
              <span :class="[$style.preflightDot, exchangeCheckOk(check as PreflightExchangeCheck) ? $style.dotOk : $style.dotFail]" />
              <span :class="$style.preflightName">{{ preflightCheckLabel(String(name)) }}</span>
              <span :class="$style.preflightDetail">
                pos {{ check.positions ? '\u2713' : '\u2717' }}
                &middot; bal {{ check.balance === true ? '\u2713' : check.balance === null ? '-' : '\u2717' }}
                <template v-if="(check as any).signer_ok !== undefined">
                  &middot; signer {{ (check as any).signer_ok ? '\u2713' : '\u2717' }}
                </template>
              </span>
            </div>
          </template>
        </div>
        <div v-if="preflight && !preflight.can_start" :class="$style.preflightWarnBox">
          <Typography size="text-xs" color="error">
            {{ Object.values(preflight.checks).some(c => 'signer_ok' in c && !(c as any).signer_ok)
              ? 'Cannot start: Nado signer was changed externally (e.g. via nado.xyz). Please re-link Nado in Settings.'
              : 'Cannot start: at least 2 exchanges and OMS must be reachable.' }}
          </Typography>
        </div>
      </div>

      <!-- ── Exchanges ── -->
      <div :class="$style.exchangeRow">
        <span
          v-for="ex in status.config.exchanges"
          :key="ex"
          :class="$style.metaPill"
        >{{ displayExchange(ex) }}</span>
      </div>

      <!-- ── Positions table ── -->
      <div :class="$style.sectionTitle">
        <Typography size="text-md" weight="semibold">Open Positions</Typography>
      </div>

      <div v-if="!openPositions.length" :class="$style.emptySmall">
        <Typography size="text-sm" color="tertiary">
          {{ isRunning ? 'Waiting for arbitrage signals...' : 'No open positions' }}
        </Typography>
      </div>

      <div v-else :class="$style.table">
        <div :class="[$style.row, $style.rowHeader]">
          <div :class="[$style.cell, $style.cellId]">
            <Typography size="text-xs" weight="semibold" color="tertiary">ID</Typography>
          </div>
          <div :class="[$style.cell, $style.cellToken]">
            <Typography size="text-xs" weight="semibold" color="tertiary">Token</Typography>
          </div>
          <div :class="[$style.cell, $style.cellExch]">
            <Typography size="text-xs" weight="semibold" color="tertiary">Buy</Typography>
          </div>
          <div :class="[$style.cell, $style.cellExch]">
            <Typography size="text-xs" weight="semibold" color="tertiary">Sell</Typography>
          </div>
          <div :class="[$style.cell, $style.cellPrice]">
            <Typography size="text-xs" weight="semibold" color="tertiary">Buy Fill</Typography>
          </div>
          <div :class="[$style.cell, $style.cellPrice]">
            <Typography size="text-xs" weight="semibold" color="tertiary">Sell Fill</Typography>
          </div>
          <div :class="[$style.cell, $style.cellRight]">
            <Typography size="text-xs" weight="semibold" color="tertiary">Spread %</Typography>
          </div>
          <div :class="[$style.cell, $style.cellRight]">
            <Typography size="text-xs" weight="semibold" color="tertiary">Qty</Typography>
          </div>
          <div :class="[$style.cell, $style.cellRight]">
            <Typography size="text-xs" weight="semibold" color="tertiary">Notional</Typography>
          </div>
          <div :class="[$style.cell, $style.cellAge]">
            <Typography size="text-xs" weight="semibold" color="tertiary">Hold</Typography>
          </div>
          <div :class="[$style.cell, $style.cellAction]">
            <Typography size="text-xs" weight="semibold" color="tertiary">Close</Typography>
          </div>
        </div>

        <div
          v-for="pos in openPositions"
          :key="pos.position_id"
          :class="[$style.row, $style.rowData]"
        >
          <div :class="[$style.cell, $style.cellId]">
            <Typography size="text-xs" color="tertiary">{{ pos.position_id }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellToken]">
            <Typography size="text-md" weight="medium">{{ pos.token }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellExch]">
            <span :class="$style.badge">{{ displayExchange(pos.buy_exchange) }}</span>
          </div>
          <div :class="[$style.cell, $style.cellExch]">
            <span :class="$style.badge">{{ displayExchange(pos.sell_exchange) }}</span>
          </div>
          <div :class="[$style.cell, $style.cellPrice]">
            <Typography size="text-sm" color="secondary">{{ formatPrice(pos.buy_fill_price) }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellPrice]">
            <Typography size="text-sm" color="secondary">{{ formatPrice(pos.sell_fill_price) }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellRight]">
            <Typography size="text-sm" weight="semibold" style="color: #22c55e">
              {{ bpsToPercent(pos.entry_spread_bps) }}%
            </Typography>
          </div>
          <div :class="[$style.cell, $style.cellRight]">
            <Typography size="text-sm" color="secondary">{{ formatQty(pos.quantity) }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellRight]">
            <Typography size="text-sm" weight="medium">{{ formatUsd(pos.notional_usd) }}</Typography>
          </div>
          <div :class="[$style.cell, $style.cellAge]">
            <Typography size="text-xs" :style="{ color: (Date.now() / 1000 - pos.opened_at) >= pos.exit_min_hold_s ? '#22c55e' : 'var(--color-text-tertiary)' }">
              {{ holdDuration(pos.opened_at, pos.exit_min_hold_s) }}
            </Typography>
          </div>
          <div :class="[$style.cell, $style.cellAction]">
            <button
              :class="[$style.btn, $style.btnClosePos]"
              :disabled="actionLoading || pos.status === 'closing'"
              @click="handleClosePosition(pos.position_id, pos.token)"
            >{{ pos.status === 'closing' ? '...' : '✕' }}</button>
          </div>
        </div>
      </div>

      <!-- ── Trade History ── -->
      <div :class="$style.sectionTitle">
        <Typography size="text-md" weight="semibold">Trade History</Typography>
        <Typography size="text-xs" color="tertiary">{{ tradeHistory.length }} trades</Typography>
      </div>

      <div v-if="!tradeHistory.length" :class="$style.emptySmall">
        <Typography size="text-sm" color="tertiary">No completed trades yet</Typography>
      </div>

      <div v-else :class="$style.tradeList">
        <div
          v-for="trade in tradeHistory"
          :key="trade.position_id"
          :class="$style.tradeCard"
        >
          <div :class="$style.tradeHeader">
            <div :class="$style.tradeToken">
              <Typography size="text-md" weight="bold">{{ trade.token }}</Typography>
              <span :class="[$style.tradeBadge, trade.simulation ? $style.tradeBadgeSim : $style.tradeBadgeLive]">
                {{ trade.simulation ? 'SIM' : 'LIVE' }}
              </span>
              <span :class="$style.tradeBadgeReason">{{ trade.close_reason }}</span>
            </div>
            <div :class="$style.tradeMeta">
              <Typography size="text-xs" color="tertiary">{{ formatDate(trade.opened_at) }} → {{ trade.closed_at ? formatDate(trade.closed_at) : '-' }}</Typography>
              <Typography size="text-xs" color="tertiary">held {{ formatDuration(trade.opened_at, trade.closed_at) }}</Typography>
              <button
                :class="[$style.btn, $style.btnDeleteTrade]"
                :disabled="actionLoading"
                @click="handleDeleteTrade(trade.position_id, trade.token)"
                title="Remove from history"
              >✕</button>
            </div>
          </div>

          <div :class="$style.tradeGrid">
            <div :class="$style.tradeLeg">
              <Typography size="text-xs" color="tertiary" weight="semibold">BUY LEG</Typography>
              <div :class="$style.tradeLegRow">
                <span :class="$style.badge">{{ displayExchange(trade.buy_exchange) }}</span>
                <div :class="$style.tradePrices">
                  <div :class="$style.tradePriceEntry">
                    <Typography size="text-xs" color="tertiary">Entry</Typography>
                    <Typography size="text-sm" weight="medium">{{ formatPrice(trade.buy_fill_price) }}</Typography>
                  </div>
                  <div :class="$style.tradePriceArrow">→</div>
                  <div :class="$style.tradePriceExit">
                    <Typography size="text-xs" color="tertiary">Exit</Typography>
                    <Typography size="text-sm" weight="medium">{{ trade.close_sell_fill_price > 0 ? formatPrice(trade.close_sell_fill_price) : '-' }}</Typography>
                  </div>
                </div>
              </div>
            </div>

            <div :class="$style.tradeLeg">
              <Typography size="text-xs" color="tertiary" weight="semibold">SELL LEG</Typography>
              <div :class="$style.tradeLegRow">
                <span :class="$style.badge">{{ displayExchange(trade.sell_exchange) }}</span>
                <div :class="$style.tradePrices">
                  <div :class="$style.tradePriceEntry">
                    <Typography size="text-xs" color="tertiary">Entry</Typography>
                    <Typography size="text-sm" weight="medium">{{ formatPrice(trade.sell_fill_price) }}</Typography>
                  </div>
                  <div :class="$style.tradePriceArrow">→</div>
                  <div :class="$style.tradePriceExit">
                    <Typography size="text-xs" color="tertiary">Exit</Typography>
                    <Typography size="text-sm" weight="medium">{{ trade.close_buy_fill_price > 0 ? formatPrice(trade.close_buy_fill_price) : '-' }}</Typography>
                  </div>
                </div>
              </div>
            </div>

            <div :class="$style.tradeSummary">
              <div :class="$style.tradeSummaryItem">
                <Typography size="text-xs" color="tertiary">Qty</Typography>
                <Typography size="text-sm" weight="medium">{{ formatQty(trade.quantity) }}</Typography>
              </div>
              <div :class="$style.tradeSummaryItem">
                <Typography size="text-xs" color="tertiary">Notional</Typography>
                <Typography size="text-sm" weight="medium">{{ formatUsd(trade.notional_usd) }}</Typography>
              </div>
              <div :class="$style.tradeSummaryItem">
                <Typography size="text-xs" color="tertiary">Entry Spread</Typography>
                <Typography size="text-sm" weight="semibold" style="color: #22c55e">{{ bpsToPercent(trade.entry_spread_bps) }}%</Typography>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- ── Activity Log ── -->
      <div :class="$style.sectionTitle">
        <Typography size="text-md" weight="semibold">Activity Log</Typography>
        <Typography size="text-xs" color="tertiary">{{ activityLog.length }} entries</Typography>
      </div>

      <div v-if="!activityLog.length" :class="$style.emptySmall">
        <Typography size="text-sm" color="tertiary">No activity yet</Typography>
      </div>

      <div v-else ref="logContainer" :class="$style.logBox">
        <div
          v-for="(entry, idx) in activityLog"
          :key="idx"
          :class="$style.logEntry"
        >
          <span :class="$style.logTime">{{ formatTimestamp(entry.timestamp) }}</span>
          <span :class="$style.logBadge" :style="{ color: eventColor(entry.event), borderColor: eventColor(entry.event) }">
            {{ eventLabel(entry.event) }}
          </span>
          <span :class="$style.logMsg">{{ entry.message }}</span>
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
}

.headerLeft {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.headerRight {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.statusPill {
  display: inline-flex;
  padding: 4px 12px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
}

.statusRunning {
  background: rgba(34, 197, 94, 0.12);
  color: #22c55e;
  border: 1px solid rgba(34, 197, 94, 0.3);
}

.statusStopped {
  background: var(--color-white-4);
  color: var(--color-text-tertiary);
  border: 1px solid var(--color-stroke-divider);
}

.simPill {
  display: inline-flex;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  background: rgba(245, 158, 11, 0.12);
  color: #f59e0b;
  border: 1px solid rgba(245, 158, 11, 0.3);
}

/* ── Buttons ── */
.btn {
  height: 36px;
  padding: 0 16px;
  border-radius: var(--radius-md);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: all 0.15s;
  border: 1px solid transparent;
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btnStart {
  background: #22c55e;
  color: #fff;
}
.btnStart:hover:not(:disabled) { background: #16a34a; }

.btnStop {
  background: #ef4444;
  color: #fff;
}
.btnStop:hover:not(:disabled) { background: #dc2626; }

.btnConfig {
  background: var(--color-white-4);
  color: var(--color-text-primary);
  border-color: var(--color-stroke-divider);
}
.btnConfig:hover { border-color: var(--color-text-tertiary); }

.btnReset {
  background: transparent;
  color: #ef4444;
  border-color: #ef4444;
}
.btnReset:hover:not(:disabled) { background: rgba(239, 68, 68, 0.08); }

/* ── Stat cards ── */
.statRow {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
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

.statIconYellow {
  background: rgba(245, 158, 11, 0.12);
  color: #f59e0b;
}

/* ── Pre-Flight Checks ── */
.preflightBar {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: var(--space-3) var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.preflightHeader {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.preflightPill {
  display: inline-flex;
  padding: 2px 10px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
}

.preflightOk {
  background: rgba(34, 197, 94, 0.12);
  color: #22c55e;
  border: 1px solid rgba(34, 197, 94, 0.3);
}

.preflightWarn {
  background: rgba(245, 158, 11, 0.12);
  color: #f59e0b;
  border: 1px solid rgba(245, 158, 11, 0.3);
}

.preflightFail {
  background: rgba(239, 68, 68, 0.12);
  color: #ef4444;
  border: 1px solid rgba(239, 68, 68, 0.3);
}

.preflightLoading {
  background: var(--color-white-4);
  color: var(--color-text-tertiary);
  border: 1px solid var(--color-stroke-divider);
}

.preflightChecks {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-4);
}

.preflightItem {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  cursor: default;
}

.preflightDot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.dotOk {
  background: #22c55e;
  box-shadow: 0 0 4px rgba(34, 197, 94, 0.4);
}

.dotFail {
  background: #ef4444;
  box-shadow: 0 0 4px rgba(239, 68, 68, 0.4);
}

.preflightName {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
}

.preflightDetail {
  font-size: 11px;
  color: var(--color-text-tertiary);
}

.preflightWarnBox {
  padding: var(--space-2) var(--space-3);
  background: rgba(239, 68, 68, 0.06);
  border: 1px solid rgba(239, 68, 68, 0.2);
  border-radius: var(--radius-md);
}

/* ── Exchange pills ── */
.exchangeRow {
  display: flex;
  gap: var(--space-2);
}

.metaPill {
  display: inline-flex;
  padding: 4px 10px;
  border-radius: 999px;
  background: var(--color-white-4);
  border: 1px solid var(--color-stroke-divider);
  font-size: 11px;
  color: var(--color-text-tertiary);
}

/* ── Section titles ── */
.sectionTitle {
  display: flex;
  align-items: baseline;
  gap: var(--space-3);
  padding-top: var(--space-3);
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
  user-select: none;
}

.rowData {
  transition: background 0.1s;
}
.rowData:hover { background: var(--color-white-4); }

.cell { flex: 1; }
.cellId { flex: 0 0 80px; }
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
.cellAge {
  flex: 0 0 80px;
  text-align: right;
}
.cellAction {
  flex: 0 0 50px;
  display: flex;
  justify-content: center;
}

.btnClosePos {
  width: 28px;
  height: 28px;
  padding: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: #ef4444;
  border: 1px solid #ef4444;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
}
.btnClosePos:hover:not(:disabled) { background: rgba(239, 68, 68, 0.12); }
.btnClosePos:disabled { opacity: 0.4; cursor: not-allowed; }

.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  background: var(--color-white-4);
  border: 1px solid var(--color-stroke-divider);
  font-size: 11px;
  color: var(--color-text-secondary);
}

/* ── Trade History ── */
.tradeList {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.tradeCard {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: var(--space-4) var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.tradeHeader {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.tradeToken {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.tradeBadge {
  display: inline-flex;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 700;
}

.tradeBadgeLive {
  background: rgba(34, 197, 94, 0.12);
  color: #22c55e;
  border: 1px solid rgba(34, 197, 94, 0.3);
}

.tradeBadgeSim {
  background: rgba(245, 158, 11, 0.12);
  color: #f59e0b;
  border: 1px solid rgba(245, 158, 11, 0.3);
}

.tradeBadgeReason {
  display: inline-flex;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 600;
  background: var(--color-white-4);
  border: 1px solid var(--color-stroke-divider);
  color: var(--color-text-tertiary);
}

.tradeMeta {
  display: flex;
  gap: var(--space-3);
  align-items: center;
}

.tradeGrid {
  display: grid;
  grid-template-columns: 1fr 1fr auto;
  gap: var(--space-4);
  padding-top: var(--space-2);
  border-top: 1px solid var(--color-stroke-divider);
}

.tradeLeg {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.tradeLegRow {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.tradePrices {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.tradePriceEntry, .tradePriceExit {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 1px;
}

.tradePriceArrow {
  color: var(--color-text-tertiary);
  font-size: 12px;
  padding: 0 2px;
}

.tradeSummary {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  padding-left: var(--space-4);
  border-left: 1px solid var(--color-stroke-divider);
  justify-content: center;
}

.tradeSummaryItem {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 1px;
}

.btnDeleteTrade {
  width: 24px;
  height: 24px;
  padding: 0;
  border-radius: var(--radius-sm);
  background: transparent;
  color: var(--color-text-tertiary);
  border: 1px solid var(--color-stroke-divider);
  font-size: 11px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.15s;
  margin-left: var(--space-2);
}
.btnDeleteTrade:hover:not(:disabled) {
  color: #ef4444;
  border-color: #ef4444;
  background: rgba(239, 68, 68, 0.08);
}
.btnDeleteTrade:disabled { opacity: 0.4; cursor: not-allowed; }

/* ── Activity Log ── */
.logBox {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  max-height: 400px;
  overflow-y: auto;
  padding: var(--space-2) 0;
}

.logEntry {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  padding: var(--space-2) var(--space-5);
  font-size: 13px;
  border-bottom: 1px solid var(--color-stroke-divider);
}
.logEntry:last-child { border-bottom: none; }

.logTime {
  flex: 0 0 80px;
  font-size: 11px;
  color: var(--color-text-tertiary);
  font-variant-numeric: tabular-nums;
}

.logBadge {
  flex: 0 0 90px;
  font-size: 10px;
  font-weight: 700;
  padding: 2px 6px;
  border-radius: 4px;
  border: 1px solid;
  text-align: center;
  background: transparent;
}

.logMsg {
  flex: 1;
  color: var(--color-text-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* ── Config Modal ── */
.configOverlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}

.configModal {
  background: var(--color-bg-primary);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-xl);
  padding: var(--space-6);
  min-width: 400px;
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}

.configGrid {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.configField {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.configLabel {
  font-size: 12px;
  color: var(--color-text-tertiary);
  font-weight: 600;
}

.configInput {
  height: 40px;
  padding: 0 var(--space-4);
  border-radius: var(--radius-md);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  color: var(--color-text-primary);
  font-size: var(--text-sm);
  outline: none;
}
.configInput:focus { border-color: var(--color-text-secondary); }

.configDivider {
  padding-top: var(--space-2);
  border-top: 1px solid var(--color-stroke-divider);
}

.configActions {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-3);
}

/* ── Checkbox group ── */
.checkboxGroup {
  display: flex;
  gap: var(--space-4);
  padding: var(--space-2) 0;
}

.checkboxLabel {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: 13px;
  color: var(--color-text-secondary);
  cursor: pointer;
  user-select: none;
}

.checkbox {
  width: 16px;
  height: 16px;
  accent-color: #22c55e;
  cursor: pointer;
}

/* ── Toggle ── */
.toggleWrap {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.toggle {
  width: 44px;
  height: 24px;
  border-radius: 12px;
  background: var(--color-white-10);
  border: 1px solid var(--color-stroke-divider);
  position: relative;
  cursor: pointer;
  transition: all 0.2s;
  padding: 0;
}

.toggleOn {
  background: #22c55e;
  border-color: #16a34a;
}

.toggleDot {
  position: absolute;
  top: 3px;
  left: 3px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  background: white;
  transition: transform 0.2s;
}

.toggleOn .toggleDot {
  transform: translateX(20px);
}

.toggleLabel {
  font-size: 13px;
  color: var(--color-text-secondary);
}

/* ── Empty / Error ── */
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

.emptySmall {
  padding: var(--space-8) 0;
  text-align: center;
}
</style>
