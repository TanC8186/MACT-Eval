<template>
  <a-card :class="['metric-card', highlight ? 'metric-highlight' : '']" :bordered="false" :size="size">
    <div class="metric-top">
      <div class="metric-title">{{ title }}</div>
      <span v-if="trend !== undefined" class="metric-trend" :class="trendClass">
        <CaretUpOutlined v-if="trend > 0" />
        <CaretDownOutlined v-else-if="trend < 0" />
        {{ Math.abs(trend) }}{{ trendUnit || '%' }}
      </span>
    </div>
    <div class="metric-value-row">
      <span class="metric-value numeric" :class="{ 'is-highlight': highlight }">{{ displayValue }}</span>
      <span v-if="suffix" class="metric-suffix">{{ suffix }}</span>
    </div>
    <div v-if="subtitle" class="metric-subtitle">{{ subtitle }}</div>
    <div v-if="description" class="metric-desc">{{ description }}</div>
  </a-card>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { CaretUpOutlined, CaretDownOutlined } from '@ant-design/icons-vue'

const props = defineProps<{
  title: string
  value: number | string
  suffix?: string
  prefix?: string
  precision?: number
  trend?: number
  trendUnit?: string
  subtitle?: string
  description?: string
  highlight?: boolean
  size?: 'default' | 'small'
}>()

const displayValue = computed(() => {
  if (typeof props.value === 'number') return props.value
  return props.value || '—'
})

const trendClass = computed(() => {
  if (!props.trend) return 'is-flat'
  return props.trend > 0 ? 'is-up' : 'is-down'
})

</script>

<style scoped>
.metric-card { height: 100%; }
.metric-highlight { border-color: #ced9f1 !important; }
.metric-top { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.metric-title { color: var(--color-text-secondary); font-size: 12px; }
.metric-trend { display: inline-flex; align-items: center; gap: 2px; font-size: 12px; }
.metric-trend.is-up { color: var(--color-success); }
.metric-trend.is-down { color: var(--color-error); }
.metric-trend.is-flat { color: var(--color-text-secondary); }
.metric-value-row { display: flex; align-items: baseline; gap: 7px; margin-top: 16px; }
.metric-value { font-size: 34px; line-height: 1.1; font-weight: 650; letter-spacing: -.7px; color: var(--color-ink); }
.metric-value.is-highlight { color: var(--color-primary); }
.metric-suffix { font-size: 12px; color: var(--color-text-secondary); }
.metric-subtitle { font-size: 12px; color: var(--color-text-secondary); margin-top: 17px; }
.metric-desc { font-size: 12px; color: var(--color-text-secondary); margin-top: 8px; line-height: 1.7; }
</style>
