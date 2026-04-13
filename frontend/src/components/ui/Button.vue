<script setup lang="ts">
withDefaults(defineProps<{
  variant?: 'outline' | 'solid' | 'ghost'
  size?: 'sm' | 'md' | 'lg'
  color?: 'default' | 'success' | 'error' | 'warning'
  loading?: boolean
  disabled?: boolean
}>(), {
  variant: 'outline',
  size: 'md',
  color: 'default',
  loading: false,
  disabled: false,
})

defineEmits<{ click: [e: MouseEvent] }>()
</script>

<template>
  <button
    :class="[
      $style.Button,
      $style[`Button--${variant}`],
      $style[`Button--${size}`],
      $style[`Button--${color}`],
      loading && $style['Button--loading'],
    ]"
    :disabled="disabled || loading"
    @click="$emit('click', $event)"
  >
    <span v-if="$slots.prefix" :class="$style.Button__prefix"><slot name="prefix" /></span>
    <span :class="$style.Button__label"><slot /></span>
    <span v-if="loading" :class="$style.Button__spinner" />
    <span v-else-if="$slots.suffix" :class="$style.Button__suffix"><slot name="suffix" /></span>
  </button>
</template>

<style module>
.Button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  font-family: var(--font-inter);
  font-size: var(--text-md);
  font-weight: 500;
  border-radius: var(--radius-md);
  cursor: pointer;
  transition: all var(--duration-md) var(--ease-out-1);
  white-space: nowrap;
  border: none;
  outline: none;
}

.Button:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

/* ── Sizes ── */
.Button--sm { height: 32px; padding: 0 var(--space-3); font-size: var(--text-sm); }
.Button--md { height: 40px; padding: 0 var(--space-4); }
.Button--lg { height: 48px; padding: 0 var(--space-6); font-size: var(--text-lg); }

/* ── Outline ── */
.Button--outline {
  background: transparent;
  border: 1px solid var(--color-stroke-primary);
  color: var(--color-text-primary);
}
.Button--outline:hover:not(:disabled) {
  background: var(--color-white-10);
}

/* ── Solid ── */
.Button--solid {
  background: var(--color-text-primary);
  color: var(--color-text-dark);
  font-weight: 600;
}
.Button--solid:hover:not(:disabled) {
  opacity: 0.9;
}

/* ── Ghost ── */
.Button--ghost {
  background: transparent;
  color: var(--color-text-secondary);
}
.Button--ghost:hover:not(:disabled) {
  color: var(--color-text-primary);
  background: var(--color-white-4);
}

/* ── Colors ── */
.Button--success.Button--outline {
  border-color: var(--color-success);
  color: var(--color-success);
}
.Button--success.Button--solid {
  background: var(--color-brand);
  color: var(--color-text-dark);
}
.Button--error.Button--outline {
  border-color: var(--color-error);
  color: var(--color-error);
}
.Button--error.Button--solid {
  background: var(--color-error);
  color: var(--color-white);
}
.Button--warning.Button--outline {
  border-color: var(--color-warning);
  color: var(--color-warning);
}

.Button__prefix,
.Button__suffix {
  display: inline-flex;
  align-items: center;
}

.Button__label {
  display: inline-flex;
  align-items: center;
}

.Button__spinner {
  width: 14px;
  height: 14px;
  border: 2px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.6s linear infinite;
}

.Button--loading {
  pointer-events: none;
}
</style>
