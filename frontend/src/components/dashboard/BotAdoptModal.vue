<script setup lang="ts">
/**
 * BotAdoptModal — adopt an existing delta-neutral hedge position into a
 * new bot.
 *
 * Unlike BotCreateModal, no exchange/token/direction selection is needed —
 * everything comes prefilled from the matched pair on PositionsView. The
 * user only confirms the bot ID and (optionally) tweaks TWAP/risk
 * settings used when the bot eventually exits the position.
 *
 * Backend enforces strict size match between the two legs and re-fetches
 * positions from the exchange before persisting; this modal only shows
 * what the operator should expect.
 */
import { ref, computed, watch } from 'vue'
import Typography from '@/components/ui/Typography.vue'
import Button from '@/components/ui/Button.vue'
import { adoptBot } from '@/lib/api'
import { useBotsStore } from '@/stores/bots'
import type { Position as AccountPosition } from '@/types/account'

interface PairLeg {
  exchange: string
  instrument: string
  size: number
  entry_price: number
  mark_price?: number
}

const props = defineProps<{
  open: boolean
  /** Token symbol of the pair (e.g. "XRP", "DOGE"). */
  token: string
  /** Long leg of the hedge — extended/grvt/nado typically. */
  long: PairLeg | AccountPosition | null
  /** Short leg of the hedge — variational/grvt/nado typically. */
  short: PairLeg | AccountPosition | null
}>()

const emit = defineEmits<{ close: []; adopted: [botId: string] }>()

const botsStore = useBotsStore()

const submitting = ref(false)
const error = ref<string | null>(null)

// ── Form fields ──
const botId = ref('')
const twapNumChunks = ref(10)
const twapInterval = ref(10)
const confirmed = ref(false)

// Reset when modal opens
watch(() => props.open, (v) => {
  if (v) {
    error.value = null
    submitting.value = false
    confirmed.value = false
    twapNumChunks.value = 10
    twapInterval.value = 10
    // Default bot ID = token, with -2/-3 suffix if collision
    const base = props.token.toUpperCase()
    const existing = new Set(botsStore.bots.map(b => b.bot_id))
    if (!existing.has(base)) {
      botId.value = base
    } else {
      let i = 2
      while (existing.has(`${base}-${i}`)) i++
      botId.value = `${base}-${i}`
    }
  }
})

// ── Validation ──
const sizeLong = computed(() => Math.abs(Number(props.long?.size ?? 0)))
const sizeShort = computed(() => Math.abs(Number(props.short?.size ?? 0)))
const sizesMatch = computed(() => {
  const l = sizeLong.value
  const s = sizeShort.value
  if (l <= 0 || s <= 0) return false
  return Math.abs(l - s) / Math.max(l, s) <= 1e-6
})

const botIdValid = computed(() => /^[A-Za-z0-9_-]+$/.test(botId.value))
const botIdCollision = computed(() =>
  botsStore.bots.some(b => b.bot_id === botId.value),
)

const canSubmit = computed(() =>
  !submitting.value &&
  !!props.long &&
  !!props.short &&
  sizesMatch.value &&
  botIdValid.value &&
  !botIdCollision.value &&
  confirmed.value &&
  twapNumChunks.value > 0 &&
  twapInterval.value > 0,
)

function fmtPrice(n: number | undefined | null): string {
  if (n == null) return '—'
  const v = Number(n)
  if (!v) return '—'
  if (v >= 1000) return `$${v.toFixed(2)}`
  if (v >= 1) return `$${v.toFixed(4)}`
  return `$${v.toFixed(6)}`
}

