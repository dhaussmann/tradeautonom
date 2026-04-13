<script setup lang="ts">
import Typography from '@/components/ui/Typography.vue'

defineProps<{
  totalPoints: number
  totalVolume: number
  activeBots: number
  totalBots: number
  openPositions: number
}>()

function formatNumber(n: number): string {
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return n.toFixed(0)
}

function formatVolume(n: number): string {
  if (n >= 1_000_000) return '$' + (n / 1_000_000).toFixed(2) + 'M'
  if (n >= 1_000) return '$' + (n / 1_000).toFixed(1) + 'K'
  return '$' + n.toFixed(0)
}
</script>

<template>
  <div :class="$style.bar">
    <div :class="$style.kpi">
      <Typography size="text-xs" color="tertiary">Total Points</Typography>
      <Typography size="text-h6" weight="bold">{{ formatNumber(totalPoints) }}</Typography>
    </div>
    <div :class="$style.divider" />
    <div :class="$style.kpi">
      <Typography size="text-xs" color="tertiary">Total Volume</Typography>
      <Typography size="text-h6" weight="bold">{{ formatVolume(totalVolume) }}</Typography>
    </div>
    <div :class="$style.divider" />
    <div :class="$style.kpi">
      <Typography size="text-xs" color="tertiary">Bots</Typography>
      <div :class="$style.kpiRow">
        <Typography size="text-h6" weight="bold">{{ activeBots }}</Typography>
        <Typography size="text-sm" color="tertiary">&nbsp;/ {{ totalBots }}</Typography>
      </div>
    </div>
    <div :class="$style.divider" />
    <div :class="$style.kpi">
      <Typography size="text-xs" color="tertiary">Open Positions</Typography>
      <Typography size="text-h6" weight="bold">{{ openPositions }}</Typography>
    </div>
  </div>
</template>

<style module>
.bar {
  display: flex;
  align-items: center;
  gap: var(--space-6);
  padding: var(--space-4) var(--space-6);
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  flex-wrap: wrap;
}

.kpi {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 100px;
}

.kpiRow {
  display: flex;
  align-items: baseline;
}

.divider {
  width: 1px;
  height: 36px;
  background: var(--color-stroke-divider);
  flex-shrink: 0;
}

@media (max-width: 640px) {
  .bar {
    gap: var(--space-4);
  }
  .divider {
    display: none;
  }
  .kpi {
    min-width: 80px;
  }
}
</style>
