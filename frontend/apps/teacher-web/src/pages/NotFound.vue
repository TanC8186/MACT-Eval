<template>
  <div class="not-found">
    <a-result
      status="404"
      title="页面不存在"
      sub-title="您访问的页面不存在或已被移除"
    >
      <template #extra>
        <a-button type="primary" @click="$router.push('/dashboard')">
          返回工作台
        </a-button>
      </template>
    </a-result>
  </div>
</template>

<script setup lang="ts">
</script>

<style scoped>
.not-found {
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--color-bg);
}
</style>
