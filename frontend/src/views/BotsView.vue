<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useBotsStore } from '@/stores/bots'
import { fetchBotStatus } from '@/lib/api'
import Typography from '@/components/ui/Typography.vue'
import Button from '@/components/ui/Button.vue'
import Chip from '@/components/ui/Chip.vue'
import BotCreateModal from '@/components/dashboard/BotCreateModal.vue'
import type { BotSummary, BotStatus, BotCreateRequest } from '@/types/bot'

const router = useRouter()
const botsStore = useBotsStore()

const showCreateModal = ref(false)
const actionLoading = ref<Record<string, string | null>>({})
const botDetails = ref<Record<string, BotStatus>>({})
let pollInterval: ReturnType<typeof setInterval> | null = null

// ── Computed ──────────────────────────────────────────

const activeBots = computed(() =>
  botsStore.bots.filter((b: BotSummary) => b.state !== 'IDLE').length
)

const idleBots = computed(() =>
  botsStore.bots.filter((b: BotSummary) => b.state === 'IDLE').length
)

const enteringBots = computed(() =>
  botsStore.bots.filter((b: BotSummary) => b.state === 'ENTERING').length
)

const holdingBots = computed(() =>
  botsStore.bots.filter((b: BotSummary) => b.state === 'HOLDING').length
)

const exitingBots = computed(() =>
  botsStore.bots.filter((b: BotSummary) => b.state === 'EXITING').length
)

// ── Formatters ────────────────────────────────────────

function stateVariant(state: string): 'success' | 'warning' | 'error' | 'neutral' | 'info' | 'brand' {
  switch (state) {
    case 'HOLDING': return 'success'
    case 'ENTERING': return 'brand'
    case 'EXITING': return 'warning'
    case 'PAUSED_ENTERING':
    case 'PAUSED_EXITING': return 'info'
    default: return 'neutral'
  }
}

function stateIcon(state: string): string {
  switch (state) {
    case 'HOLDING': return '✓'
    case 'ENTERING': return '→'
    case 'EXITING': return '←'
    case 'PAUSED_ENTERING':
    case 'PAUSED_EXITING': return '⏸'
    case 'IDLE': return '○'
    default: return '○'
  }
}

function formatPrice(price: number | undefined): string {
  if (price === undefined || price === null || price === 0) return '—'
  if (price >= 1000) return price.toFixed(2)
  if (price >= 1) return price.toFixed(4)
  return price.toFixed(6)
}

function formatPnl(pnl: number | undefined): string {
  if (pnl === undefined || pnl === null) return '—'
  const prefix = pnl >= 0 ? '+' : ''
  return `${prefix}$${pnl.toFixed(2)}`
}

// ── Actions ───────────────────────────────────────────

async function handleStart(botId: string) {
  actionLoading.value[botId] = 'start'
  try { await botsStore.start(botId) }
  catch { /* error handled in store */ }
  finally { actionLoading.value[botId] = null }
}

async function handleStop(botId: string) {
  actionLoading.value[botId] = 'stop'
  try { await botsStore.stop(botId) }
  catch { /* error handled in store */ }
  finally { actionLoading.value[botId] = null }
}

async function handleKill(botId: string) {
  actionLoading.value[botId] = 'kill'
  try { await botsStore.kill(botId) }
  catch { /* error handled in store */ }
  finally { actionLoading.value[botId] = null }
}

async function handleDelete(botId: string) {
  if (!confirm(`Delete bot "${botId}"?`)) return
  actionLoading.value[botId] = 'delete'
  try { await botsStore.remove(botId) }
  catch { /* error handled in store */ }
  finally { actionLoading.value[botId] = null }
}

async function handleCreate(req: BotCreateRequest) {
  try {
    await botsStore.create(req)
    showCreateModal.value = false
  } catch {
    /* error shown in modal */
  }
}

function navigateToBot(botId: string) {
  router.push({ name: 'bot-detail', params: { botId } })
}

async function loadBotDetails() {
  // Load detailed status for each active bot (non-IDLE)
  const activeBotIds = botsStore.bots
    .filter(b => b.state !== 'IDLE' && b.state !== 'PAUSED_ENTERING' && b.state !== 'PAUSED_EXITING')
    .map(b => b.bot_id)
  
  await Promise.allSettled(
    activeBotIds.map(async (botId) => {
      try {
        const status = await fetchBotStatus(botId)
        botDetails.value[botId] = status
      } catch {
        // Silently fail - bot details will show as unavailable
      }
    })
  )
}

// ── Lifecycle ─────────────────────────────────────────

onMounted(async () => {
  await botsStore.load()
  await loadBotDetails()
  
  pollInterval = setInterval(async () => {
    await botsStore.load()
    await loadBotDetails()
  }, 5000)
})

