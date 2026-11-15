/**
 * Format ISO date string to locale date
 */
export function formatDate(isoString: string, locale = 'zh-CN'): string {
  return new Date(isoString).toLocaleDateString(locale)
}

/**
 * Format ISO date string to locale date + time
 */
export function formatDateTime(isoString: string, locale = 'zh-CN'): string {
  return new Date(isoString).toLocaleString(locale)
}

/**
 * Format ISO date string to time only
 */
export function formatTime(isoString: string, locale = 'zh-CN'): string {
  return new Date(isoString).toLocaleTimeString(locale, { hour: '2-digit', minute: '2-digit' })
}

/**
 * Get ISO date string for today
 */
export function today(): string {
  return new Date().toISOString().split('T')[0]
}

/**
 * Get ISO date string for days ago
 */
export function daysAgo(days: number): string {
  const d = new Date()
  d.setDate(d.getDate() - days)
  return d.toISOString().split('T')[0]
}
