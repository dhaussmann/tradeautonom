<script setup lang="ts">
withDefaults(defineProps<{
  active?: boolean
  color?: 'brand' | 'success' | 'error' | 'warning' | 'neutral'
}>(), {
  active: false,
  color: 'brand',
})
</script>

<template>
  <span :class="[$style.dot, active && $style['dot--active'], $style[`dot--${color}`]]" />
</template>

<style module>
.dot {
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
}

.dot--brand   { background: var(--color-brand); }
.dot--success { background: var(--color-success); }
.dot--error   { background: var(--color-error); }
.dot--warning { background: var(--color-warning); }
.dot--neutral { background: var(--color-text-tertiary); }

.dot--active {
  animation: pulse 1.2s ease-in-out infinite;
}
</style>
