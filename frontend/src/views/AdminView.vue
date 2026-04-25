<script setup lang="ts">
import { ref, onMounted } from 'vue'
import {
  fetchAdminUsers,
  deleteAdminUser,
  setUserBackend,
  migrateUserToCf,
  migrateUserToPhoton,
} from '@/lib/admin-api'
import type { AdminUser, MigrateResult } from '@/lib/admin-api'
import Typography from '@/components/ui/Typography.vue'
import Button from '@/components/ui/Button.vue'

const users = ref<AdminUser[]>([])
const loading = ref(true)
const error = ref<string | null>(null)
const deletingId = ref<string | null>(null)
const flippingId = ref<string | null>(null)
const migratingId = ref<string | null>(null)
const migrationResult = ref<MigrateResult | null>(null)

onMounted(loadUsers)

async function loadUsers() {
  loading.value = true
  error.value = null
  try {
    users.value = await fetchAdminUsers()
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'Failed to load users'
  } finally {
    loading.value = false
  }
}

async function handleDelete(user: AdminUser) {
  if (!confirm(`Delete user "${user.name}" (${user.email})?\n\nThis will remove their container and all data.`)) return
  deletingId.value = user.id
  try {
    await deleteAdminUser(user.id)
    users.value = users.value.filter((u: AdminUser) => u.id !== user.id)
  } catch (e: unknown) {
    alert(e instanceof Error ? e.message : 'Delete failed')
  } finally {
    deletingId.value = null
  }
}

async function handleFlipBackend(user: AdminUser) {
  const current = user.backend ?? 'photon'
  const next = current === 'cf' ? 'photon' : 'cf'
  const label = next === 'cf' ? 'V2 (Cloudflare)' : 'V1 (Photon)'
  const warn =
    next === 'cf'
      ? `Move "${user.email}" to ${label}?\n\n` +
        `Warning: V2 persistence (R2) is not yet activated. Until then the V2 container will lose state on any recycle.\n\n` +
        `The user should have all bots in IDLE state before flipping. The server checks this.\n\n` +
        `Continue?`
      : `Move "${user.email}" back to ${label}?\n\n` +
        `The V1 container state is expected to still exist on Photon.\n\nContinue?`
  if (!confirm(warn)) return

  flippingId.value = user.id
  try {
    await trySetBackend(user, next, false)
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e)
    // If the pre-flight rejected (bots not idle), offer to force.
    if (msg.toLowerCase().includes('bots not idle') || msg.toLowerCase().includes('not idle')) {
      const forceOk = confirm(`${msg}\n\nForce the flip anyway? (Only do this if you know the bots will be migrated / stopped separately.)`)
      if (forceOk) {
        try {
          await trySetBackend(user, next, true)
        } catch (e2: unknown) {
          alert(e2 instanceof Error ? e2.message : String(e2))
        }
      }
    } else {
      alert(msg)
    }
  } finally {
    flippingId.value = null
  }
}

async function trySetBackend(user: AdminUser, next: 'photon' | 'cf', force: boolean) {
  const res = await setUserBackend(user.id, next, force)
  // Update the row in place.
  const idx = users.value.findIndex((u) => u.id === user.id)
  if (idx >= 0) {
    users.value[idx] = { ...users.value[idx], backend: res.backend as 'photon' | 'cf' }
  }
}

/**
 * Phase F.4 M5: full migration including state copy. Unlike the bare
 * "Flip" button, this orchestrates: state export from old backend,
 * upload to new backend, verification, and only then the D1 flip. If
 * any step fails, D1 stays unchanged.
 */
