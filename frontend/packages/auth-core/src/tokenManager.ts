/** Aligned with frontend/apps/*/src/{stores,api,router} which use localStorage 'token' */
const TOKEN_KEY = 'token'
const REFRESH_TOKEN_KEY = 'auth_refresh_token'

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token)
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

export function getRefreshToken(): string | null {
  return localStorage.getItem(REFRESH_TOKEN_KEY)
}

export function setRefreshToken(token: string): void {
  localStorage.setItem(REFRESH_TOKEN_KEY, token)
}

export function clearRefreshToken(): void {
  localStorage.removeItem(REFRESH_TOKEN_KEY)
}

/**
 * Decodes the JWT payload (without verifying the signature) and checks
 * whether the `exp` claim is in the past.
 *
 * Returns `true` if the token is expired, malformed, or missing an `exp` claim.
 */
export function isTokenExpired(token: string): boolean {
  try {
    const parts = token.split('.')
    if (parts.length !== 3) return true

    const payload = JSON.parse(atob(parts[1]))
    if (payload.exp == null) return true

    return Date.now() >= payload.exp * 1000
  } catch {
    return true
  }
}
