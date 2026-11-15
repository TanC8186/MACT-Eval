import axios from 'axios'

/**
 * 单一认证会话模块：access/refresh token 的读写、JWT 解析、单飞续期。
 *
 * 后端 refresh token 是「一次性轮换」的：每次刷新都会消费旧 token 并返回
 * 新的 access + refresh token。因此刷新成功后必须成对保存两者，否则下一次
 * 刷新会提交已消费的旧 token 并被服务端拒绝。
 */

export const TOKEN_KEY = 'token'
export const REFRESH_TOKEN_KEY = 'refreshToken'
export const USER_STORAGE_KEY = 'authUser'
export const AUTH_UPDATED_EVENT = 'mact:auth-updated'

const API_BASE = '/api'

export interface AuthResponse {
  token: string
  refreshToken: string
  userId: string
  role: string
  name: string
  mustChangePassword?: boolean
}

export interface JwtPayload {
  sub?: string
  role?: string
  exp?: number
  [key: string]: unknown
}

function storage(): Storage | null {
  try {
    return typeof localStorage === 'undefined' ? null : localStorage
  } catch {
    return null
  }
}

export function getAccessToken(): string | null {
  return storage()?.getItem(TOKEN_KEY) ?? null
}

export function getRefreshToken(): string | null {
  return storage()?.getItem(REFRESH_TOKEN_KEY) ?? null
}

export function getStoredUser<T = unknown>(): T | null {
  const raw = storage()?.getItem(USER_STORAGE_KEY)
  if (!raw) return null
  try {
    return JSON.parse(raw) as T
  } catch {
    storage()?.removeItem(USER_STORAGE_KEY)
    return null
  }
}

function notifyAuthUpdated(): void {
  if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function') return
  window.dispatchEvent(new CustomEvent(AUTH_UPDATED_EVENT))
}

export function saveSession(auth: AuthResponse): void {
  const s = storage()
  if (!s) return
  s.setItem(TOKEN_KEY, auth.token)
  s.setItem(REFRESH_TOKEN_KEY, auth.refreshToken)
  s.setItem(USER_STORAGE_KEY, JSON.stringify({
    id: auth.userId,
    name: auth.name,
    role: auth.role,
    mustChangePassword: auth.mustChangePassword === true,
  }))
  notifyAuthUpdated()
}

export function saveUser(user: unknown): void {
  const s = storage()
  if (!s) return
  s.setItem(USER_STORAGE_KEY, JSON.stringify(user))
  notifyAuthUpdated()
}

export function clearStoredSession(): void {
  const s = storage()
  if (!s) return
  s.removeItem(TOKEN_KEY)
  s.removeItem(REFRESH_TOKEN_KEY)
  s.removeItem(USER_STORAGE_KEY)
  notifyAuthUpdated()
}

/** 同时兼容标准 Base64 与 JWT 使用的 Base64URL。 */
export function decodeJwtPayload(token: string | null | undefined): JwtPayload | null {
  if (!token) return null
  const parts = token.split('.')
  if (parts.length !== 3) return null
  try {
    let payload = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    while (payload.length % 4 !== 0) payload += '='
    const json = decodeURIComponent(
      Array.prototype.map
        .call(atob(payload), (c: string) => '%' + ('00' + c.charCodeAt(0).toString(16)).slice(-2))
        .join(''),
    )
    const parsed = JSON.parse(json)
    return parsed && typeof parsed === 'object' ? parsed as JwtPayload : null
  } catch {
    return null
  }
}

export function isTokenUsable(token: string | null | undefined): boolean {
  const payload = decodeJwtPayload(token)
  return !!payload && typeof payload.exp === 'number' && Date.now() < payload.exp * 1000
}

let refreshInFlight: Promise<AuthResponse> | null = null

/**
 * 单飞刷新：并发的 401/路由守卫共享同一个 refresh 请求，避免并发消费
 * 同一个 refresh token 导致其中一方失败。
 */
export function refreshSession(): Promise<AuthResponse> {
  if (refreshInFlight) return refreshInFlight

  refreshInFlight = (async () => {
    const refreshToken = getRefreshToken()
    if (!refreshToken) throw new Error('Missing refresh token')
    const response = await axios.post(`${API_BASE}/auth/refresh`, { refreshToken })
    const data = response.data?.data as AuthResponse | undefined
    if (!data?.token || !data?.refreshToken) {
      throw new Error('Invalid refresh response')
    }
    saveSession(data)
    return data
  })()
    .finally(() => {
      refreshInFlight = null
    })

  return refreshInFlight
}

/**
 * 确保有可用的 access token：有效则直接返回；过期/缺失则尝试刷新。
 * 刷新失败会清理本地会话并抛出错误，由调用方决定是否跳转登录。
 */
export async function ensureAccessToken(): Promise<string | null> {
  const token = getAccessToken()
  if (isTokenUsable(token)) return token
  if (!getRefreshToken()) return null
  try {
    const refreshed = await refreshSession()
    return refreshed.token
  } catch (err: any) {
    // 只有服务端明确拒绝（401/403）才说明 refresh token 已失效；
    // 后端短暂重启/网络中断不应删除仍可续期的本地会话。
    const status = err?.response?.status
    if (status === 401 || status === 403) {
      clearStoredSession()
    }
    return null
  }
}

/** 登录后保存会话（登录/注册响应均走这里，保证成对持久化）。 */
export function saveLoginResponse(data: AuthResponse): void {
  saveSession(data)
}

export function loginRedirectUrl(): string {
  const path = typeof window === 'undefined'
    ? '/login'
    : `/login?redirect=${encodeURIComponent(window.location.pathname + window.location.search)}`
  return path
}

/** 只接受站内绝对路径，拒绝 //evil.com、http(s)://、javascript: 等外域跳转。 */
export function sanitizeRedirect(raw: unknown): string | null {
  if (typeof raw !== 'string' || !raw.startsWith('/') || raw.startsWith('//')) return null
  if (raw.includes('\\') || raw.includes('\n') || raw.includes('\r')) return null
  return raw
}
