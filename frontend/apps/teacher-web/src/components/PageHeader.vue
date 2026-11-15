<template>
  <div class="page-header">
    <div class="title-block">
      <div v-if="eyebrow" class="eyebrow">{{ eyebrow }}</div>
      <h1 class="page-title">{{ title }}</h1>
      <p v-if="description" class="page-desc">{{ description }}</p>
    </div>
    <div v-if="$slots.extra" class="page-extra">
      <slot name="extra" />
    </div>
  </div>
</template>

<script setup lang="ts">
defineProps<{ title: string; description?: string; eyebrow?: string }>()
</script>

<style scoped>
.page-header { display: flex; align-items: flex-end; justify-content: space-between; gap: 24px; margin-bottom: 28px; padding: 4px 0; }
.title-block { min-width: 0; }
.eyebrow { color: var(--color-text-secondary); font-size: 10px; font-weight: 550; letter-spacing: .12em; text-transform: uppercase; margin-bottom: 9px; }
.page-title { margin: 0; color: var(--color-ink, #152321); font-size: 28px; line-height: 1.2; font-weight: 720; letter-spacing: -.035em; }
.page-desc { margin: 8px 0 0; color: var(--color-text-secondary, #6b7b78); font-size: 14px; line-height: 1.5; }
.page-extra { flex-shrink: 0; }
@media (max-width: 760px) { .page-header { align-items: flex-start; flex-direction: column; gap: 14px; } .page-extra { width: 100%; } }
</style>
