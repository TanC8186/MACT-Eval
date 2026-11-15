export const shadows = {
  // Elevation levels for technology dashboard style
  none: 'none',

  sm: '0 1px 2px 0 rgba(0, 0, 0, 0.05)',

  base: [
    '0 1px 3px 0 rgba(0, 0, 0, 0.1)',
    '0 1px 2px -1px rgba(0, 0, 0, 0.1)',
  ].join(', '),

  md: [
    '0 4px 6px -1px rgba(0, 0, 0, 0.1)',
    '0 2px 4px -2px rgba(0, 0, 0, 0.1)',
  ].join(', '),

  lg: [
    '0 10px 15px -3px rgba(0, 0, 0, 0.1)',
    '0 4px 6px -4px rgba(0, 0, 0, 0.1)',
  ].join(', '),

  xl: [
    '0 20px 25px -5px rgba(0, 0, 0, 0.1)',
    '0 8px 10px -6px rgba(0, 0, 0, 0.1)',
  ].join(', '),

  '2xl': '0 25px 50px -12px rgba(0, 0, 0, 0.25)',

  // Dashboard-specific: card hover elevation
  card: [
    '0 1px 3px 0 rgba(0, 0, 0, 0.08)',
    '0 1px 2px -1px rgba(0, 0, 0, 0.06)',
  ].join(', '),

  cardHover: [
    '0 10px 15px -3px rgba(0, 0, 0, 0.1)',
    '0 4px 6px -4px rgba(0, 0, 0, 0.08)',
  ].join(', '),

  // Sidebar-specific shadow (right-side divider)
  sidebar: '1px 0 0 0 #1F2937',

  // Header bottom shadow
  header: '0 1px 0 0 #E2E6EE',

  // Modal / drawer overlay
  modal: '0 25px 50px -12px rgba(0, 0, 0, 0.3)',

  // Focus ring (using brand blue)
  focus: [
    '0 0 0 2px #FFFFFF',
    '0 0 0 4px #315FE8',
  ].join(', '),
} as const

export type ShadowKey = keyof typeof shadows
