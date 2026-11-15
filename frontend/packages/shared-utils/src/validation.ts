export function isNonEmpty(value: string | null | undefined): boolean {
  return value != null && value.trim().length > 0
}

export function isValidEmail(value: string): boolean {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)
}

export function isInRange(value: number, min: number, max: number): boolean {
  return value >= min && value <= max
}

export function isValidScore(value: number, max: number): boolean {
  return value >= 0 && value <= max
}

export function hasLength(value: string, min: number, max: number): boolean {
  return value.length >= min && value.length <= max
}
