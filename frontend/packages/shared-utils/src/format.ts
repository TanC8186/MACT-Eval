/**
 * Format a ratio (0-1) as percentage string
 */
export function formatPercent(ratio: number, decimals = 0): string {
  return `${(ratio * 100).toFixed(decimals)}%`
}

/**
 * Format a number with unit suffix
 */
export function formatMetric(value: number, unit = ''): string {
  if (unit) return `${value}${unit}`
  return String(value)
}

/**
 * Format a score out of a max
 */
export function formatScore(score: number, max: number): string {
  return `${score}/${max}`
}

/**
 * Truncate text with ellipsis
 */
export function truncate(text: string, maxLength: number): string {
  if (text.length <= maxLength) return text
  return text.slice(0, maxLength) + '…'
}
