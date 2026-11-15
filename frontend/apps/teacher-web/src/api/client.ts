import axios from 'axios'
import type { AxiosInstance, InternalAxiosRequestConfig, AxiosResponse, AxiosError } from 'axios'
import {
  clearStoredSession,
  getAccessToken,
  loginRedirectUrl,
  refreshSession,
} from './authSession'

const API_BASE = '/api'

const apiClient: AxiosInstance = axios.create({
  baseURL: API_BASE,
  timeout: 30000,
  headers: {
    'Content-Type': 'application/json',
  },
})

apiClient.interceptors.request.use(
  (config: InternalAxiosRequestConfig) => {
    // 每次请求都从最新存储读取：刷新成功后其他标签页/组件立即使用新 access token。
    const token = getAccessToken()
    if (token) {
      config.headers.Authorization = `Bearer ${token}`
    }
    return config
  },
  (error: AxiosError) => Promise.reject(error),
)

let redirectingToLogin = false

function redirectToLogin(): void {
  if (typeof window === 'undefined') return
  if (window.location.pathname.includes('/login') || redirectingToLogin) return
  redirectingToLogin = true
  window.location.href = loginRedirectUrl()
}

apiClient.interceptors.response.use(
  (response: AxiosResponse) => response,
  async (error: AxiosError) => {
    const originalRequest = error.config as (InternalAxiosRequestConfig & { _retry?: boolean }) | undefined
    const isAuthRequest = originalRequest?.url?.startsWith('/auth/')

    if (error.response?.status === 401 && originalRequest && !originalRequest._retry && !isAuthRequest) {
      originalRequest._retry = true
      const tokenBeforeRefresh = getAccessToken()
      try {
        // 单飞刷新：并发 401 只触发一次 refresh；同时成对保存轮换后的两种 token。
        const data = await refreshSession()
        originalRequest.headers = originalRequest.headers ?? {}
        originalRequest.headers.Authorization = `Bearer ${data.token}`
        return apiClient.request(originalRequest)
      } catch (refreshError: any) {
        // 多标签页轮换：另一个标签页可能已经刷新成功，直接使用它写入的新 token，
        // 不能因为本标签页提交了旧 refresh token 就把整个会话清掉。
        const latest = getAccessToken()
        if (latest && latest !== tokenBeforeRefresh) {
          originalRequest.headers = originalRequest.headers ?? {}
          originalRequest.headers.Authorization = `Bearer ${latest}`
          return apiClient.request(originalRequest)
        }
        const status = refreshError?.response?.status
        if (status === 401 || status === 403) {
          clearStoredSession()
          redirectToLogin()
        }
        // 网络错误：保留 refresh token，等待网络恢复后由路由/下一次请求重试。
        return Promise.reject(error)
      }
    }

    if (error.response?.status === 401 && !isAuthRequest) {
      clearStoredSession()
      redirectToLogin()
    }
    return Promise.reject(error)
  },
)

export default apiClient
