# DESIGN.md — Hera · "Pavão & Noite"

> Sistema visual e marca do Hera, no formato `DESIGN.md` (token + regra + o porquê no mesmo arquivo), para que pessoas **e agentes de IA** mantenham a interface coerente. Formato inspirado na coleção [awesome-claude-design](https://github.com/VoltAgent/awesome-claude-design) (MIT); a identidade abaixo é **própria** e não copia a de nenhuma outra marca.
>
> Implementação: `web/static/hera-ui.css` (tokens + componentes), `tools/ui/tailwind.config.js` (paleta do Tailwind), `web/static/brand/` (logos). Para regenerar o CSS: `python tools/ui/build_ui.py`.

## 1. Marca

**Hera** é o orquestrador local de IAs: planeja, delega a motores locais e da nuvem, revisa cada mudança e guarda o aprendizado no Obsidian, tudo na máquina do usuário.

- **Ideia central — "a que vigia":** na mitologia, Hera pôs os cem olhos de Argos na cauda do pavão. O produto faz o mesmo: *vê* cada passo (DLP, aprovação humana, registro, revisão de diffs). O **olho de pavão** é o símbolo.
- **Tagline:** *Orquestração local de IAs.* Frase de apoio: *Orquestre suas IAs. A Hera vigia cada passo.*
- **Personalidade:** soberana, calma, precisa. Confiança sem arrogância; técnica sem jargão desnecessário.
- **Voz (pt-BR):** direta, na segunda pessoa ("você"), frases curtas, verbos de ação ("Conectar", "Revisar"). Diz o que aconteceu e o que fazer depois. Sem exclamações, sem gíria, sem emoji decorativo. Nunca culpa o usuário.
  - Bom: "Não consegui consultar os motores." · "Nada sai da sua máquina sem a sua regra."
  - Evitar: "Oops! Algo deu errado 😅" · "Incrível!!"
- **Por que não laranja/👑:** o visual anterior (laranja + coroa) era genérico e se confundia com outras ferramentas de IA. O teal do pavão com o ouro do olho é próprio e comunica "vigilância calma".

## 2. Logo

| Arquivo | Uso |
|---|---|
| `web/static/brand/hera-logo-original.jpg` | Logo oficial desde 27/10/2026 (garota com tiara e fone, "HERA"), arquivo original 2048 px |
| `web/static/brand/hera-logo.png` | Logo completo 1024 px (README, divulgação) |
| `web/static/brand/hera-mark-512.png`, `hera-mark-192.png` | Símbolo: só o rosto, cantos arredondados (cabeçalho do painel, avatar, apple-touch-icon) |
| `web/static/brand/favicon.png`, `favicon.ico` | Ícone de aba (64 px; .ico com 16 a 64 px) |
| `web/static/brand/hera-mark.svg`, `hera-logo.svg`, `hera-logo-light.svg`, `favicon.svg` | Marca ANTIGA (até 27/10/2026), mantida só como histórico |
| `web/static/brand/hera-mark-mono.svg` | Uma cor (`currentColor`); usar **inline** no HTML |

- **Construção:** quadrado de cantos arredondados (raio 25%) com gradiente teal→azul-petróleo; um **H** em traço grosso (tinta `#04201c`) cuja barra central carrega o **olho** (ouro `#e0a93f`, pupila escura). O H deve continuar legível: a barra aparece dos dois lados do olho.
- **Wordmark:** "HERA" em Inter ExtraBold, caixa alta, espaçamento de letras ~0,18em.
- **Área de respiro:** pelo menos 1/4 da altura do símbolo em todos os lados. **Tamanho mínimo:** 16 px (símbolo) / 72 px de largura (logotipo).
- **Não:** girar, esticar, trocar as cores do olho, pôr sobre fundo de baixo contraste, recolocar a coroa.

## 3. Atmosfera visual

Painel **noturno e frio**, denso mas respirável, de operador. Superfícies em camadas de azul-tinta (nunca preto puro), bordas finas, um brilho teal suave no canto superior esquerdo e um traço de ouro no direito. O teal é a **única** cor de ação; o ouro é só detalhe (o olho). Cores semânticas aparecem apenas para estado.

*Referências de sensação (não de cópia):* terminais de dev (Warp/Ollama: monocromático, foco no conteúdo), precisão de ferramentas de engenharia (Linear), canvas escuro com um acento único (VoltAgent).

## 4. Paleta e papéis

Tokens em `:root` (`hera-ui.css`) e na escala do Tailwind (`zinc` = noite, `brand` = pavão, `gold` = ouro).

| Papel | Token | Hex | Uso |
|---|---|---|---|
| Fundo | `--hera-bg` / `zinc-950`≈`void` | `#0a0f15` / `#070b10` | página / campos de entrada |
| Superfície | `--hera-surface` `-2` `-3` | `#0f1620` `#141d29` `#1a2534` | cartões, hover, elevação |
| Linha | `--hera-line` `-strong` | `#1e2a3a` `#2b3b50` | bordas, divisórias |
| Texto | `--hera-text` | `#e8eef5` | títulos e corpo |
| Texto 2 | `--hera-text-2` | `#9fb0c3` | apoio (contraste ≥ 5:1) |
| Texto 3 | `--hera-text-3` | `#71869d` | dicas (contraste ≥ 4,5:1) |
| **Ação** | `--hera-brand-500` | `#19b8a4` | botão primário, foco, item ativo |
| Ação (hover) | `--hera-brand-400` | `#3fcfbb` | hover, brilho, ícones ativos |
| Ação (texto) | `--hera-brand-300` | `#7fe3d3` | links, números de destaque |
| Tinta | `--hera-brand-ink` | `#04201c` | texto **sobre** teal |
| Ouro | `--hera-gold-400/500` | `#ecc66e` `#e0a93f` | só o olho e detalhes raros |
| OK / Aviso / Erro / Info | `--hera-ok/warn/danger/info` | `#34d399` `#fbbf24` `#fb7185` `#60a5fa` | **somente estado** |

**Regras**
- Texto sobre teal é **tinta escura**, nunca branco (branco sobre `#19b8a4` dá ~2,6:1; tinta dá ~7:1).
- Um botão primário por região. Ação destrutiva usa vermelho de estado, nunca teal.
- Não usar ouro para botões, links ou estado.

## 5. Tipografia

- **Interface:** Inter (local, `web/static/fonts`), `font-feature-settings: "cv11","ss01"`.
- **Código/dados:** JetBrains Mono (ids, endereços, logs, números alinhados).
- Escala: título de página 19 px/750; título de cartão 14 px/700; corpo 13–14 px; apoio 12 px; rótulo de seção 10 px caixa alta, `letter-spacing .14em`.
- Números de indicadores: `font-variant-numeric: tabular-nums`, 26 px/750.
- Títulos com leve tracking negativo (`-.015em`); caixa alta só em rótulos curtos.

## 6. Layout e navegação

- **Casca:** barra lateral fixa (15 rem) + barra superior fixa + conteúdo (máx. 80 rem) + console recolhível embaixo.
- **Organização por intenção** (não por tecnologia):
  - *(topo)* **Início**
  - **Trabalho:** Plano (conversa com projetos e imagens) · Histórico · Revisão de diffs
  - **Inteligência:** Motores & IAs · Roteador (catálogo de IAs: contas, login em um clique, chaves de API) · Arena & MCP
  - **Conhecimento:** Obsidian
  - **Sistema:** Hardware
- **Barra superior:** migalha (`Hera / Grupo`), **título único da página** (`h1`) + subtítulo, chips de estado (100% offline, motores prontos) e a ação global **Nova tarefa**. Páginas **não** repetem o título.
- **Endereço:** cada tela tem link direto (`#roteador`, `#router/connections`); a última aba é restaurada.
- **Grade:** 4 colunas de indicadores ≥ 1280 px, 2 em telas médias; cartões em 2/3 + 1/3 para lista + contexto.
- **Celular (< 1024 px):** a barra lateral vira **gaveta** (botão ☰, `Esc` e toque fora fecham); o console fechado mostra só título e "abrir".

## 7. Componentes

| Componente | Classe | Regra |
|---|---|---|
| Cartão | `.card` | gradiente sutil `surface-2→surface`, borda `line`, raio 16, realce interno de 1 px |
| Item de menu | `.nav-item` | ativo = `aria-current="page"`, fundo teal 17%, barra luminosa à esquerda |
| Etiqueta de estado | `.chip` + `-ok/-warn/-bad/-info/-brand` | pílula, 11,5 px/600, ponto `pip` (pulsa só em "ao vivo") |
| Botão primário | `.btn.btn-primary` | teal + tinta, sombra teal; hover `brand-400` |
| Botão secundário | `.btn.btn-ghost` | `surface-2`, borda `line` |
| Indicador | `.kpi` | rótulo 11,5 px, valor 26 px, dica 11,5 px |
| Vazio | `.empty` | borda tracejada, texto `text-3`; **sempre** diz o que fazer |
| Interruptor | `.switch` | ligado = teal com bolinha tinta |
| Hero | `.hero` | só no Início; "olho" de pavão decorativo (`::after`) |
| Cartão de IA | `.pv-card` (+ `.is-ready`) | avatar de inicial colorida, chip de estado, nota, 1 ação principal; disponível = borda verde. Login por conta é o único botão teal da grade |

## 8. Profundidade e movimento

- Elevação por **borda + realce**, não por sombra pesada. Sombra só em botão primário e gaveta.
- Transições de 150–220 ms com `cubic-bezier(.2,.7,.2,1)`; `:active` desloca 1 px.
- `prefers-reduced-motion` desliga animações. Animação contínua só no ponto "ao vivo".

## 9. Acessibilidade (obrigatório)

- Foco visível teal de 2 px em **tudo** interativo; link "Ir para o conteúdo" no topo.
- Contraste mínimo 4,5:1 para texto; estado nunca só por cor (sempre texto/ícone junto).
- Controles com nome (`aria-label`) quando só têm ícone; menu com `aria-current`, gaveta com `aria-controls/expanded`.
- Alvos de toque ≥ 38 px no celular.

## 10. Faça / Não faça

**Faça:** teal para ação, tinta sobre teal, cartões consistentes, estado com texto, estados vazios úteis, texto em pt-BR direto, versão no endereço dos estáticos (`?v=`).
**Não faça:** laranja ou coroa (legado), branco sobre teal, gradientes coloridos em botões, ouro como ação, emoji decorativo, sombras grandes, título repetido na página, hex solto no HTML (use token ou classe do Tailwind).

## 11. Guia para agentes

1. Leia este arquivo antes de mexer em `web/index.html`.
2. Cores: use classes `zinc-*`/`brand-*`/`gold-*` ou `var(--hera-*)`; nunca hex novo.
3. Nova tela: adicione a `<section id="tab-x" class="tab-content hidden">`, o botão `.nav-item` com `id="tab-btn-x"` no grupo certo e a entrada em `TAB_META` (título, subtítulo, grupo).
4. Novo componente repetível vira classe em `hera-ui.css` (carregado **antes** do Tailwind, para os utilitários vencerem empates).
5. Depois de mudar classes do Tailwind: `python tools/ui/build_ui.py`. Teste: `tests/test_ui_conexoes.py` e `tests/test_ui_marca.py`.
6. Todo dado dinâmico em `innerHTML` passa por `escapeHtml`/`escAttr`; links vindos de terceiros só `https:`.

Prompt-base: *"Crie a tela X seguindo DESIGN.md do Hera: fundo noite, cartões `.card`, ação em teal com texto tinta, título na barra superior, estados com `.chip`, vazio com orientação, pt-BR direto."*