onUnmounted(() => {
  if (pollInterval) clearInterval(pollInterval)
})
</script>

<template>
  <div :class="$style.botsPage">
    <!-- ── Header Section ── -->
    <div :class="$style.headerSection">
      <div :class="$style.titleRow">
        <div>
          <Typography size="text-h3" weight="bold">Bots</Typography>
          <Typography size="text-sm" color="tertiary">
            {{ botsStore.bots.length }} total · {{ activeBots }} active
          </Typography>
        </div>
        <Button variant="solid" size="md" @click="showCreateModal = true">
          <template #prefix>+</template>
          Create Bot
        </Button>
      </div>

      <!-- ── Stats Cards ── -->
      <div :class="$style.statsRow">
        <div :class="$style.statCard">
          <Typography size="text-h4" weight="bold" :class="$style.statValue">{{ activeBots }}</Typography>
          <Typography size="text-xs" color="tertiary" :class="$style.statLabel">ACTIVE</Typography>
        </div>
        <div :class="$style.statCard">
          <Typography size="text-h4" weight="bold" :class="$style.statValue">{{ idleBots }}</Typography>
          <Typography size="text-xs" color="tertiary" :class="$style.statLabel">IDLE</Typography>
        </div>
        <div :class="$style.statCard">
          <Typography size="text-h4" weight="bold" :class="$style.statValueSuccess">{{ enteringBots }}</Typography>
          <Typography size="text-xs" color="tertiary" :class="$style.statLabel">ENTERING</Typography>
        </div>
        <div :class="$style.statCard">
          <Typography size="text-h4" weight="bold" :class="$style.statValueSuccess">{{ holdingBots }}</Typography>
          <Typography size="text-xs" color="tertiary" :class="$style.statLabel">HOLDING</Typography>
        </div>
        <div :class="$style.statCard">
          <Typography size="text-h4" weight="bold" :class="$style.statValueWarning">{{ exitingBots }}</Typography>
          <Typography size="text-xs" color="tertiary" :class="$style.statLabel">EXITING</Typography>
        </div>
      </div>
    </div>

    <!-- ── Bot Gallery ── -->
    <div :class="$style.gallerySection">
      <Typography size="text-lg" weight="semibold" :class="$style.sectionTitle">
        All Bots
      </Typography>

      <!-- Loading State -->
      <div v-if="botsStore.loading && !botsStore.bots.length" :class="$style.emptyState">
        <Typography size="text-md" color="secondary">Loading bots...</Typography>
      </div>

      <!-- Empty State -->
      <div v-else-if="!botsStore.bots.length" :class="$style.emptyState">
        <Typography size="text-md" color="secondary">No bots configured yet</Typography>
        <Typography size="text-sm" color="tertiary">
          Create your first bot to start trading
        </Typography>
        <Button variant="outline" size="md" @click="showCreateModal = true" :class="$style.emptyCta">
          <template #prefix>+</template>
          Create Bot
        </Button>
      </div>

      <!-- Error State -->
      <div v-else-if="botsStore.error" :class="$style.errorState">
        <Typography size="text-md" color="error">{{ botsStore.error }}</Typography>
        <Button variant="outline" size="sm" @click="botsStore.load()">Retry</Button>
      </div>

      <!-- Bot Cards Grid -->
      <div v-else :class="$style.botGrid">
        <div
          v-for="bot in botsStore.bots"
          :key="bot.bot_id"
          :class="[$style.botCard, bot.is_running && $style.botCardRunning]"
        >
          <!-- Card Header -->
          <div :class="$style.cardHeader" @click="navigateToBot(bot.bot_id)">
            <div :class="$style.botIdentity">
              <Typography size="text-lg" weight="semibold">{{ bot.bot_id }}</Typography>
              <Chip :variant="stateVariant(bot.state)" size="sm">
                <span :class="$style.stateIcon">{{ stateIcon(bot.state) }}</span>
                {{ bot.state }}
              </Chip>
            </div>
            <div :class="$style.runningIndicator" v-if="bot.is_running">
              <span :class="$style.pulseDot"></span>
              <Typography size="text-xs" color="success">Running</Typography>
            </div>
          </div>

          <!-- Card Body -->
          <div :class="$style.cardBody" @click="navigateToBot(bot.bot_id)">
            <!-- Position Info (if active) -->
            <div v-if="botDetails[bot.bot_id]?.position" :class="$style.positionsSection">
              <!-- Long Exchange Position -->
              <div :class="$style.positionRow">
                <div :class="$style.positionSide">
                  <span :class="$style.longBadge">LONG</span>
                  <Typography size="text-xs" color="tertiary">{{ bot.long_exchange }}</Typography>
                </div>
                <div :class="$style.positionDetails">
                  <Typography size="text-sm" weight="medium">
                    {{ formatPrice(botDetails[bot.bot_id].position.long_entry_price) }}
                  </Typography>
                  <Typography size="text-xs" color="tertiary">
                    {{ botDetails[bot.bot_id].position.long_qty.toFixed(4) }} qty
                  </Typography>
                </div>
              </div>

              <!-- Short Exchange Position -->
              <div :class="$style.positionRow">
                <div :class="$style.positionSide">
                  <span :class="$style.shortBadge">SHORT</span>
                  <Typography size="text-xs" color="tertiary">{{ bot.short_exchange }}</Typography>
                </div>
                <div :class="$style.positionDetails">
                  <Typography size="text-sm" weight="medium">
                    {{ formatPrice(botDetails[bot.bot_id].position.short_entry_price) }}
                  </Typography>
                  <Typography size="text-xs" color="tertiary">
                    {{ botDetails[bot.bot_id].position.short_qty.toFixed(4) }} qty
                  </Typography>
                </div>
              </div>

              <!-- Net Delta -->
              <div :class="$style.deltaRow">
                <Typography size="text-xs" color="tertiary">Net Delta</Typography>
                <Typography 
                  size="text-sm" 
                  weight="medium"
                  :color="botDetails[bot.bot_id].position.net_delta > 0 ? 'success' : botDetails[bot.bot_id].position.net_delta < 0 ? 'error' : 'secondary'"
                >
                  {{ botDetails[bot.bot_id].position.net_delta.toFixed(4) }}
                </Typography>
              </div>

              <!-- PnL -->
              <div v-if="botDetails[bot.bot_id]?.pnl" :class="$style.pnlRow">
                <Typography size="text-xs" color="tertiary">Total PnL</Typography>
                <Typography 
                  size="text-sm" 
                  weight="semibold"
                  :color="botDetails[bot.bot_id].pnl.total_pnl >= 0 ? 'success' : 'error'"
                >
                  {{ formatPnl(botDetails[bot.bot_id].pnl.total_pnl) }}
                </Typography>
              </div>
            </div>

            <!-- Config Info (if idle) -->
            <div v-else :class="$style.configSection">
              <!-- Exchange Pair -->
              <div :class="$style.infoRow">
                <Typography size="text-xs" color="tertiary">Exchanges</Typography>
                <Typography size="text-sm" weight="medium">
                  <span :class="$style.exchangeLong">{{ bot.long_exchange }}</span>
                  <span :class="$style.exchangeArrow">↔</span>
                  <span :class="$style.exchangeShort">{{ bot.short_exchange }}</span>
                </Typography>
              </div>

              <!-- Instruments -->
              <div :class="$style.infoRow">
                <Typography size="text-xs" color="tertiary">Instruments</Typography>
                <Typography size="text-sm" weight="medium">
                  {{ bot.instrument_a }} · {{ bot.instrument_b }}
                </Typography>
              </div>

              <!-- Quantity -->
              <div :class="$style.infoRow">
                <Typography size="text-xs" color="tertiary">Quantity</Typography>
                <Typography size="text-sm" weight="medium">{{ bot.quantity }}</Typography>
              </div>
            </div>
          </div>

          <!-- Card Actions -->
          <div :class="$style.cardActions">
            <Button
              v-if="!bot.is_running"
              variant="outline"
              size="sm"
              color="success"
              :loading="actionLoading[bot.bot_id] === 'start'"
              @click="handleStart(bot.bot_id)"
              :class="$style.actionBtn"
            >
              Start
            </Button>
            <Button
              v-if="bot.is_running"
              variant="outline"
              size="sm"
              color="warning"
              :loading="actionLoading[bot.bot_id] === 'stop'"
              @click="handleStop(bot.bot_id)"
              :class="$style.actionBtn"
            >
              Stop
            </Button>
            <Button
              v-if="bot.is_running"
              variant="ghost"
              size="sm"
              color="error"
              :loading="actionLoading[bot.bot_id] === 'kill'"
              @click="handleKill(bot.bot_id)"
              :class="$style.actionBtn"
            >
              Kill
            </Button>
            <Button
              v-if="!bot.is_running"
              variant="ghost"
              size="sm"
              color="error"
              :loading="actionLoading[bot.bot_id] === 'delete'"
              @click="handleDelete(bot.bot_id)"
              :class="$style.actionBtn"
            >
              Delete
            </Button>
          </div>
        </div>
      </div>
    </div>

    <!-- ── Create Modal ── -->
    <BotCreateModal
      :open="showCreateModal"
      @close="showCreateModal = false"
      @create="handleCreate"
    />
  </div>
