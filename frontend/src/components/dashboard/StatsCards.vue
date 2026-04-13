<script setup lang="ts">
import Typography from '@/components/ui/Typography.vue'

defineProps<{
  activeBots: number
  totalBots: number
  totalPnl: number
  totalPositions: number
}>()

function formatPnl(val: number | string): string {
  const n = Number(val) || 0
  const sign = n >= 0 ? '+' : ''
  return `${sign}$${n.toFixed(2)}`
}
</script>

<template>
  <div :class="$style.cards">
    <div :class="$style.card">
      <div :class="$style.icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10" /><path d="M12 6v6l4 2" />
        </svg>
      </div>
      <div :class="$style.info">
        <Typography size="text-sm" color="secondary">Active Bots</Typography>
        <Typography size="text-h5" weight="medium">{{ activeBots }} / {{ totalBots }}</Typography>
      </div>
    </div>

    <div :class="$style.card">
      <div :class="$style.icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M12 2v20M17 5H9.5a3.5 3.5 0 000 7h5a3.5 3.5 0 010 7H6" />
        </svg>
      </div>
      <div :class="$style.info">
        <Typography size="text-sm" color="secondary">Unrealized PnL</Typography>
        <Typography
          size="text-h5"
          weight="medium"
          :color="totalPnl >= 0 ? 'success' : 'error'"
        >{{ formatPnl(totalPnl) }}</Typography>
      </div>
    </div>

    <div :class="$style.card">
      <div :class="$style.icon">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="2" y="3" width="20" height="14" rx="2" /><path d="M8 21h8M12 17v4" />
        </svg>
      </div>
      <div :class="$style.info">
        <Typography size="text-sm" color="secondary">Open Positions</Typography>
        <Typography size="text-h5" weight="medium">{{ totalPositions }}</Typography>
      </div>
    </div>
  </div>
</template>

<style module>
.cards {
  display: flex;
  gap: var(--space-5);
}

.card {
  flex: 1 1 0%;
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: 1.25rem;
  display: flex;
  gap: 0.75rem;
  align-items: start;
}

.icon {
  border-radius: var(--radius-md);
  background: var(--color-white-10);
  width: 36px;
  height: 36px;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  color: var(--color-text-secondary);
}

.info {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}
</style>
