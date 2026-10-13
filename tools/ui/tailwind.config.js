// Mesma configuracao que a UI usava no CDN do Tailwind: paleta de neutros quentes + laranja.
module.exports = {
  darkMode: 'class',
  content: ['./web/index.html'],
  theme: {
    extend: {
      fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'], mono: ['JetBrains Mono', 'ui-monospace', 'monospace'] },
      colors: {
        zinc: { 50: '#fafaf9', 100: '#ededed', 200: '#d4d4d4', 300: '#b8b8b8', 400: '#9ca3af', 500: '#6b7280',
                600: '#4a4a4a', 700: '#3a3a3a', 800: '#333333', 900: '#262626', 950: '#161616' },
        brand: { 50: '#fff4f0', 300: '#f5a58f', 400: '#ee8466', 500: '#e56a4a', 600: '#e56a4a', 700: '#cc5236', 900: '#6b2a1a' }
      }
    }
  },
  plugins: []
};
