<template>
  <div>
    <!-- Loading -->
    <div v-if="state === 'loading'">
      <a-skeleton v-if="variant === 'card'" active :paragraph="{ rows: 4 }" />
      <a-skeleton v-else-if="variant === 'table'" active :paragraph="{ rows: 6 }" />
      <a-spin v-else :tip="loadingText" />
    </div>

    <!-- Error -->
    <a-result
      v-else-if="state === 'error'"
      status="error"
      :title="errorTitle || '请求失败'"
      :sub-title="errorMessage || '请稍后重试'"
    >
      <template #extra>
        <a-button type="primary" @click="$emit('retry')" v-if="retryable">重试</a-button>
        <a-button @click="$router.back()" v-else>返回</a-button>
      </template>
    </a-result>

    <!-- Empty -->
    <a-empty
      v-else-if="state === 'empty'"
      :description="emptyText || '暂无数据'"
    >
      <template #children>
        <a-button v-if="emptyActionText" type="primary" @click="$emit('emptyAction')">
          {{ emptyActionText }}
        </a-button>
      </template>
    </a-empty>

    <!-- Demo Mode -->
    <a-alert
      v-if="isDemo"
      message="演示模式"
      :description="demoDescription || '当前展示的数据为演示数据，不代表真实课堂记录。'"
      type="warning"
      show-icon
      closable
      style="margin-bottom: 16px"
    />

    <!-- Content -->
    <slot v-if="state === 'success' || state === 'demo'" />
  </div>
</template>

<script setup lang="ts">
defineProps<{
  state: 'idle' | 'loading' | 'error' | 'empty' | 'success' | 'demo'
  variant?: 'card' | 'table' | 'inline'
  errorTitle?: string
  errorMessage?: string
  emptyText?: string
  emptyActionText?: string
  loadingText?: string
  retryable?: boolean
  isDemo?: boolean
  demoDescription?: string
}>()

defineEmits<{
  retry: []
  emptyAction: []
}>()
</script>
