<script setup lang="ts">
/**
 * Phase F.4 M6 — V2 Persistence Status Dashboard
 *
 * Shows per-user health of the R2-backed state persistence so an admin
 * can spot problems before users notice. Pulls from
 * GET /api/admin/persistence-status which aggregates:
 *   - Analytics Engine `tradeautonom-persistence` (last 24 h flush/restore events)
 *   - R2 bucket `tradeautonom-user-state` (current object sizes)
 *   - D1 user table (backend flag, email)
 */
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { fetchPersistenceStatus } from '@/lib/admin-api'
import type { PersistenceRow, PersistenceStatusResponse } from '@/lib/admin-api'
import Typography from '@/components/ui/Typography.vue'
import Button from '@/components/ui/Button.vue'

const data = ref<PersistenceStatusResponse | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const autoRefresh = ref(false)
let refreshTimer: ReturnType<typeof setInterval> | null = null

// Filters
const filterHealth = ref<'all' | 'green' | 'yellow' | 'red' | 'idle'>('all')
const filterBackend = ref<'all' | 'cf' | 'photon'>('cf')

const filteredRows = computed<PersistenceRow[]>(() => {
  if (!data.value) return []
  return data.value.rows.filter((r) => {
    if (filterHealth.value !== 'all' && r.health !== filterHealth.value) return false
    if (filterBackend.value !== 'all' && r.backend !== filterBackend.value) return false
    return true
  })
})

async function load() {
  loading.value = true
  error.value = null
  try {
    data.value = await fetchPersistenceStatus()
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'Failed to load persistence status'
  } finally {
    loading.value = false
  }
}

function toggleAuto() {
  autoRefresh.value = !autoRefresh.value
  if (autoRefresh.value) {
    refreshTimer = setInterval(load, 30000)
  } else if (refreshTimer) {
    clearInterval(refreshTimer)
    refreshTimer = null
  }
}

onMounted(load)
onUnmounted(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})

function healthColor(h: PersistenceRow['health']): string {
  return h === 'green'
    ? '#22c55e'
    : h === 'yellow'
      ? '#eab308'
      : h === 'red'
        ? '#ef4444'
        : 'var(--color-text-tertiary)'
}

function healthLabel(h: PersistenceRow['health']): string {
  return { green: 'OK', yellow: 'Warn', red: 'Critical', idle: 'V1 only' }[h]
}