</template>

<style module>
.botsPage {
  padding: 32px 40px;
  max-width: 1400px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  gap: var(--space-8);
}

/* ── Header Section ── */
.headerSection {
  display: flex;
  flex-direction: column;
  gap: var(--space-6);
}

.titleRow {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
}

/* ── Stats Row ── */
.statsRow {
  display: grid;
  grid-template-columns: repeat(5, 1fr);
  gap: var(--space-4);
}

.statCard {
  background: var(--color-white-2);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-xl);
  padding: var(--space-4) var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.statValue {
  color: var(--color-text-primary);
}

.statValueSuccess {
  color: var(--color-success);
}

.statValueWarning {
  color: var(--color-warning);
}

.statLabel {
  letter-spacing: 0.04em;
}

/* ── Gallery Section ── */
.gallerySection {
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.sectionTitle {
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--color-stroke-divider);
}

/* ── Empty State ── */
.emptyState {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
  padding: var(--space-16) var(--space-8);
  background: var(--color-white-2);
  border: 1px dashed var(--color-stroke-divider);
  border-radius: var(--radius-xl);
  text-align: center;
}

.emptyCta {
  margin-top: var(--space-4);
}

/* ── Error State ── */
.errorState {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-4);
  padding: var(--space-12);
  background: var(--color-error-bg);
  border: 1px solid var(--color-error-stroke);
  border-radius: var(--radius-xl);
  text-align: center;
}

