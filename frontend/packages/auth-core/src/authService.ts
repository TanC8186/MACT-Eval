import { defineStore } from 'pinia'
import type { User, LoginRequest, RegisterRequest, AuthResponse } from './types'
import {
  getToken,
  setToken,
  clearToken,
  getRefreshToken,
  setRefreshToken,
  clearRefreshToken,
  isTokenExpired,
} from './tokenManager'

const API_BASE = '/api'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    user: null as User | null,
    token: getToken(),
    refreshToken: getRefreshToken(),
  }),

  getters: {
    isAuthenticated: (state): boolean => {
      if (!state.token) return false
      return !isTokenExpired(state.token)
    },
  },

  actions: {
    async login(credentials: LoginRequest): Promise<void> {
      const res = await fetch(`${API_BASE}/auth/login`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(credentials),
      })

      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.message ?? 'Login failed')
      }

      const data: AuthResponse = await res.json()

      setToken(data.token)
      setRefreshToken(data.refreshToken)

      this.token = data.token
      this.refreshToken = data.refreshToken
      this.user = data.user
    },

    async register(payload: RegisterRequest): Promise<void> {
      const res = await fetch(`${API_BASE}/auth/register`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })

      if (!res.ok) {
        const body = await res.json().catch(() => ({}))
        throw new Error(body.message ?? 'Registration failed')
      }

      const data: AuthResponse = await res.json()

      setToken(data.token)
      setRefreshToken(data.refreshToken)

      this.token = data.token
      this.refreshToken = data.refreshToken
      this.user = data.user
    },

    logout(): void {
      clearToken()
      clearRefreshToken()
      this.$reset()
    },

    async refreshAccessToken(): Promise<void> {
      const currentRefresh = this.refreshToken
      if (!currentRefresh) {
        this.logout()
        return
      }

      const res = await fetch(`${API_BASE}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refreshToken: currentRefresh }),
      })

      if (!res.ok) {
        this.logout()
        return
      }

      const data: AuthResponse = await res.json()

      setToken(data.token)
      setRefreshToken(data.refreshToken)

      this.token = data.token
      this.refreshToken = data.refreshToken

      if (data.user) {
        this.user = data.user
      }
    },
  },
})
