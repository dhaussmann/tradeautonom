<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import Typography from '@/components/ui/Typography.vue'
import Button from '@/components/ui/Button.vue'
import Chip from '@/components/ui/Chip.vue'
import StatusDot from '@/components/ui/StatusDot.vue'
import type { BotSummary } from '@/types/bot'

const props = defineProps<{
  bot: BotSummary
  actionLoading?: string | null
}>()

const emit = defineEmits<{
  start: [botId: string]
  stop: [botId: string]
  kill: [botId: string]
  delete: [botId: string]
}>()

const router = useRouter()

const stateChipVariant = computed(() => {
  switch (props.bot.state) {
    case 'ENTERING': return 'brand'
    case 'HOLDING':  return 'success'
    case 'EXITING':  return 'warning'
    default:         return 'neutral'
  }
})

const isActive = computed(() => props.bot.state !== 'IDLE')
const canStart = computed(() => props.bot.state === 'IDLE')
const canStop = computed(() => props.bot.is_running || props.bot.state === 'HOLDING')
const canDelete = computed(() => props.bot.state === 'IDLE')

function exchangeColor(exchange: string): string {
  const map: Record<string, string> = {
    extended: 'var(--color-extended-brand)',
    grvt: 'var(--color-grvt-brand)',
    variational: 'var(--color-variational-brand)',
    nado: 'var(--color-nado-brand)',
  }
  return map[exchange] || 'var(--color-text-secondary)'
}

function goToDetail() {
  router.push({ name: 'bot-detail', params: { botId: props.bot.bot_id } })
}
</script>

<template>
  <div
    :class="[$style.container, isActive && $style['container--active']]"
    @click="goToDetail"
  >
    <!-- Active border animation -->
    <div v-if="isActive" :class="$style.borderGlow" />

    <!-- Header row -->
    <div :class="$style.header">
      <div :class="$style.titleRow">
        <StatusDot :active="bot.is_running" :color="isActive ? 'brand' : 'neutral'" />
        <Typography size="text-lg" weight="semibold">{{ bot.bot_id }}</Typography>
        <Chip :variant="stateChipVariant" size="sm">{{ bot.state }}</Chip>
      </div>
      <Typography size="text-sm" color="secondary">{{ bot.quantity }} qty</Typography>
    </div>

    <!-- Exchanges -->
    <div :class="$style.exchanges">
      <div :class="$style.leg">
        <Chip variant="long" size="sm">LONG</Chip>
        <Typography size="text-sm" :style="{ color: exchangeColor(bot.long_exchange) }">
          {{ bot.long_exchange }}
        </Typography>
        <Typography size="text-xs" color="tertiary">{{ bot.instrument_a }}</Typography>
      </div>
      <div :class="$style.leg">
        <Chip variant="short" size="sm">SHORT</Chip>
        <Typography size="text-sm" :style="{ color: exchangeColor(bot.short_exchange) }">
          {{ bot.short_exchange }}
        </Typography>
        <Typography size="text-xs" color="tertiary">{{ bot.instrument_b }}</Typography>
      </div>
    </div>

    <!-- Controls -->
    <div :class="$style.controls" @click.stop>
      <Button
        v-if="canStart"
        variant="solid"
        color="success"
        size="sm"
        :loading="actionLoading === 'start'"
        @click="emit('start', bot.bot_id)"
      >Start</Button>
      <Button
        v-if="canStop"
        variant="outline"
        color="warning"
        size="sm"
        :loading="actionLoading === 'stop'"
        @click="emit('stop', bot.bot_id)"
      >Stop</Button>
      <Button
        v-if="isActive"
        variant="outline"
        color="error"
        size="sm"
        :loading="actionLoading === 'kill'"
        @click="emit('kill', bot.bot_id)"
      >Kill</Button>
      <Button
        v-if="canDelete"
        variant="ghost"
        color="error"
        size="sm"
        :loading="actionLoading === 'delete'"
        @click="emit('delete', bot.bot_id)"
      >Delete</Button>
    </div>
  </div>
</template>

<style module>
.container {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  padding: var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-3);
  position: relative;
  overflow: visible;
  transition: box-shadow var(--duration-xl);
  cursor: pointer;
}

.container:hover {
  border-color: var(--color-stroke-primary);
}

.container--active {
  box-shadow: 0 0 12px rgba(31, 210, 79, 0.1);
}

.borderGlow {
  position: absolute;
  inset: -1px;
  border-radius: 17px;
  padding: 1px;
  background: conic-gradient(
    from var(--border-angle, 0deg),
    transparent 0%,
    var(--color-brand) 25%,
    transparent 40%,
    transparent 72%,
    var(--color-brand) 85%,
    transparent 100%
  );
  animation: border-rotate 3s linear infinite;
  -webkit-mask: linear-gradient(#fff 0 0) content-box, linear-gradient(#fff 0 0);
  mask-composite: exclude;
  pointer-events: none;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.titleRow {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}

.exchanges {
  display: flex;
  gap: var(--space-4);
}

.leg {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex: 1;
}

.controls {
  display: flex;
  gap: var(--space-2);
  padding-top: var(--space-2);
  border-top: 1px solid var(--color-stroke-divider);
}
</style>
