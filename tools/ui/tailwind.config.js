// Sistema visual "Pavão & Noite" (ver DESIGN.md). A escala `zinc` vira a "noite fria" da marca e `brand` o teal do pavão;
// assim todo o HTML que já usa zinc-*/brand-* muda de uma vez. `gold` é o ouro do "olho" (só em detalhes).
module.exports = {
  darkMode: 'class',
  content: ['./web/index.html'],
  theme: {
    extend: {
      fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'], mono: ['JetBrains Mono', 'ui-monospace', 'monospace'] },
      colors: {
        zinc: { 50: '#f4f7fb', 100: '#e8eef5', 200: '#cfdae6', 300: '#9fb0c3', 400: '#8396ab', 500: '#71869d',
                600: '#41566f', 700: '#2d3f56', 800: '#202e41', 900: '#0f1620', 950: '#070b10' },
        brand: { 50: '#ecfdf9', 100: '#c9f6ec', 200: '#a0eddc', 300: '#7fe3d3', 400: '#3fcfbb', 500: '#19b8a4', 600: '#0f9a89',
                 700: '#0b7468', 800: '#095049', 900: '#063b35', ink: '#04201c' },
        gold: { 300: '#f6dc9a', 400: '#ecc66e', 500: '#e0a93f' }
      }
    }
  },
  plugins: []
};
