<template>
  <a-card :title="title" :extra="extraSlot ? $slots.extra : undefined" size="small">
    <template v-if="$slots.extra && !extraSlot" #extra>
      <slot name="extra" />
    </template>

    <DataState :state="state" variant="card" :retryable="true" @retry="$emit('retry')" :empty-text="emptyText">
      <div v-if="type === 'chart'" :style="{ height: height + 'px' }">
        <slot />
      </div>
      <div v-else>
        <slot />
      </div>
    </DataState>
  </a-card>
</template>

<script setup lang="ts">
import DataState from './DataState.vue'

defineProps<{
  title: string
  state: 'idle' | 'loading' | 'error' | 'empty' | 'success' | 'demo'
  type?: 'chart' | 'table'
  height?: number
  emptyText?: string
  extraSlot?: boolean
}>()

defineEmits<{
  retry: []
}>()
</script>
