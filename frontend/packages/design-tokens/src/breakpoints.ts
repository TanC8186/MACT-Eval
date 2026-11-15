export const breakpoints = {
  // Min-width breakpoints (mobile-first)
  sm: 640,
  md: 768,
  lg: 1024,
  xl: 1280,
  '2xl': 1536,
} as const

export const mediaQuery = {
  sm: `@media (min-width: ${breakpoints.sm}px)`,
  md: `@media (min-width: ${breakpoints.md}px)`,
  lg: `@media (min-width: ${breakpoints.lg}px)`,
  xl: `@media (min-width: ${breakpoints.xl}px)`,
  '2xl': `@media (min-width: ${breakpoints['2xl']}px)`,

  // Max-width queries for cascade-down overrides
  downSm: `@media (max-width: ${breakpoints.sm - 1}px)`,
  downMd: `@media (max-width: ${breakpoints.md - 1}px)`,
  downLg: `@media (max-width: ${breakpoints.lg - 1}px)`,
  downXl: `@media (max-width: ${breakpoints.xl - 1}px)`,

  // Dark mode
  dark: '@media (prefers-color-scheme: dark)',
} as const

export type BreakpointKey = keyof typeof breakpoints
export type MediaQueryKey = keyof typeof mediaQuery
