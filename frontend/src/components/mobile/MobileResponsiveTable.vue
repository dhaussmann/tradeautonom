<script setup lang="ts">
import { computed } from 'vue'
import Typography from '@/components/ui/Typography.vue'

export interface TableColumn {
  key: string
  label: string
  width?: string
  align?: 'left' | 'center' | 'right'
  format?: (value: any, row: any) => string
  component?: 'chip' | 'text' | 'custom'
  color?: (value: any, row: any) => string
}

interface Props {
  columns: TableColumn[]
  data: any[]
  keyField: string
  loading?: boolean
  emptyText?: string
  cardTitleField?: string
  cardSubtitleField?: string
}

const props = withDefaults(defineProps<Props>(), {
  loading: false,
  emptyText: 'No data available',
})

function formatValue(column: TableColumn, row: any): string {
  const value = row[column.key]
  if (column.format) {
    return column.format(value, row)
  }
  return value != null ? String(value) : '—'
}

type ColorType = 'primary' | 'secondary' | 'tertiary' | 'success' | 'error' | 'warning' | 'brand'

function getValueColor(column: TableColumn, row: any): ColorType {
  if (column.color) {
    const color = column.color(row[column.key], row)
    // Only return valid color names
    const validColors: ColorType[] = ['primary', 'secondary', 'tertiary', 'success', 'error', 'warning', 'brand']
    if (validColors.includes(color as ColorType)) {
      return color as ColorType
    }
  }
  return 'primary'
}

const hasData = computed(() => props.data.length > 0)
</script>

<template>
  <div :class="$style.container">
    <!-- Desktop: Traditional Table -->
    <div :class="$style.desktopTable">
      <div :class="$style.tableWrap">
        <table :class="$style.table">
          <thead>
            <tr>
              <th
                v-for="col in columns"
                :key="col.key"
                :style="{ width: col.width, textAlign: col.align || 'left' }"
                :class="$style.th"
              >
                <Typography size="text-xs" weight="semibold" color="tertiary">
                  {{ col.label }}
                </Typography>
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="row in data" :key="String(row[keyField])" :class="$style.tr">
              <td
                v-for="col in columns"
                :key="col.key"
                :style="{ textAlign: col.align || 'left' }"
                :class="$style.td"
              >
                <Typography
                  size="text-sm"
                  :weight="col.component === 'chip' ? 'medium' : 'normal'"
                  :color="getValueColor(col, row)"
                >
                  {{ formatValue(col, row) }}
                </Typography>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Mobile: Card Layout -->
    <div :class="$style.mobileCards">
      <div
        v-for="row in data"
        :key="String(row[keyField])"
        :class="$style.card"
      >
        <!-- Card Header (Title + Subtitle) -->
        <div v-if="cardTitleField" :class="$style.cardHeader">
          <Typography size="text-md" weight="semibold">
            {{ row[cardTitleField] }}
          </Typography>
          <Typography v-if="cardSubtitleField" size="text-xs" color="tertiary">
            {{ row[cardSubtitleField] }}
          </Typography>
        </div>

        <!-- Card Body (Key-Value pairs) -->
        <div :class="$style.cardBody">
          <div
            v-for="col in columns.filter(c => c.key !== cardTitleField && c.key !== cardSubtitleField)"
            :key="col.key"
            :class="$style.cardRow"
          >
            <Typography size="text-xs" color="tertiary" :class="$style.cardLabel">
              {{ col.label }}
            </Typography>
            <Typography
              size="text-sm"
              :weight="col.component === 'chip' ? 'medium' : 'normal'"
              :color="getValueColor(col, row)"
              :align="col.align || 'right'"
            >
              {{ formatValue(col, row) }}
            </Typography>
          </div>
        </div>
      </div>
    </div>

    <!-- Loading State -->
    <div v-if="loading" :class="$style.loading">
      <Typography color="secondary">Loading...</Typography>
    </div>

    <!-- Empty State -->
    <div v-else-if="!hasData" :class="$style.empty">
      <Typography color="tertiary">{{ emptyText }}</Typography>
    </div>
  </div>
</template>

<style module>
.container {
  width: 100%;
}

/* Desktop Table */
.desktopTable {
  display: block;
}

.tableWrap {
  border-radius: var(--radius-lg);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  overflow-x: auto;
}

.table {
  width: 100%;
  border-collapse: collapse;
  min-width: 600px;
}

.th {
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
  text-transform: uppercase;
  letter-spacing: 0.04em;
  white-space: nowrap;
}

.td {
  padding: var(--space-2) var(--space-3);
  border-bottom: 1px solid var(--color-stroke-divider);
  white-space: nowrap;
}

.tr:last-child .td {
  border-bottom: none;
}

.tr:hover .td {
  background: var(--color-white-4);
}

/* Mobile Cards */
.mobileCards {
  display: none;
  flex-direction: column;
  gap: var(--space-3);
}

.card {
  border-radius: var(--radius-xl);
  border: 1px solid var(--color-stroke-divider);
  background: var(--color-white-2);
  overflow: hidden;
}

.cardHeader {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--space-4);
  border-bottom: 1px solid var(--color-stroke-divider);
  background: var(--color-white-4);
}

.cardBody {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: var(--space-3);
  padding: var(--space-4);
}

.cardRow {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cardLabel {
  text-transform: uppercase;
  letter-spacing: 0.02em;
}

/* Loading & Empty States */
.loading,
.empty {
  padding: var(--space-10) 0;
  text-align: center;
}

/* Mobile Breakpoint */
@media (max-width: 767px) {
  .desktopTable {
    display: none;
  }

  .mobileCards {
    display: flex;
  }

  .tableWrap {
    overflow-x: visible;
  }
}

/* Small mobile: single column cards */
@media (max-width: 480px) {
  .cardBody {
    grid-template-columns: 1fr;
  }
}
</style>
