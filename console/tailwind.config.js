/** Forge design system — white + volcanic orange, mechanical/geometric. */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        bg: '#FFFFFF',
        surface: '#FAFAF9',
        ink: '#111110',
        'ink-muted': '#57534E',
        line: '#E7E5E4',
        grid: '#F1F0EE',
        orange: '#FF4D00',
        'orange-press': '#E04300',
        'orange-tint': '#FFF1EA',
        ok: '#15803D',
        danger: '#B91C1C',
      },
      borderRadius: { forge: '2px' },
      fontFamily: {
        sans: ['Inter', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      transitionTimingFunction: { mech: 'linear' },
    },
  },
  plugins: [],
};
