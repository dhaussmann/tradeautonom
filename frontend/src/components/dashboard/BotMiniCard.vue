<script setup lang="ts">
import Typography from '@/components/ui/Typography.vue'
import Chip from '@/components/ui/Chip.vue'
import Button from '@/components/ui/Button.vue'
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
  navigate: [botId: string]
}>()

function stateVariant(state: string): 'success' | 'warning' | 'error' | 'neutral' | 'info' | 'brand' {
  switch (state) {
    case 'HOLDING': return 'success'
    case 'ENTERING': return 'brand'
    case 'EXITING': return 'warning'
    default: return 'neutral'
  }
}
</script>

<template>
  <div :class="$style.card" @click="emit('navigate', bot.bot_id)">
    <div :class="$style.top">
      <div :class="$style.nameRow">
        <Typography size="text-md" weight="semibold">{{ bot.bot_id }}</Typography>
        <Chip :variant="stateVariant(bot.state)" size="sm">{{ bot.state }}</Chip>
      </div>
      <Typography size="text-xs" color="secondary">
        {{ bot.long_exchange }} ↔ {{ bot.short_exchange }}
      </Typography>
      <Typography size="text-xs" color="tertiary">
        {{ bot.instrument_a }} · {{ bot.quantity }} qty
      </Typography>
    </div>
    <div :class="$style.actions" @click.stop>
      <Button
        v-if="!bot.is_running"
        variant="ghost"
        size="sm"
        color="success"
        :loading="actionLoading === 'start'"
        @click="emit('start', bot.bot_id)"
      >Start</Button>
      <Button
        v-if="bot.is_running"
        variant="ghost"
        size="sm"
        color="warning"
        :loading="actionLoading === 'stop'"
        @click="emit('stop', bot.bot_id)"
      >Stop</Button>
      <Button
        v-if="bot.is_running"
        variant="ghost"
        size="sm"
        color="error"
        :loading="actionLoading === 'kill'"
        @click="emit('kill', bot.bot_id)"
      >Kill</Button>
      <Button
        v-if="!bot.is_running"
        variant="ghost"
        size="sm"
        color="error"
        :loading="actionLoading === 'delete'"
        @click="emit('delete', bot.bot_id)"
      >Delete</Button>
    </div>
  </div>
</template>

<style module>
.card {
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  padding: var(--space-3) var(--space-4);
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  cursor: pointer;
  transition: background 0.15s ease, border-color 0.15s ease;
}

.card:hover {
  background: var(--color-white-4);
  border-color: var(--color-stroke-primary);
}

.top {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
}

.nameRow {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
}

.actions {
  display: flex;
  gap: var(--space-1);
  margin-top: var(--space-1);
}
</style>