function fmtAge(seconds: number | null): string {
  if (seconds === null || seconds < 0) return '—'
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`
}

function fmtBytes(b: number | null): string {
  if (b === null) return '—'
  if (b < 1024) return `${b} B`
  if (b < 1024 * 1024) return `${(b / 1024).toFixed(1)} KB`
  return `${(b / 1024 / 1024).toFixed(2)} MB`
}

function fmtTs(iso: string | null): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleTimeString('de-DE')
  } catch {
    return iso
  }
}
</script>

<template>
  <div :class="$style.page">
    <div :class="$style.header">
      <div>
        <Typography size="text-h5" weight="bold">V2 Persistence Status</Typography>
        <Typography size="text-xs" color="tertiary">
          Per-user health of R2-backed state on Cloudflare. Snapshot of last 24 h of flush/restore events.
        </Typography>
      </div>
      <div :class="$style.headerActions">
        <Button variant="outline" size="sm" :loading="loading" @click="load">Refresh</Button>
        <Button variant="ghost" size="sm" @click="toggleAuto">
          {{ autoRefresh ? '⏸ Stop auto-refresh' : '▶ Auto (30s)' }}
        </Button>
      </div>
    </div>

    <div v-if="error" :class="$style.error">
      <Typography size="text-sm" color="error">{{ error }}</Typography>
    </div>

    <div v-if="data" :class="$style.summary">
      <div :class="$style.summaryCard" :style="{ borderColor: '#22c55e' }">
        <Typography size="text-h5" weight="bold" :style="{ color: '#22c55e' }">{{ data.summary.green }}</Typography>
        <Typography size="text-xs" color="tertiary">Green (OK)</Typography>
      </div>
      <div :class="$style.summaryCard" :style="{ borderColor: '#eab308' }">
        <Typography size="text-h5" weight="bold" :style="{ color: '#eab308' }">{{ data.summary.yellow }}</Typography>
        <Typography size="text-xs" color="tertiary">Yellow (Warn)</Typography>
      </div>
      <div :class="$style.summaryCard" :style="{ borderColor: '#ef4444' }">
        <Typography size="text-h5" weight="bold" :style="{ color: '#ef4444' }">{{ data.summary.red }}</Typography>
        <Typography size="text-xs" color="tertiary">Red (Critical)</Typography>
      </div>
      <div :class="$style.summaryCard">
        <Typography size="text-h5" weight="bold">{{ data.summary.on_v2 }}</Typography>
        <Typography size="text-xs" color="tertiary">On V2 / {{ data.summary.total_users }} total</Typography>
      </div>
    </div>

    <div :class="$style.filters">
      <label :class="$style.filterField">
        <Typography size="text-xs" color="tertiary">Health</Typography>
        <select v-model="filterHealth" :class="$style.select">
          <option value="all">All</option>
          <option value="green">Green</option>
          <option value="yellow">Yellow</option>
          <option value="red">Red</option>
          <option value="idle">Idle (V1)</option>
        </select>
      </label>
      <label :class="$style.filterField">
        <Typography size="text-xs" color="tertiary">Backend</Typography>
        <select v-model="filterBackend" :class="$style.select">
          <option value="all">All</option>
          <option value="cf">V2 (CF)</option>
          <option value="photon">V1 (Photon)</option>
        </select>
      </label>
      <Typography size="text-xs" color="tertiary">
        Showing {{ filteredRows.length }} of {{ data?.rows.length || 0 }} users
        <span v-if="data"> · generated at {{ fmtTs(data.generated_at) }}</span>
      </Typography>
    </div>

    <div :class="$style.tableWrap">
      <table :class="$style.table">
        <thead>
          <tr>
            <th>Health</th>
            <th>User</th>
            <th>Backend</th>
            <th>R2 size</th>
            <th>R2 age</th>
            <th>Last flush</th>
            <th>Flushes 24h</th>
            <th>Errors 24h</th>
            <th>Reason</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in filteredRows" :key="r.user_id">
            <td>
              <span :class="$style.healthBadge" :style="{ borderColor: healthColor(r.health), color: healthColor(r.health) }">
                {{ healthLabel(r.health) }}
              </span>
            </td>
            <td>
              <Typography size="text-sm">{{ r.email }}</Typography>
              <Typography size="text-xs" color="tertiary">{{ r.user_id.slice(0, 12) }}…</Typography>
            </td>
            <td>
              <Typography size="text-xs" :color="r.backend === 'cf' ? 'primary' : 'tertiary'">
                {{ r.backend === 'cf' ? 'V2 (CF)' : 'V1 (Photon)' }}
              </Typography>
            </td>
            <td>{{ fmtBytes(r.r2_size_bytes) }}</td>
            <td>{{ fmtAge(r.r2_age_s) }}</td>
            <td>
              <Typography size="text-xs">{{ fmtTs(r.last_flush_ts) }}</Typography>
              <Typography v-if="r.last_flush_status && r.last_flush_status !== 'ok'" size="text-xs" color="error">
                {{ r.last_flush_status }}
              </Typography>
            </td>
            <td>{{ r.flushes_24h }}</td>
            <td>
              <Typography size="text-sm" :color="r.flush_errors_24h > 0 ? 'error' : 'tertiary'">
                {{ r.flush_errors_24h }}
              </Typography>
            </td>
            <td>
              <Typography size="text-xs" color="tertiary">{{ r.health_reason }}</Typography>
            </td>
          </tr>
          <tr v-if="!loading && filteredRows.length === 0">
            <td colspan="9" style="text-align:center;padding:24px">
              <Typography size="text-sm" color="tertiary">No users match the filter.</Typography>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style module>
.page {
  padding: 30px 40px;
  max-width: 1300px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 16px;
}

.headerActions {
  display: flex;
  gap: 8px;
}

.error {
  padding: 12px;
  border-radius: 6px;
  background: rgba(239, 68, 68, 0.05);
  border: 1px solid rgba(239, 68, 68, 0.3);
}

.summary {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
}

.summaryCard {
  border: 1px solid var(--color-border, rgba(255,255,255,0.08));
  border-radius: 8px;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.filters {
  display: flex;
  gap: 16px;
  align-items: flex-end;
  flex-wrap: wrap;
}

.filterField {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.select {
  padding: 6px 8px;
  border-radius: 6px;
  border: 1px solid var(--color-border, rgba(255,255,255,0.1));
  background: var(--color-bg-elevated, rgba(255,255,255,0.05));
  color: inherit;
  font-size: 13px;
  min-width: 140px;
}

.tableWrap {
  border: 1px solid var(--color-border, rgba(255,255,255,0.08));
  border-radius: 8px;
  overflow-x: auto;
}

.table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.table th,
.table td {
  padding: 8px 12px;
  text-align: left;
  border-bottom: 1px solid var(--color-border, rgba(255,255,255,0.06));
}

.table th {
  font-weight: 600;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--color-text-tertiary, #888);
  background: var(--color-bg-subtle, rgba(255,255,255,0.02));
}

.table tbody tr:hover {
  background: rgba(255,255,255,0.02);
}

.healthBadge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 12px;
  border: 1px solid currentColor;
  font-size: 11px;
  font-weight: 600;
  white-space: nowrap;
}
</style>
