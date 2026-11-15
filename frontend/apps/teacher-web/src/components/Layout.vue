<template>
  <a-layout class="app-layout" :class="{ 'is-collapsed': collapsed }">
    <a-layout-sider
      v-model:collapsed="collapsed"
      :trigger="null"
      collapsible
      :width="224"
      :collapsed-width="72"
      theme="light"
      id="workspace-navigation"
      aria-label="主导航"
      class="app-sider"
      :class="{ 'mobile-open': drawerOpen }"
      :inert="isMobile && !drawerOpen ? true : undefined"
    >
      <div class="sider-header">
        <div v-if="!collapsed" class="brand-lockup">
          <span class="brand-mark">M</span>
          <div><span class="sider-logo">MACT<span> Eval</span></span><span class="brand-caption">课堂教学分析平台</span></div>
        </div>
        <span v-else class="sider-logo-collapsed">M</span>
      </div>
      <div v-if="!collapsed" class="nav-caption">教学工作空间</div>
      <a-menu
        :selectedKeys="selectedKeys"
        mode="inline"
        theme="light"
        :items="menuItems"
        @click="handleMenuClick"
      />
      <div class="sider-footer">
        <div v-if="!collapsed" class="workspace-note"><span class="workspace-symbol"><BookOutlined /></span><div><strong>教师工作空间</strong><span>记录 · 分析 · 教学反思</span></div></div>
        <a-button type="text" class="settings-link" @click="router.push('/profile')" aria-label="个人设置"><SettingOutlined /><span v-if="!collapsed">个人设置</span></a-button>
      </div>
    </a-layout-sider>
    <div v-if="drawerOpen" class="sider-mask" aria-hidden="true" @click="drawerOpen = false" />
    <a-layout>
      <a-layout-header class="app-header">
        <div class="header-left"><a-button
          type="text"
          class="trigger"
          aria-label="打开或关闭导航菜单"
          aria-controls="workspace-navigation"
          :aria-expanded="isMobile ? drawerOpen : !collapsed"
          @click="toggleCollapsed"
        >
          <MenuOutlined />
        </a-button><div class="header-breadcrumb"><span>教学工作空间</span><span class="breadcrumb-divider">/</span><strong>{{ currentPageLabel }}</strong></div></div>
        <div class="header-right">
          <span class="portal-label">教师端</span>
          <a-dropdown v-model:open="userMenuOpen" :trigger="['click']">
            <button type="button" class="user-info" aria-label="用户菜单" aria-haspopup="menu" :aria-expanded="userMenuOpen">
              <a-avatar :size="32">{{ userName?.[0] }}</a-avatar>
              <span class="user-name">{{ userName }}</span>
              <DownOutlined class="user-chevron" />
            </button>
            <template #overlay>
              <a-menu>
                <a-menu-item key="profile" @click="router.push('/profile')">个人设置</a-menu-item>
                <a-menu-item key="logout" @click="handleLogout">退出登录</a-menu-item>
              </a-menu>
            </template>
          </a-dropdown>
        </div>
      </a-layout-header>
      <a-layout-content id="main-content" class="app-content">
        <router-view />
      </a-layout-content>
    </a-layout>
  </a-layout>
</template>

<script setup lang="ts">
import { ref, computed, h, onMounted, onUnmounted, watch } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import {
  MenuOutlined,
  DownOutlined,
  BookOutlined,
  DashboardOutlined,
  FileSearchOutlined,
  SettingOutlined,
  BarChartOutlined,
} from '@ant-design/icons-vue'
import { useAuthStore } from '@/stores/auth'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const collapsed = ref(false)
const drawerOpen = ref(false)
const userMenuOpen = ref(false)
const isMobile = ref(typeof window !== 'undefined' ? window.innerWidth <= 900 : false)

function syncViewport() {
  isMobile.value = window.innerWidth <= 900
  if (!isMobile.value) drawerOpen.value = false
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && drawerOpen.value) drawerOpen.value = false
  if (event.key === 'Escape') userMenuOpen.value = false
}

const userName = computed(() => authStore.user?.name || '用户')

const menuItems = computed(() => {
  const items = [
    { key: '/dashboard', icon: () => h(DashboardOutlined), label: '工作台' },
    { key: '/classroom', icon: () => h(BookOutlined), label: '课程与课堂' },
    { key: '/evaluation', icon: () => h(FileSearchOutlined), label: '课堂分析' },
    { key: '/reports', icon: () => h(BarChartOutlined), label: '评价报告' },
  ]
  if (authStore.isAdmin) {
    items.push({ key: '/admin', icon: () => h(SettingOutlined), label: '管理后台' })
  }
  return items
})

const selectedKeys = computed(() => [route.path])
const currentPageLabel = computed(() => menuItems.value.find(item => item.key === route.path)?.label || '个人设置')

function toggleCollapsed() {
  if (isMobile.value) {
    drawerOpen.value = !drawerOpen.value
    if (drawerOpen.value) collapsed.value = false
    return
  }
  collapsed.value = !collapsed.value
}

function handleMenuClick({ key }: { key: string }) {
  drawerOpen.value = false
  router.push(key)
}

function handleLogout() {
  authStore.logout()
  router.push('/login')
}

watch(() => route.path, () => {
  drawerOpen.value = false
  userMenuOpen.value = false
})

