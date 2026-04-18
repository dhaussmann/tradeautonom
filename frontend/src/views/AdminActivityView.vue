<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { fetchActivityLogs } from '@/lib/admin-api'
import type { ActivityLogEntry, ActivityLogFilters } from '@/lib/admin-api'
import Typography from '@/components/ui/Typography.vue'
import Button from '@/components/ui/Button.vue'

const rows = ref<ActivityLogEntry[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const autoRefresh = ref(false)
let refreshTimer: ReturnType<typeof setInterval> | null = null

// Filters
const filterContainer = ref('')
const filterBotType = ref('')
const filterEvent = ref('')
const filterSearch = ref('')
const filterFrom = ref('')
const filterTo = ref('')
const filterLimit = ref(200)

// Unique values for dropdowns (derived from results)
const containers = computed(() => [...new Set(rows.value.map(r => r.container).filter(Boolean))].sort())
const botTypes = computed(() => [...new Set(rows.value.map(r => r.bot_type).filter(Boolean))].sort())
const events = computed(() => [...new Set(rows.value.map(r => r.event).filter(Boolean))].sort())

async function loadLogs() {
  loading.value = true
  error.value = null
  try {
    const filters: ActivityLogFilters = { limit: filterLimit.value }
    if (filterContainer.value) filters.container = filterContainer.value
    if (filterBotType.value) filters.bot_type = filterBotType.value
    if (filterEvent.value) filters.event = filterEvent.value
    if (filterSearch.value) filters.search = filterSearch.value
    if (filterFrom.value) filters.from = filterFrom.value
    if (filterTo.value) filters.to = filterTo.value
    const data = await fetchActivityLogs(filters)
    rows.value = data.rows || []
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'Failed to load activity logs'
  } finally {
    loading.value = false
  }
}

function clearFilters() {
  filterContainer.value = ''
  filterBotType.value = ''
  filterEvent.value = ''
  filterSearch.value = ''
  filterFrom.value = ''
  filterTo.value = ''
  loadLogs()
}

function toggleAutoRefresh() {
  autoRefresh.value = !autoRefresh.value
  if (autoRefresh.value) {
    refreshTimer = setInterval(loadLogs, 10000)
  } else if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
}

function formatTimestamp(ts: number, dt?: string): string {
  // Prefer epoch timestamp if available
  if (ts && ts > 0) {
    try {
      const d = new Date(ts * 1000)
      return d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric' })
        + ' ' + d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    } catch {
      // fall through
    }
  }
  // Fallback to datetime string from Analytics Engine
  if (dt) {
    try {
      const d = new Date(dt + 'Z')
      return d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric' })
        + ' ' + d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
    } catch {
      return dt
    }
  }
  return '—'
}

function eventColor(evt: string): string {
  if (evt.includes('failed') || evt.includes('error') || evt.includes('mismatch')) return 'var(--color-error)'
  if (evt.includes('opened') || evt.includes('started') || evt.includes('connected')) return 'var(--color-success)'
  if (evt.includes('closed') || evt.includes('stopped')) return 'var(--color-text-tertiary)'
  if (evt.includes('signal') || evt.includes('closing')) return 'var(--color-warning, #e6a700)'
  return 'var(--color-text-secondary)'
}

function botTypeLabel(bt: string): string {
  if (bt === 'dna') return 'DNA'
  if (bt === 'funding_arb') return 'Funding'
  return bt
}

onMounted(loadLogs)
onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<template>
  <div :class="$style.page">
    <div :class="$style.header">
      <Typography size="text-h5" weight="bold">Admin — Activity Log</Typography>
      <div :class="$style.headerActions">
        <Button
          variant="outline"
          size="sm"
          @click="toggleAutoRefresh"
          :class="{ [$style.activeToggle]: autoRefresh }"
        >{{ autoRefresh ? 'Auto ●' : 'Auto ○' }}</Button>
        <Button variant="outline" size="sm" @click="loadLogs" :loading="loading">Refresh</Button>
      </div>
    </div>

    <!-- Filter bar -->
    <div :class="$style.filters">
      <div :class="$style.filterGroup">
        <label :class="$style.filterLabel">Search</label>
        <input
          v-model="filterSearch"
          :class="$style.filterInput"
          type="text"
          placeholder="Search messages..."
          @keydown.enter="loadLogs"
        />
      </div>
      <div :class="$style.filterGroup">
        <label :class="$style.filterLabel">Container</label>
        <select v-model="filterContainer" :class="$style.filterSelect">
          <option value="">All</option>
          <option v-for="c in containers" :key="c" :value="c">{{ c }}</option>
        </select>
      </div>
      <div :class="$style.filterGroup">
        <label :class="$style.filterLabel">Bot Type</label>
        <select v-model="filterBotType" :class="$style.filterSelect">
          <option value="">All</option>
          <option v-for="bt in botTypes" :key="bt" :value="bt">{{ botTypeLabel(bt) }}</option>
        </select>
      </div>
      <div :class="$style.filterGroup">
        <label :class="$style.filterLabel">Event</label>
        <select v-model="filterEvent" :class="$style.filterSelect">
          <option value="">All</option>
          <option v-for="ev in events" :key="ev" :value="ev">{{ ev }}</option>
        </select>
      </div>
      <div :class="$style.filterGroup">
        <label :class="$style.filterLabel">From</label>
        <input v-model="filterFrom" :class="$style.filterInput" type="datetime-local" />
      </div>
      <div :class="$style.filterGroup">
        <label :class="$style.filterLabel">To</label>
        <input v-model="filterTo" :class="$style.filterInput" type="datetime-local" />
      </div>
      <div :class="$style.filterActions">
        <Button variant="solid" size="sm" @click="loadLogs" :loading="loading">Apply</Button>
        <Button variant="ghost" size="sm" @click="clearFilters">Clear</Button>
      </div>
    </div>

    <div v-if="error" :class="$style.error">
      <Typography size="text-sm" color="error">{{ error }}</Typography>
    </div>

    <div v-if="loading && !rows.length" :class="$style.empty">
      <Typography size="text-sm" color="secondary">Loading activity logs...</Typography>
    </div>

    <div v-else-if="!rows.length" :class="$style.empty">
      <Typography size="text-sm" color="tertiary">No activity logs found.</Typography>
    </div>

    <div v-else :class="$style.tableWrap">
      <table :class="$style.table">
        <thead>
          <tr>
            <th>Time</th>
            <th>Container</th>
            <th>Port</th>
            <th>Type</th>
            <th>Bot</th>
            <th>Event</th>
            <th>Message</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, i) in rows" :key="i">
            <td :class="$style.noWrap">
              <Typography size="text-xs" color="tertiary">{{ formatTimestamp(row.timestamp, row.datetime) }}</Typography>
            </td>
            <td>
              <Typography size="text-xs" color="secondary">{{ row.container }}</Typography>
            </td>
            <td>
              <Typography size="text-xs">{{ row.port }}</Typography>
            </td>
            <td>
              <span :class="$style.chip">{{ botTypeLabel(row.bot_type) }}</span>
            </td>
            <td>
              <Typography size="text-xs" color="secondary">{{ row.bot_id }}</Typography>
            </td>
            <td>
              <span :class="$style.eventBadge" :style="{ color: eventColor(row.event), borderColor: eventColor(row.event) }">
                {{ row.event }}
              </span>
            </td>
            <td :class="$style.msgCell">
              <Typography size="text-xs">{{ row.message }}</Typography>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div :class="$style.footer">
      <Typography size="text-xs" color="tertiary">{{ rows.length }} entries{{ autoRefresh ? ' (auto-refreshing)' : '' }}</Typography>
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
  gap: var(--space-4);
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.headerActions {
  display: flex;
  gap: var(--space-2);
}

