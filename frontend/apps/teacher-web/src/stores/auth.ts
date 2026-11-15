import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import apiClient from '@/api/client'
import { endpoints } from '@/api/endpoints'
import {
  AUTH_UPDATED_EVENT,
  ensureAccessToken,
  getAccessToken,
  getRefreshToken,
  getStoredUser,
  refreshSession,
  saveLoginResponse,
  saveUser,
  clearStoredSession,
} from '@/api/authSession'
import type { AuthResponse, UserProfile } from '@/types/auth'

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string | null>(getAccessToken())
  const refreshToken = ref<string | null>(getRefreshToken())
  const user = ref<UserProfile | null>(getStoredUser<UserProfile>())
  const loading = ref(false)

  const isAuthenticated = computed(() => !!token.value)
  const isTeacher = computed(() => user.value?.role === 'teacher')
  const isStudent = computed(() => user.value?.role === 'student')
  const isAdmin = computed(() => user.value?.role === 'admin')

  /** 从 localStorage 重新同步（刷新响应、跨标签页 storage 事件都会触发）。 */
  function syncFromStorage() {
    token.value = getAccessToken()
    refreshToken.value = getRefreshToken()
    user.value = getStoredUser<UserProfile>()
  }

  if (typeof window !== 'undefined') {
    window.addEventListener(AUTH_UPDATED_EVENT, syncFromStorage)
    window.addEventListener('storage', syncFromStorage)
  }

  async function login(username: string, password: string) {
    loading.value = true
    try {
      const res = await apiClient.post(endpoints.auth.login, { username, password })
      const data: AuthResponse = res.data.data
      saveLoginResponse(data)
      syncFromStorage()
    } finally {
      loading.value = false
    }
  }

  async function register(username: string, name: string, password: string) {
    loading.value = true
    try {
      const res = await apiClient.post(endpoints.auth.register, { username, password, name, role: 'teacher' })
      const data: AuthResponse = res.data.data
      saveLoginResponse(data)
      syncFromStorage()
    } finally {
      loading.value = false
    }
  }

  /** 修改密码后服务端返回新令牌（不再带强制改密声明），立即替换本地会话。 */
  function applySession(data: AuthResponse) {
    saveLoginResponse(data)
    syncFromStorage()
  }

  async function refreshAccessToken() {
    const data = await refreshSession()
    syncFromStorage()
    return data.token
  }

  /**
   * 路由守卫使用：access token 可用则直接返回；否则尝试单飞刷新。
   * 刷新失败时会清理会话并返回 null（不清除仍可续期的 refresh token）。
   */
  async function ensureSession(): Promise<string | null> {
    const tokenValue = await ensureAccessToken()
    syncFromStorage()
    return tokenValue
  }

  async function logout() {
    const rt = refreshToken.value
    clearStoredSession()
    syncFromStorage()
    if (rt) {
      try {
        await apiClient.post(endpoints.auth.logout, { refreshToken: rt })
      } catch {
        // best-effort：服务端撤销失败不影响本地登出
      }
    }
  }

  async function fetchProfile() {
    try {
      const res = await apiClient.get(endpoints.auth.profile)
      user.value = res.data.data
      saveUser(user.value)
    } catch (err: unknown) {
      // 仅 401 时强制登出；网络抖动/5xx 保留本地会话，避免弱网误踢
      const status = (err as { response?: { status?: number } })?.response?.status
      if (status === 401 || status === 403) {
        logout()
      }
    }
  }

  return {
    token, refreshToken, user, loading,
    isAuthenticated, isTeacher, isStudent, isAdmin,
    login, register, logout, fetchProfile, refreshAccessToken, ensureSession, syncFromStorage, applySession,
  }
})
