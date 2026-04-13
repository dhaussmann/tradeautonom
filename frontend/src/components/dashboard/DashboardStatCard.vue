<script setup lang="ts">
import Typography from '@/components/ui/Typography.vue'

defineProps<{
  title: string
  value: string
  subtitle?: string
  color?: 'success' | 'error' | 'primary' | 'secondary' | 'tertiary'
  gauge?: number | null // 0-200 range for gauge display (100 = neutral)
}>()
</script>

<template>
  <div :class="$style.card">
    <Typography size="text-xs" color="tertiary" :class="$style.title">{{ title }}</Typography>

    <!-- Gauge ring (optional, for DN Factor / Point Factor) -->
    <div v-if="gauge != null" :class="$style.gaugeWrap">
      <svg viewBox="0 0 100 60" :class="$style.gaugeSvg">
        <!-- Background arc -->
        <path
          d="M 10 55 A 40 40 0 0 1 90 55"
          fill="none"
          stroke="var(--color-stroke-divider)"
          stroke-width="6"
          stroke-linecap="round"
        />
        <!-- Value arc -->
        <path
          d="M 10 55 A 40 40 0 0 1 90 55"
          fill="none"
          :stroke="(gauge ?? 0) >= 100 ? '#22c55e' : '#ef4444'"
          stroke-width="6"
          stroke-linecap="round"
          :stroke-dasharray="`${Math.min(Math.max((gauge ?? 0) / 200, 0), 1) * 126} 126`"
        />
      </svg>
      <div :class="$style.gaugeValue">
        <Typography
          size="text-lg"
          weight="bold"
          :color="(gauge ?? 0) >= 100 ? 'success' : 'error'"
        >{{ value }}</Typography>
      </div>
    </div>

    <!-- Standard value (no gauge) -->
    <Typography
      v-else
      size="text-h6"
      weight="bold"
      :color="color || 'primary'"
      :class="$style.value"
    >{{ value }}</Typography>

    <Typography v-if="subtitle" size="text-xs" color="tertiary" :class="$style.subtitle">
      {{ subtitle }}
    </Typography>
  </div>
</template>

<style module>
.card {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: var(--space-4) var(--space-5);
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-height: 120px;
}

.title {
  text-transform: uppercase;
  letter-spacing: 0.04em;
}

.value {
  margin-top: auto;
}

.subtitle {
  margin-top: var(--space-1);
}

.gaugeWrap {
  position: relative;
  width: 100%;
  max-width: 140px;
  margin: var(--space-2) auto 0;
}

.gaugeSvg {
  width: 100%;
  height: auto;
}

.gaugeValue {
  position: absolute;
  bottom: 0;
  left: 50%;
  transform: translateX(-50%);
  text-align: center;
}
</style>
