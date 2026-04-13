<script setup lang="ts">
withDefaults(defineProps<{
  as?: string
  size?: 'text-xs' | 'text-sm' | 'text-md' | 'text-lg' | 'text-h6' | 'text-h5' | 'text-h4' | 'text-h3' | 'text-h2' | 'text-h1'
  weight?: 'normal' | 'medium' | 'semibold' | 'bold'
  font?: 'inter' | 'bricolage'
  color?: 'primary' | 'secondary' | 'tertiary' | 'success' | 'error' | 'warning' | 'brand'
}>(), {
  as: 'span',
  size: 'text-md',
  weight: 'normal',
  font: 'inter',
  color: 'primary',
})
</script>

<template>
  <component
    :is="as"
    :class="[
      $style.Typography,
      $style[`Typography--${size}`],
      $style[`Typography--${weight}`],
      $style[`Typography--${font}`],
      $style[`Typography--${color}`],
    ]"
  >
    <slot />
  </component>
</template>

<style module>
.Typography {
  margin: 0;
  padding: 0;
}

/* ── Sizes ── */
.Typography--text-xs  { font-size: var(--text-xs);  line-height: var(--lh-sm);  letter-spacing: var(--ls-sm); }
.Typography--text-sm  { font-size: var(--text-sm);  line-height: var(--lh-sm);  letter-spacing: var(--ls-sm); }
.Typography--text-md  { font-size: var(--text-md);  line-height: var(--lh-md);  letter-spacing: var(--ls-md); }
.Typography--text-lg  { font-size: var(--text-lg);  line-height: var(--lh-lg);  letter-spacing: var(--ls-lg); }
.Typography--text-h6  { font-size: var(--text-h6);  line-height: var(--lh-h6);  letter-spacing: var(--ls-h6); }
.Typography--text-h5  { font-size: var(--text-h5);  line-height: var(--lh-h5);  letter-spacing: var(--ls-h5); }
.Typography--text-h4  { font-size: var(--text-h4);  line-height: var(--lh-h4);  letter-spacing: var(--ls-h4); }
.Typography--text-h3  { font-size: var(--text-h3);  line-height: var(--lh-h3);  letter-spacing: var(--ls-h3); }
.Typography--text-h2  { font-size: var(--text-h2);  line-height: var(--lh-h2);  letter-spacing: var(--ls-h2); }
.Typography--text-h1  { font-size: var(--text-h1);  line-height: var(--lh-h1);  letter-spacing: var(--ls-h1); }

/* ── Weights ── */
.Typography--normal   { font-weight: 400; }
.Typography--medium   { font-weight: 500; }
.Typography--semibold { font-weight: 600; }
.Typography--bold     { font-weight: 700; }

/* ── Fonts ── */
.Typography--inter     { font-family: var(--font-inter); }
.Typography--bricolage { font-family: var(--font-bricolage); }

/* ── Colors ── */
.Typography--primary   { color: var(--color-text-primary); }
.Typography--secondary { color: var(--color-text-secondary); }
.Typography--tertiary  { color: var(--color-text-tertiary); }
.Typography--success   { color: var(--color-success); }
.Typography--error     { color: var(--color-error); }
.Typography--warning   { color: var(--color-warning); }
.Typography--brand     { color: var(--color-brand); }
</style>
