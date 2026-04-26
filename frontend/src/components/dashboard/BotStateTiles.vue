<script setup lang="ts">
import { computed } from 'vue'
import Typography from '@/components/ui/Typography.vue'
import Button from '@/components/ui/Button.vue'
import type { BotSummary, BotState } from '@/types/bot'

export type BotStateFilter = BotState | 'ALL'

const props = defineProps<{
  bots: BotSummary[]
  activeFilter: BotStateFilter
}>()

const emit = defineEmits<{
  'filter-change': [filter: BotStateFilter]
  'add-bot': []
}>()

// ── Counts ────────────────────────────────────────────
// PAUSED_ENTERING is counted as ENTERING (same state-bucket).
// PAUSED_EXITING is counted as EXITING.

const total = computed(() => props.bots.length)

const holdingCount = computed(() =>
  props.bots.filter(b => b.state === 'HOLDING').length
)

const enteringCount = computed(() =>
  props.bots.filter(
    b => b.state === 'ENTERING' || b.state === 'PAUSED_ENTERING'
  ).length
)

const exitingCount = computed(() =>
  props.bots.filter(
    b => b.state === 'EXITING' || b.state === 'PAUSED_EXITING'
  ).length
)

const idleCount = computed(() =>
  props.bots.filter(b => b.state === 'IDLE').length
)

interface Tile {
  id: BotStateFilter
  label: string
  icon: string
  count: number
  /** Tailwind-ish semantic color name → maps to CSS classes below */
  tone: 'neutral' | 'success' | 'brand' | 'warning' | 'tertiary'
}

const tiles = computed<Tile[]>(() => [
  { id: 'ALL',      label: 'ALL',      icon: '▦', count: total.value,         tone: 'neutral'   },
  { id: 'HOLDING',  label: 'HOLDING',  icon: '✓', count: holdingCount.value,  tone: 'success'   },
  { id: 'ENTERING', label: 'ENTERING', icon: '→', count: enteringCount.value, tone: 'brand'     },
  { id: 'EXITING',  label: 'EXITING',  icon: '←', count: exitingCount.value,  tone: 'warning'   },
  { id: 'IDLE',     label: 'IDLE',     icon: '○', count: idleCount.value,     tone: 'tertiary'  },
])

function isActive(id: BotStateFilter): boolean {
  return props.activeFilter === id
}

function selectTile(id: BotStateFilter) {
  emit('filter-change', id)
}
</script>

<template>
  <div :class="$style.tiles">
    <button
      v-for="tile in tiles"
      :key="tile.id"
      type="button"
      :class="[
        $style.tile,
        $style[`tile--${tile.tone}`],
        isActive(tile.id) && $style['tile--active'],
      ]"
      @click="selectTile(tile.id)"
    >
      <div :class="$style.tileTop">
        <span :class="$style.tileIcon">{{ tile.icon }}</span>
        <Typography size="text-xs" color="tertiary" :class="$style.tileLabel">
          {{ tile.label }}
        </Typography>
      </div>
      <Typography
        size="text-h3"
        weight="bold"
        :class="$style.tileCount"
      >{{ tile.count }}</Typography>
    </button>

    <div :class="$style.addCell">
      <Button
        variant="solid"
        size="md"
        :class="$style.addBtn"
        @click="emit('add-bot')"
      >
        <template #prefix>+</template>
        Add Bot
      </Button>
    </div>
  </div>
</template>

<style module>
.tiles {
  display: grid;
  grid-template-columns: repeat(5, 1fr) auto;
  gap: var(--space-3);
  align-items: stretch;
}

.tile {
  appearance: none;
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  border-radius: var(--radius-xl);
  padding: var(--space-4) var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  cursor: pointer;
  text-align: left;
  font: inherit;
  color: inherit;
  transition: background 0.15s ease, border-color 0.15s ease, transform 0.1s ease;
  min-height: 96px;
}

.tile:hover {
  background: var(--color-white-4);
  border-color: var(--color-stroke-primary);
}

.tile:active {
  transform: scale(0.98);
}

.tile--active {
  border-color: var(--color-brand);
  background: var(--color-white-4);
  box-shadow: 0 0 0 1px var(--color-brand) inset;
}

.tileTop {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.tileIcon {
  font-size: var(--text-md);
  line-height: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
}

.tileLabel {
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.tileCount {
  margin-top: auto;
}

/* Tone-based icon coloring */
.tile--neutral  .tileIcon { color: var(--color-text-secondary); }
.tile--success  .tileIcon { color: var(--color-success); }
.tile--brand    .tileIcon { color: var(--color-brand); }
.tile--warning  .tileIcon { color: var(--color-warning); }
.tile--tertiary .tileIcon { color: var(--color-text-tertiary); }

.tile--success.tile--active  { box-shadow: 0 0 0 1px var(--color-success) inset; border-color: var(--color-success); }
.tile--brand.tile--active    { box-shadow: 0 0 0 1px var(--color-brand) inset;   border-color: var(--color-brand); }
.tile--warning.tile--active  { box-shadow: 0 0 0 1px var(--color-warning) inset; border-color: var(--color-warning); }

.addCell {
  display: flex;
  align-items: center;
  justify-content: center;
  padding-left: var(--space-2);
}

.addBtn {
  white-space: nowrap;
}

/* Responsive: <900px → 3 columns; <600px → 2 columns; Add-Bot wraps */
@media (max-width: 900px) {
  .tiles {
    grid-template-columns: repeat(3, 1fr);
  }
  .addCell {
    grid-column: 1 / -1;
    padding-left: 0;
  }
  .addBtn {
    width: 100%;
  }
}

@media (max-width: 480px) {
  .tiles {
    grid-template-columns: repeat(2, 1fr);
  }
}
</style>
