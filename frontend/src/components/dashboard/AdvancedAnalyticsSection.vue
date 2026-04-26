<script setup lang="ts">
import Typography from '@/components/ui/Typography.vue'
import DashboardStatCard from '@/components/dashboard/DashboardStatCard.vue'
import PointsEfficiencyWidget from '@/components/dashboard/PointsEfficiencyWidget.vue'
import FundingWidget from '@/components/dashboard/FundingWidget.vue'

interface MostTradedToken {
  token: string
  volume: number
}

defineProps<{
  pointFactor: number
  mostTraded: MostTradedToken[]
  deltaNeutralFactor: number | null
  totalFees: number
  closedCount: number
}>()

function formatUsd(n: number): string {
  const abs = Math.abs(n)
  const sign = n < 0 ? '-' : ''
  if (abs >= 1_000_000) return `${sign}$${(abs / 1_000_000).toFixed(2)}M`
  if (abs >= 1_000) return `${sign}$${(abs / 1_000).toFixed(1)}K`
  return `${sign}$${abs.toFixed(2)}`
}

function formatVolume(n: number): string {
  if (n >= 1_000_000) return '$' + (n / 1_000_000).toFixed(2) + 'M'
  if (n >= 1_000) return '$' + (n / 1_000).toFixed(1) + 'K'
  return '$' + n.toFixed(0)
}
</script>

<template>
  <details :class="$style.section">
    <summary :class="$style.summary">
      <span :class="$style.summaryChevron" aria-hidden="true">▶</span>
      <Typography size="text-md" weight="semibold">Advanced Analytics</Typography>
      <Typography size="text-xs" color="tertiary" :class="$style.summaryHint">
        click to expand
      </Typography>
    </summary>

    <div :class="$style.content">
      <!-- Top: 2-col widget row -->
      <div :class="$style.widgetRow">
        <PointsEfficiencyWidget />
        <FundingWidget />
      </div>

      <!-- Bottom: 4 small stat cards -->
      <div :class="$style.statRow">
        <DashboardStatCard
          title="Point Factor"
          :value="pointFactor > 0 ? pointFactor.toFixed(1) : '—'"
          subtitle="points per $100K volume"
        />

        <div :class="$style.mostTradedCard">
          <Typography size="text-xs" color="tertiary" :class="$style.mostTradedTitle">
            MOST TRADED
          </Typography>
          <div v-if="mostTraded.length" :class="$style.tokenList">
            <div v-for="(t, i) in mostTraded" :key="t.token" :class="$style.tokenRow">
              <Typography size="text-sm" weight="semibold">
                {{ i + 1 }}. {{ t.token }}
              </Typography>
              <Typography size="text-xs" color="tertiary">
                {{ formatVolume(t.volume) }}
              </Typography>
            </div>
          </div>
          <Typography v-else size="text-sm" color="tertiary" :class="$style.noData">
            No trades yet
          </Typography>
        </div>

        <DashboardStatCard
          title="Delta Neutral Factor"
          :value="deltaNeutralFactor != null ? deltaNeutralFactor.toFixed(1) + '%' : '—'"
          :gauge="deltaNeutralFactor"
        />

        <DashboardStatCard
          title="Paid Fees"
          :value="formatUsd(Math.abs(totalFees))"
          :subtitle="`${closedCount} positions`"
          color="error"
        />
      </div>
    </div>
  </details>
</template>

<style module>
.section {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  overflow: hidden;
}

.summary {
  list-style: none;
  cursor: pointer;
  padding: var(--space-4) var(--space-5);
  display: flex;
  align-items: center;
  gap: var(--space-3);
  user-select: none;
  transition: background 0.15s ease;
}

.summary:hover {
  background: var(--color-white-4);
}

/* Hide native marker (Chrome/Safari) */
.summary::-webkit-details-marker {
  display: none;
}

.summaryChevron {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  font-size: 10px;
  color: var(--color-text-secondary);
  transition: transform 0.2s ease;
}

.section[open] .summaryChevron {
  transform: rotate(90deg);
}

.summaryHint {
  margin-left: auto;
}

.section[open] .summaryHint {
  display: none;
}

.content {
  padding: var(--space-4) var(--space-5) var(--space-5);
  border-top: 1px solid var(--color-stroke-divider);
  display: flex;
  flex-direction: column;
  gap: var(--space-5);
}

.widgetRow {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: var(--space-5);
  align-items: start;
}

.statRow {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: var(--space-4);
}

/* Most-Traded card (mirrors DashboardStatCard styling) */
.mostTradedCard {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: var(--space-4) var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-height: 120px;
}

.mostTradedTitle {
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.tokenList {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  margin-top: auto;
}

.tokenRow {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.noData {
  margin-top: auto;
}

@media (max-width: 900px) {
  .widgetRow {
    grid-template-columns: 1fr;
  }
  .statRow {
    grid-template-columns: repeat(2, 1fr);
  }
}

@media (max-width: 480px) {
  .statRow {
    grid-template-columns: 1fr;
  }
}
</style>