.activeToggle {
  background: var(--color-success) !important;
  color: white !important;
  border-color: var(--color-success) !important;
}

.filters {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-3);
  align-items: flex-end;
  padding: var(--space-4);
  background: var(--color-white-2);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-xl);
}

.filterGroup {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 140px;
}

.filterLabel {
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  font-weight: 500;
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.filterInput, .filterSelect {
  padding: 6px 10px;
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-md);
  font-size: var(--text-sm);
  background: var(--color-bg-secondary);
  color: var(--color-text-primary);
  outline: none;
  color-scheme: dark;
}

.filterSelect option {
  background: var(--color-bg-secondary);
  color: var(--color-text-primary);
}

.filterInput::placeholder {
  color: var(--color-text-tertiary);
}

.filterInput:focus, .filterSelect:focus {
  border-color: var(--color-brand);
}

.filterActions {
  display: flex;
  gap: var(--space-2);
  align-items: flex-end;
}

.error {
  padding: var(--space-3) var(--space-4);
  background: var(--color-error-bg);
  border: 1px solid var(--color-error-stroke);
  border-radius: var(--radius-md);
}

.empty {
  padding: var(--space-10);
  text-align: center;
}

.tableWrap {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  overflow-x: auto;
  max-height: 70vh;
  overflow-y: auto;
}

.table {
  width: 100%;
  border-collapse: collapse;
}

.table th {
  text-align: left;
  padding: var(--space-2) var(--space-3);
  background: var(--color-white-4);
  border-bottom: 1px solid var(--color-stroke-divider);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-weight: 500;
  position: sticky;
  top: 0;
  z-index: 1;
}

.table td {
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-stroke-divider);
  vertical-align: top;
}

.table tbody tr:last-child td {
  border-bottom: none;
}

.table tbody tr:hover {
  background: var(--color-white-4);
}

.noWrap {
  white-space: nowrap;
}

.chip {
  display: inline-block;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  background: var(--color-white-4);
  font-size: var(--text-xs);
  font-weight: 500;
  color: var(--color-text-secondary);
}

.eventBadge {
  display: inline-block;
  padding: 1px 6px;
  border-radius: var(--radius-sm);
  border: 1px solid;
  font-size: var(--text-xs);
  font-weight: 500;
  white-space: nowrap;
}

.msgCell {
  max-width: 500px;
  word-break: break-word;
}

.footer {
  text-align: right;
}

@media (max-width: 900px) {
  .page {
    padding: 24px 16px;
  }
  .filters {
    flex-direction: column;
  }
  .filterGroup {
    min-width: 100%;
  }
}
</style>