async function handleMigrate(user: AdminUser) {
  const current = user.backend ?? 'photon'
  const direction = current === 'cf' ? 'V1 (Photon)' : 'V2 (Cloudflare)'
  const warn =
    current === 'cf'
      ? `Roll back "${user.email}" from V2 (CF) to ${direction}?\n\n` +
        `Steps the server will run:\n` +
        `  1. Verify all V2 bots are IDLE/HOLDING\n` +
        `  2. Force-flush V2 state to R2\n` +
        `  3. Download R2 tarball into the Photon container\n` +
        `  4. Restart Photon container with restored state\n` +
        `  5. Flip user.backend back to 'photon' in D1\n\n` +
        `Photon container '${user.container_name || 'ta-' + user.id}' must already exist.\n\n` +
        `Continue?`
      : `Migrate "${user.email}" from V1 (Photon) to ${direction}?\n\n` +
        `Steps the server will run:\n` +
        `  1. Verify all Photon bots are IDLE/HOLDING\n` +
        `  2. Tar /app/data/ from the Photon container\n` +
        `  3. Upload tarball to R2 via the user-v2 Worker\n` +
        `  4. Verify R2 round-trip\n` +
        `  5. Flip user.backend to 'cf' in D1\n` +
        `  6. Stop Photon container, recycle CF container\n\n` +
        `Continue?`
  if (!confirm(warn)) return

  migratingId.value = user.id
  migrationResult.value = null
  try {
    await tryMigrate(user, current === 'cf' ? 'photon' : 'cf', false)
  } catch (e: unknown) {
    const msg = e instanceof Error ? e.message : String(e)
    if (msg.toLowerCase().includes('not idle')) {
      const forceOk = confirm(`${msg}\n\nForce the migration anyway? This will copy state mid-execution and may leave bots in an inconsistent state.`)
      if (forceOk) {
        try {
          await tryMigrate(user, current === 'cf' ? 'photon' : 'cf', true)
        } catch (e2: unknown) {
          alert(e2 instanceof Error ? e2.message : String(e2))
        }
      }
    } else {
      alert(msg)
    }
  } finally {
    migratingId.value = null
  }
}

async function tryMigrate(user: AdminUser, target: 'photon' | 'cf', force: boolean) {
  const res = target === 'cf'
    ? await migrateUserToCf(user.id, force)
    : await migrateUserToPhoton(user.id, force)
  migrationResult.value = res
  // Reflect new backend in the row
  const idx = users.value.findIndex((u) => u.id === user.id)
  if (idx >= 0) {
    users.value[idx] = { ...users.value[idx], backend: res.backend }
  }
}

function formatDate(iso: string): string {
  if (!iso) return '—'
  try {
    const d = new Date(iso)
    return d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric' })
      + ' ' + d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })
  } catch {
    return iso
  }
}

function statusColor(status: string | null): string {
  if (status === 'running') return 'var(--color-success)'
  if (status === 'stopped') return 'var(--color-text-tertiary)'
  if (status === 'crash_loop') return 'var(--color-error)'
  return 'var(--color-text-secondary)'
}

function backendColor(backend: string | null): string {
  // V2 (cf) = distinct accent color so ops can spot migrated users at a glance.
  if (backend === 'cf') return 'var(--color-primary, #7c3aed)'
  return 'var(--color-text-secondary)'
}
</script>