/* ── Bot Grid ── */
.botGrid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(360px, 1fr));
  gap: var(--space-4);
}

/* ── Bot Card ── */
.botCard {
  background: var(--color-white-2);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-xl);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  transition: all 0.15s ease;
}

.botCard:hover {
  border-color: var(--color-stroke-primary);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.08);
}

.botCardRunning {
  border-color: var(--color-success-stroke, rgba(40, 167, 69, 0.3));
}

/* Card Header */
.cardHeader {
  padding: var(--space-4) var(--space-5);
  background: var(--color-white-4);
  border-bottom: 1px solid var(--color-stroke-divider);
  cursor: pointer;
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--space-2);
}

.botIdentity {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.stateIcon {
  margin-right: var(--space-1);
}

.runningIndicator {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.pulseDot {
  width: 8px;
  height: 8px;
  background: var(--color-success);
  border-radius: 50%;
  animation: pulse 2s infinite;
}

@keyframes pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.5; }
}

/* Card Body */
.cardBody {
  padding: var(--space-4) var(--space-5);
  cursor: pointer;
  flex: 1;
}

/* ── Positions Section ── */
.positionsSection {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.positionRow {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: var(--space-2) 0;
  border-bottom: 1px solid var(--color-stroke-divider);
}

.positionRow:last-of-type {
  border-bottom: none;
}

.positionSide {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.longBadge {
  background: rgba(40, 167, 69, 0.15);
  color: var(--color-success);
  font-size: 10px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  letter-spacing: 0.02em;
}

.shortBadge {
  background: rgba(220, 53, 69, 0.15);
  color: var(--color-error);
  font-size: 10px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: var(--radius-sm);
  letter-spacing: 0.02em;
}

.positionDetails {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 2px;
}

.deltaRow {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-top: var(--space-2);
  margin-top: var(--space-1);
  border-top: 1px dashed var(--color-stroke-divider);
}

.pnlRow {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-top: var(--space-2);
  margin-top: var(--space-1);
  border-top: 1px solid var(--color-stroke-divider);
}

/* ── Config Section (Idle bots) ── */
.configSection {
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
}

.infoRow {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.exchangeLong {
  color: var(--color-success);
}

.exchangeArrow {
  color: var(--color-text-tertiary);
  margin: 0 var(--space-1);
}

.exchangeShort {
  color: var(--color-error);
}

/* Card Actions */
.cardActions {
  display: flex;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-5);
  background: var(--color-white-4);
  border-top: 1px solid var(--color-stroke-divider);
}

.actionBtn {
  flex: 1;
}

/* ── Responsive ── */
@media (max-width: 1024px) {
  .statsRow {
    grid-template-columns: repeat(3, 1fr);
  }
}

@media (max-width: 768px) {
  .botsPage {
    padding: 24px 16px 100px;
  }

  .statsRow {
    grid-template-columns: repeat(3, 1fr);
    gap: var(--space-3);
  }

  .statCard {
    padding: var(--space-3) var(--space-4);
  }

  .botGrid {
    grid-template-columns: 1fr;
  }
}

@media (max-width: 480px) {
  .statsRow {
    grid-template-columns: repeat(2, 1fr);
  }

  .titleRow {
    flex-direction: column;
    gap: var(--space-4);
  }

  .cardHeader,
  .cardBody {
    padding: var(--space-3) var(--space-4);
  }

  .cardActions {
    padding: var(--space-2) var(--space-4);
  }
}
</style>
