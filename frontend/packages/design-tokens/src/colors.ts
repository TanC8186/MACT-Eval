export const colors = {
  // Brand
  brand: {
    blue: '#2B54E8',
    purple: '#6941C6',
    blueLight: '#EEF2FF',
    purpleLight: '#F0EBFF',
  },

  // Neutrals
  neutral: {
    bg: '#F3F5F9',
    surface: '#FFFFFF',
    border: '#E2E8F0',
    divider: '#EEF2F7',
  },

  // Navigation
  nav: {
    bg: '#0A1020',
    bgHover: 'rgba(255,255,255,0.05)',
    text: '#94A3B8',
    textActive: '#C7D2FE',
    activeIndicator: '#2B54E8',
  },

  // Text
  text: {
    primary: '#0F172A',
    secondary: '#64748B',
    disabled: '#94A3B8',
    inverse: '#FFFFFF',
  },

  // Semantic
  semantic: {
    success: '#059669',
    successBg: '#ECFDF5',
    warning: '#D97706',
    warningBg: '#FFFBEB',
    error: '#E11D48',
    errorBg: '#FFF1F2',
    info: '#2B54E8',
    infoBg: '#EEF2FF',
  },

  // Chart series (separate from semantic colors)
  chart: {
    blue: '#2B54E8',
    purple: '#6941C6',
    cyan: '#06B6D4',
    teal: '#0D9488',
    orange: '#EA580C',
    pink: '#DB2777',
    neutral1: '#64748B',
    neutral2: '#94A3B8',
  },

  // Status for behavior/attendance
  status: {
    present: '#059669',
    late: '#D97706',
    absent: '#E11D48',
    excused: '#64748B',
    pending: '#94A3B8',
    upright: '#2B54E8',
    limited: '#D97706',
    unavailable: '#94A3B8',
  },
} as const

export type ColorKey = keyof typeof colors
