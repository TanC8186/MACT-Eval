export const typography = {
  fontFamily: {
    sans: [
      'Inter',
      '-apple-system',
      'BlinkMacSystemFont',
      '"Segoe UI"',
      'Roboto',
      '"Helvetica Neue"',
      'Arial',
      '"Noto Sans SC"',
      'sans-serif',
    ].join(', '),
    mono: [
      '"JetBrains Mono"',
      '"Fira Code"',
      'ui-monospace',
      'SFMono-Regular',
      'Consolas',
      '"Liberation Mono"',
      'Menlo',
      'monospace',
    ].join(', '),
  },

  fontWeight: {
    regular: 400,
    medium: 500,
    semibold: 600,
    bold: 700,
  },

  // Desktop-first typography scale
  fontSize: {
    xs: '0.75rem',     // 12px — captions, badges
    sm: '0.8125rem',   // 13px — secondary text, metadata
    base: '0.875rem',  // 14px — body, table content
    md: '0.9375rem',   // 15px — large body
    lg: '1rem',        // 16px — sidebar / section headings
    xl: '1.125rem',    // 18px — card titles
    '2xl': '1.25rem',  // 20px — page section headings
    '3xl': '1.5rem',   // 24px — page titles
    '4xl': '1.875rem', // 30px — dashboard hero metrics
    '5xl': '2.25rem',  // 36px — large hero
  },

  lineHeight: {
    tight: '1.2',
    snug: '1.375',
    normal: '1.5',
    relaxed: '1.625',
    loose: '2',
  },

  letterSpacing: {
    tighter: '-0.02em',
    tight: '-0.01em',
    normal: '0',
    wide: '0.02em',
    wider: '0.04em',
  },
} as const

export type TypographyKey = keyof typeof typography
