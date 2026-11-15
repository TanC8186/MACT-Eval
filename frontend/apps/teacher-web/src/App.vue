<template>
  <a-config-provider :locale="locale" :theme="theme">
    <router-view />
  </a-config-provider>
</template>

<script setup lang="ts">
import zhCN from 'ant-design-vue/es/locale/zh_CN'
const locale = zhCN
const theme = {
  token: {
    colorPrimary: '#4263d5',
    colorInfo: '#4263d5',
    colorText: '#243247',
    colorTextSecondary: '#68758a',
    colorBorder: '#dfe5ed',
    borderRadius: 8,
    controlHeight: 36,
    fontFamily: '"Segoe UI", "Microsoft YaHei", "PingFang SC", sans-serif',
  },
}
</script>