function fmtSize(n: number | undefined | null): string {
  if (n == null) return '—'
  const v = Math.abs(Number(n))
  if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`
  if (v >= 1_000) return `${(v / 1_000).toFixed(2)}K`
  if (v >= 1) return v.toFixed(4)
  return v.toFixed(6)
}

function displayExchange(ex: string): string {
  if (ex === 'grvt') return 'GRVT'
  if (ex === 'nado') return 'Nado'
  return ex.charAt(0).toUpperCase() + ex.slice(1)
}

async function submit() {
  if (!canSubmit.value) return
  if (!props.long || !props.short) return

  submitting.value = true
  error.value = null
  try {
    const resp = await adoptBot({
      bot_id: botId.value,
      long_exchange: props.long.exchange,
      long_symbol: props.long.instrument,
      short_exchange: props.short.exchange,
      short_symbol: props.short.instrument,
      quantity: Number(sizeLong.value.toFixed(8)),
      twap_num_chunks: twapNumChunks.value,
      twap_interval_s: twapInterval.value,
    })
    // Refresh bots list so the new bot appears in stores
    await botsStore.load()
    emit('adopted', resp.bot_id)
    emit('close')
  } catch (e) {
    error.value = e instanceof Error ? e.message : 'Adoption failed'
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
          <Typography size="text-h6" weight="semibold">Adopt Hedge as Bot</Typography>
          <button :class="$style.closeBtn" @click="emit('close')">✕</button>
        </div>

        <div :class="$style.body">
          <!-- Position summary -->
          <div :class="$style.section">
            <Typography size="text-sm" color="tertiary">
              You're about to adopt this existing delta-neutral hedge into a
              new bot. The bot will start in <strong>HOLDING</strong> state.
              No new orders are placed — the position is already open.
              When you eventually press <strong>Stop</strong>, the bot will
              close both legs via TWAP using the settings below.
            </Typography>
          </div>

          <div :class="$style.tokenRow">
            <Typography size="text-h5" weight="bold">{{ token }}</Typography>
            <span
              v-if="!sizesMatch"
              :class="$style.warnPill"
              :title="`long=${sizeLong}, short=${sizeShort}`"
            >Sizes don't match</span>
          </div>

          <div :class="$style.legGrid">
            <div :class="[$style.legCard, $style.legLong]">
              <div :class="$style.legHeader">
                <Typography size="text-xs" color="tertiary">LONG</Typography>
                <Typography size="text-sm" weight="medium">{{ long ? displayExchange(long.exchange) : '—' }}</Typography>
              </div>
              <div :class="$style.legBody">
                <div :class="$style.legRow">
                  <span :class="$style.legLabel">Symbol</span>
                  <Typography size="text-xs" color="secondary">{{ long?.instrument || '—' }}</Typography>
                </div>
                <div :class="$style.legRow">
                  <span :class="$style.legLabel">Size</span>
                  <Typography size="text-sm" weight="medium">{{ fmtSize(long?.size) }}</Typography>
                </div>
                <div :class="$style.legRow">
                  <span :class="$style.legLabel">Entry</span>
                  <Typography size="text-sm" color="secondary">{{ fmtPrice(long?.entry_price) }}</Typography>
                </div>
                <div v-if="long?.mark_price" :class="$style.legRow">
                  <span :class="$style.legLabel">Mark</span>
                  <Typography size="text-sm">{{ fmtPrice(long.mark_price) }}</Typography>
                </div>
              </div>
            </div>

            <div :class="[$style.legCard, $style.legShort]">
              <div :class="$style.legHeader">
                <Typography size="text-xs" color="tertiary">SHORT</Typography>
                <Typography size="text-sm" weight="medium">{{ short ? displayExchange(short.exchange) : '—' }}</Typography>
              </div>
              <div :class="$style.legBody">
                <div :class="$style.legRow">
                  <span :class="$style.legLabel">Symbol</span>
                  <Typography size="text-xs" color="secondary">{{ short?.instrument || '—' }}</Typography>
                </div>
                <div :class="$style.legRow">
                  <span :class="$style.legLabel">Size</span>
                  <Typography size="text-sm" weight="medium">{{ fmtSize(short?.size) }}</Typography>
                </div>
                <div :class="$style.legRow">
                  <span :class="$style.legLabel">Entry</span>
                  <Typography size="text-sm" color="secondary">{{ fmtPrice(short?.entry_price) }}</Typography>
                </div>
                <div v-if="short?.mark_price" :class="$style.legRow">
                  <span :class="$style.legLabel">Mark</span>
                  <Typography size="text-sm">{{ fmtPrice(short.mark_price) }}</Typography>
                </div>
              </div>
            </div>
          </div>

          <!-- Bot settings -->
          <div :class="$style.section">
            <div :class="$style.field">
              <label :class="$style.label" for="adopt-bot-id">Bot ID</label>
              <input
                id="adopt-bot-id"
                v-model="botId"
                type="text"
                :class="[$style.input, !botIdValid && $style.inputError]"
                placeholder="e.g. XRP"
                spellcheck="false"
              />
              <Typography
                v-if="!botIdValid && botId"
                size="text-xs"
                color="error"
              >Only letters, numbers, dashes and underscores.</Typography>
              <Typography
                v-else-if="botIdCollision"
                size="text-xs"
                color="error"
              >A bot with this ID already exists.</Typography>
              <Typography
                v-else
                size="text-xs"
                color="tertiary"
              >Used as the persistent identifier and shown in /bots.</Typography>
            </div>

            <Typography size="text-sm" weight="semibold" :class="$style.subhead">Exit TWAP Settings</Typography>
            <Typography size="text-xs" color="tertiary">
              These control how the bot will exit the position when you press Stop later.
            </Typography>

            <div :class="$style.row">
              <div :class="$style.field">
                <label :class="$style.label" for="adopt-chunks">Chunks</label>
                <input
                  id="adopt-chunks"
                  v-model.number="twapNumChunks"
                  type="number"
                  min="1"
                  max="100"
                  :class="$style.input"
                />
                <Typography size="text-xs" color="tertiary">Split exit into N chunks</Typography>
              </div>
              <div :class="$style.field">
                <label :class="$style.label" for="adopt-interval">Interval (s)</label>
                <input
                  id="adopt-interval"
                  v-model.number="twapInterval"
                  type="number"
                  min="1"
                  max="600"
                  :class="$style.input"
                />
                <Typography size="text-xs" color="tertiary">Seconds between chunks</Typography>
              </div>
            </div>
          </div>

          <!-- Confirmation -->
          <label :class="$style.confirmBox">
            <input v-model="confirmed" type="checkbox" />
            <span>
              I understand that this bot will take ownership of the existing
              hedge and will close both legs when I press Stop.
            </span>
          </label>

          <Typography v-if="error" size="text-sm" color="error">{{ error }}</Typography>
        </div>

        <!-- Footer -->
        <div :class="$style.footer">
          <Button variant="ghost" @click="emit('close')">Cancel</Button>
          <Button variant="solid" :disabled="!canSubmit" @click="submit">
            {{ submitting ? 'Adopting…' : 'Adopt as Bot' }}
          </Button>
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
  width: 600px;
  max-width: 90vw;
  max-height: 90vh;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
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

.body {
  padding: var(--space-5) var(--space-6);
  display: flex;
  flex-direction: column;
  gap: var(--space-4);
}

.section {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}

.subhead {
  margin-top: var(--space-2);
}

.tokenRow {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.warnPill {
  background: rgba(239, 68, 68, 0.12);
  color: #ef4444;
  padding: 2px var(--space-2);
  border-radius: var(--radius-md);
  font-size: var(--text-xs);
  font-weight: 500;
}

.legGrid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-3);
}

.legCard {
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-lg);
  padding: var(--space-3) var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  background: var(--color-bg-primary);
}

.legLong {
  border-color: rgba(34, 197, 94, 0.4);
}

.legShort {
  border-color: rgba(239, 68, 68, 0.4);
}

.legHeader {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-bottom: var(--space-2);
  border-bottom: 1px solid var(--color-stroke-divider);
}

.legBody {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.legRow {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: var(--text-xs);
}

.legLabel {
  color: var(--color-text-tertiary);
}

.row {
  display: flex;
  gap: var(--space-4);
}

.row > .field {
  flex: 1;
}

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
  background: var(--color-bg-primary);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-md);
  padding: var(--space-2) var(--space-3);
  color: var(--color-text-primary);
  font-size: var(--text-sm);
  outline: none;
  transition: border-color 0.15s;
}

.input:focus {
  border-color: var(--color-brand, #6366f1);
}

.inputError {
  border-color: #ef4444;
}

.confirmBox {
  display: flex;
  gap: var(--space-2);
  align-items: flex-start;
  padding: var(--space-3);
  background: var(--color-bg-primary);
  border: 1px solid var(--color-stroke-divider);
  border-radius: var(--radius-md);
  font-size: var(--text-xs);
  color: var(--color-text-secondary);
  cursor: pointer;
  line-height: 1.4;
}

.confirmBox input[type="checkbox"] {
  margin-top: 2px;
  flex-shrink: 0;
}

.footer {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-3);
  padding: var(--space-4) var(--space-6);
  border-top: 1px solid var(--color-stroke-divider);
}

@keyframes fade-in {
  from { opacity: 0; transform: scale(0.96); }
  to { opacity: 1; transform: scale(1); }
}
</style>