onMounted(() => {
  syncViewport()
  window.addEventListener('resize', syncViewport)
  window.addEventListener('keydown', onKeydown)
  if (authStore.token && !authStore.user) authStore.fetchProfile()
})

onUnmounted(() => {
  window.removeEventListener('resize', syncViewport)
  window.removeEventListener('keydown', onKeydown)
})
</script>

<style scoped>
.app-layout { min-height: 100vh; }
.app-sider { position: fixed; inset: 0 auto 0 0; z-index: 100; overflow: auto; background: #fff !important; border-right: 1px solid var(--color-border); }
.app-sider :deep(.ant-layout-sider-children) { display: flex; flex-direction: column; }
.sider-header { height: 92px; display: flex; align-items: center; padding: 0 24px; }
.brand-lockup { display: flex; align-items: center; gap: 11px; }
.brand-mark, .sider-logo-collapsed { width: 34px; height: 36px; display: grid; place-items: center; color: #fff; background: var(--color-primary); border-radius: 10px 10px 4px 10px; font-size: 19px; font-weight: 750; }
.sider-logo-collapsed { margin: auto; }
.is-collapsed .sider-header { padding: 0; justify-content: center; }
.sider-logo { display: block; font-size: 18px; color: var(--color-ink); font-weight: 750; letter-spacing: -.5px; line-height: 1.3; }
.sider-logo span { font-weight: 450; }
.brand-caption { display: block; color: var(--color-text-secondary); font-size: 12px; margin-top: 3px; letter-spacing: 1px; }
.nav-caption { font-size: 12px; color: var(--color-text-tertiary); margin: 24px 26px 12px; letter-spacing: .5px; }
.app-sider :deep(.ant-menu) { border: 0; padding: 0 12px; background: transparent; }
.app-sider :deep(.ant-menu-item) { margin: 4px 0; border-radius: 7px; color: #627087; height: 43px; line-height: 43px; width: 100%; font-size: 13px; }
.app-sider :deep(.ant-menu-item .anticon) { font-size: 17px; }
.app-sider :deep(.ant-menu-item-selected) { background: #edf1fc; color: #3856bb; font-weight: 650; }
.app-sider :deep(.ant-menu-item:hover) { background: #f4f6fa; }
.sider-footer { margin-top: auto; padding: 22px 16px 18px; }
.workspace-note { display: flex; align-items: center; gap: 10px; background: #f7f9fc; border: 1px solid #e9edf4; border-radius: 8px; padding: 13px 10px; margin-bottom: 14px; }
.workspace-symbol { color: var(--color-primary); font-size: 21px; }
.workspace-note strong, .workspace-note div > span { display: block; }
.workspace-note strong { font-size: 12px; font-weight: 600; }
.workspace-note div > span { color: var(--color-text-secondary); font-size: 12px; margin-top: 4px; }
.settings-link { width: 100%; color: var(--color-text-secondary); text-align: left; font-size: 12px; }
.app-header { background: rgba(255,255,255,.96) !important; padding: 0 32px; display: flex; align-items: center; justify-content: space-between; height: var(--header-height); border-bottom: 1px solid var(--color-border); margin-left: var(--sidebar-width); position: sticky; top: 0; z-index: 90; }
.header-left, .header-right, .header-breadcrumb { display: flex; align-items: center; }
.header-left { gap: 18px; }
.header-breadcrumb { gap: 15px; font-size: 12px; color: var(--color-text-secondary); }
.header-breadcrumb strong { font-weight: 550; color: var(--color-ink); }
.breadcrumb-divider { color: #c5cdd9; }
.trigger { font-size: 16px; color: var(--color-text-secondary); }
.header-right { gap: 22px; }
.portal-label { border-right: 1px solid var(--color-border); padding-right: 22px; color: var(--color-text-secondary); font-size: 12px; line-height: 20px; }
.user-info { display: flex; align-items: center; gap: 9px; cursor: pointer; border: 0; background: transparent; padding: 4px; border-radius: 7px; }
.user-info:hover { background: var(--color-surface-soft); }
.user-info :deep(.ant-avatar) { background: #eaf0fc; color: var(--color-primary); font-weight: 600; }
.user-name { font-size: 12px; color: var(--color-ink); }
.user-chevron { font-size: 9px; color: var(--color-text-tertiary); margin-left: 5px; }
.app-content { margin-left: var(--sidebar-width); padding: 34px 36px 40px; min-height: calc(100vh - var(--header-height)); }
.is-collapsed .app-header, .is-collapsed .app-content { margin-left: var(--sidebar-collapsed-width); }
.sider-mask { position: fixed; inset: 0; background: rgba(29,40,61,.28); z-index: 99; display: none; }
@media (max-width: 1100px) { .app-content { padding: 26px 24px; } }
@media (max-width: 900px) {
  .app-sider { transform: translateX(-100%); transition: transform .2s ease; width: 224px !important; max-width: 84vw !important; min-width: 0 !important; flex: 0 0 224px !important; }
  .app-sider.mobile-open { transform: translateX(0); }
  .sider-mask { display: block; }
  .app-header, .app-content, .is-collapsed .app-header, .is-collapsed .app-content { margin-left: 0; }
  .app-header { padding: 0 16px; }
  .app-content { padding: 24px 16px 32px; }
  .user-name, .portal-label, .header-breadcrumb > span:first-child, .breadcrumb-divider { display: none; }
  .header-left { gap: 10px; }
}
</style>
