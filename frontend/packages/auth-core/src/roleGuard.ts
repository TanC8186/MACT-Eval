/**
 * Map a user role to its corresponding landing-page URL path.
 */
export function roleToApp(role: string): string {
  const routes: Record<string, string> = {
    teacher: '/dashboard',
    student: '/classroom',
    admin: '/admin',
  }
  return routes[role] ?? '/login'
}

/**
 * Redirect the browser to the landing page for the given role.
 */
export function redirectToApp(role: string): void {
  window.location.href = roleToApp(role)
}

function decodePayload(token: string): Record<string, unknown> | null {
  try {
    const parts = token.split('.')
    if (parts.length !== 3) return null
    return JSON.parse(atob(parts[1])) as Record<string, unknown>
  } catch {
    return null
  }
}

/**
 * Decode the JWT from localStorage and return the `role` claim,
 * or `null` if the token is missing or malformed.
 */
export function getRoleFromToken(): string | null {
  const token = localStorage.getItem('token')
  if (!token) return null

  const payload = decodePayload(token)
  if (!payload) return null

  const role = payload.role
  return typeof role === 'string' ? role : null
}