<template>
  <div :class="$style.page">
    <div :class="$style.header">
      <Typography size="text-h5" weight="bold">Admin — Users</Typography>
      <Button variant="outline" size="sm" @click="loadUsers" :loading="loading">Refresh</Button>
    </div>

    <div v-if="error" :class="$style.error">
      <Typography size="text-sm" color="error">{{ error }}</Typography>
    </div>

    <div v-if="loading && !users.length" :class="$style.empty">
      <Typography size="text-sm" color="secondary">Loading users...</Typography>
    </div>

    <div v-else-if="!users.length" :class="$style.empty">
      <Typography size="text-sm" color="tertiary">No users found.</Typography>
    </div>

    <div v-else :class="$style.tableWrap">
      <table :class="$style.table">
        <thead>
          <tr>
            <th>Name</th>
            <th>Email</th>
            <th>Backend</th>
            <th>Container</th>
            <th>Port</th>
            <th>Status</th>
            <th>Created</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="u in users" :key="u.id">
            <td>
              <Typography size="text-sm" weight="medium">{{ u.name }}</Typography>
            </td>
            <td>
              <Typography size="text-sm" color="secondary">{{ u.email }}</Typography>
            </td>
            <td>
              <div :class="$style.backendCell">
                <span
                  :class="$style.backendBadge"
                  :style="{ color: backendColor(u.backend), borderColor: backendColor(u.backend) }"
                >
                  {{ (u.backend ?? 'photon') === 'cf' ? 'V2 (CF)' : 'V1 (Photon)' }}
                </span>
                <Button
                  variant="solid"
                  color="success"
                  size="sm"
                  :loading="migratingId === u.id"
                  :disabled="!!migratingId || !!flippingId"
                  @click="handleMigrate(u)"
                  :title="(u.backend ?? 'photon') === 'cf' ? 'Roll back to V1 with state copy' : 'Migrate to V2 with state copy'"
                >
                  {{ (u.backend ?? 'photon') === 'cf' ? '↩ Migrate to V1' : 'Migrate to V2 →' }}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  :loading="flippingId === u.id"
                  :disabled="!!migratingId || !!flippingId"
                  @click="handleFlipBackend(u)"
                  title="Bare flip without copying state — for emergency only"
                >
                  Flip (no copy)
                </Button>
              </div>
            </td>
            <td>
              <Typography size="text-xs" color="tertiary">
                {{ u.container_name || '—' }}
              </Typography>
            </td>
            <td>
              <Typography size="text-sm">{{ u.port || '—' }}</Typography>
            </td>
            <td>
              <span
                :class="$style.statusBadge"
                :style="{ color: statusColor(u.container_status), borderColor: statusColor(u.container_status) }"
              >
                {{ u.container_status || 'none' }}
              </span>
            </td>
            <td>
              <Typography size="text-xs" color="tertiary">{{ formatDate(u.createdAt) }}</Typography>
            </td>
            <td>
              <Button
                variant="ghost"
                size="sm"
                color="error"
                :loading="deletingId === u.id"
                @click="handleDelete(u)"
              >Delete</Button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="migrationResult" :class="$style.migrateResult">
      <div :class="$style.migrateHeader">
        <Typography size="text-sm" weight="semibold" color="success">
          ✓ Migration complete: {{ migrationResult.email }} → {{ migrationResult.backend === 'cf' ? 'V2 (CF)' : 'V1 (Photon)' }}
        </Typography>
        <Button variant="ghost" size="sm" @click="migrationResult = null">×</Button>
      </div>
      <div :class="$style.migrateMeta">
        <span><strong>Tar size:</strong> {{ migrationResult.tar_bytes }} bytes</span>
        <span v-if="migrationResult.r2_verify_bytes != null"><strong>R2 verify:</strong> {{ migrationResult.r2_verify_bytes }} bytes</span>
        <span v-if="migrationResult.photon_stopped !== undefined"><strong>Photon stopped:</strong> {{ migrationResult.photon_stopped ? 'yes' : 'no' }}</span>
        <span v-if="migrationResult.photon_started !== undefined"><strong>Photon started:</strong> {{ migrationResult.photon_started ? 'yes' : 'no' }}</span>
        <span v-if="migrationResult.cf_recycled !== undefined"><strong>CF recycled:</strong> {{ migrationResult.cf_recycled ? 'yes' : 'no' }}</span>
        <span v-if="migrationResult.forced"><strong style="color:var(--color-warning);">forced</strong></span>
      </div>
      <details :class="$style.migrateTrace">
        <summary>Step-by-step trace ({{ migrationResult.trace.length }} steps)</summary>
        <ol>
          <li v-for="(line, i) in migrationResult.trace" :key="i">{{ line }}</li>
        </ol>
      </details>
    </div>

    <div :class="$style.footer">
      <Typography size="text-xs" color="tertiary">{{ users.length }} user(s) total</Typography>
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
  gap: var(--space-5);
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
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
  overflow: hidden;
}

.table {
  width: 100%;
  border-collapse: collapse;
}

.table th {
  text-align: left;
  padding: var(--space-3) var(--space-4);
  background: var(--color-white-4);
  border-bottom: 1px solid var(--color-stroke-divider);
  font-size: var(--text-xs);
  color: var(--color-text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  font-weight: 500;
}

.table td {
  padding: var(--space-3) var(--space-4);
  border-bottom: 1px solid var(--color-stroke-divider);
  vertical-align: middle;
}

.table tbody tr:last-child td {
  border-bottom: none;
}

.table tbody tr:hover {
  background: var(--color-white-4);
}

.statusBadge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  border: 1px solid;
  font-size: var(--text-xs);
  font-weight: 500;
}

.backendBadge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  border: 1px solid;
  font-size: var(--text-xs);
  font-weight: 600;
  white-space: nowrap;
}

.backendCell {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}

.migrateResult {
  border: 1px solid var(--color-success, #22c55e);
  border-radius: 8px;
  padding: 16px;
  background: var(--color-success-bg, rgba(34, 197, 94, 0.05));
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.migrateHeader {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.migrateMeta {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.migrateTrace {
  font-family: ui-monospace, monospace;
  font-size: 11px;
  color: var(--color-text-secondary);
}

.migrateTrace summary {
  cursor: pointer;
  user-select: none;
}

.migrateTrace ol {
  margin: 8px 0 0 24px;
  padding: 0;
}

.migrateTrace li {
  margin: 2px 0;
  list-style: decimal;
}

.footer {
  text-align: right;
}

@media (max-width: 900px) {
  .page {
    padding: 24px 16px;
  }
  .tableWrap {
    overflow-x: auto;
  }
}
</style>
