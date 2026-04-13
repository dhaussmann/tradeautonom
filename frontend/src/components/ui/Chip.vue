<script setup lang="ts">
withDefaults(defineProps<{
  variant?: 'long' | 'short' | 'success' | 'error' | 'warning' | 'neutral' | 'brand' | 'info'
  size?: 'sm' | 'md'
}>(), {
  variant: 'neutral',
  size: 'sm',
})
</script>

<template>
  <span :class="[$style.Chip, $style[`Chip--${variant}`], $style[`Chip--${size}`]]">
    <slot />
  </span>
</template>

<style module>
.Chip {
  display: inline-flex;
  align-items: center;
  border-radius: var(--radius-sm);
  font-family: var(--font-inter);
  font-weight: 500;
  white-space: nowrap;
}

.Chip--sm { padding: 2px 8px; font-size: var(--text-xs); }
.Chip--md { padding: 4px 10px; font-size: var(--text-sm); }

.Chip--long    { background: var(--color-success-bg); color: var(--color-success); }
.Chip--short   { background: var(--color-error-bg);   color: var(--color-error); }
.Chip--success { background: var(--color-success-bg); color: var(--color-success); }
.Chip--error   { background: var(--color-error-bg);   color: var(--color-error); }
.Chip--warning { background: var(--color-warning-bg); color: var(--color-warning); }
.Chip--neutral { background: var(--color-white-10);   color: var(--color-text-secondary); }
.Chip--brand   { background: var(--color-brand-bg);   color: var(--color-brand); }
.Chip--info    { background: rgba(59, 130, 246, 0.15); color: #3b82f6; }
</style>
