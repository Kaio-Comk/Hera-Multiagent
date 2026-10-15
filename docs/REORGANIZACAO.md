# Reorganização de pastas — plano (nada foi movido ainda)

> Data: 13/10/2026. Base: relatório de mapeamento (`run-d7811869-s1`) e varredura de referências de caminho no repositório.
> Baseline de testes antes de qualquer mudança: **970 passed, 1 skipped** (`python -m pytest -q`).
> Princípio: **mudanças pequenas e de baixo risco**, nada de redesenho total. Sem namespace novo (`hera/`, `src/`), sem
> mexer em `main.py`, sem mexer em módulos importados por muitos arquivos. Todo movimento é com `git mv` (preserva o histórico).

---

## 1. Árvore alvo

Só o que muda aparece marcado. **(novo)** = pasta/arquivo criado, **(movido)** = chega por `git mv`. O resto fica como está.

```text
multiagent-orchestrator/
├── .github/workflows/ci.yml          (fixo; nada a ajustar, ver §5)
├── adapters/                         (fixo)
├── app/
│   ├── arena/  gateway/  hitl/  mcp/  repo_map/  security/   (fixos)
│   ├── chat/                         (novo)  ← Lote 2
│   │   ├── __init__.py               (novo)
│   │   ├── api.py                    (movido de app/chat_api.py)
│   │   ├── conversations.py          (movido de app/conversations.py)
│   │   └── uploads.py                (movido de app/uploads.py)
│   ├── vault/                        (novo)  ← Lote 2  (Obsidian / segundo cérebro)
│   │   ├── __init__.py               (novo)
│   │   ├── obsidian.py               (movido de app/obsidian.py)
│   │   ├── memory.py                 (movido de app/vault_memory.py)
│   │   └── manager.py                (movido de app/vault_manager.py)
│   ├── __init__.py                   (fixo, só ajusta 2 imports)
│   ├── server.py, config_manager.py, router.py, coordinator.py, validator.py,
│   │   state_store.py, privacy_guard.py, dag_scheduler.py, ...   (todos fixos)
├── data/                             (fixo: bancos, segredos, logs, runs)
├── docs/
│   ├── REORGANIZACAO.md              (este plano)
│   └── screenshots/                  (novo)  ← Lote 1
│       ├── painel-inicio.png         (movido de docs/brand/)
│       └── painel-roteador.png       (movido de docs/brand/)
├── models/                           (fixo)
├── policies/permissions.yaml         (fixo)
├── tests/                            (fixo; só imports ajustados)
├── tools/
│   ├── mcp_rea_bridge.py             (fixo, ver §3)
│   ├── rea/                          (fixo)
│   └── ui/
│       ├── build_ui.py               (movido de tools/build_ui.py)  ← Lote 1
│       ├── input.css
│       └── tailwind.config.js
├── web/                              (fixo: index.html + static/)
├── CONTRIBUTING.md  DESIGN.md  LICENSE  README.md   (fixos na raiz)
├── config.example.yaml  pyproject.toml  main.py      (fixos na raiz)
└── config.yaml                       (local, ignorado pelo git; fica)
```

---

## 2. Tabela caminho antigo → caminho novo

| Lote | Caminho antigo | Caminho novo | Comando |
|---|---|---|---|
| 1 | `tools/build_ui.py` | `tools/ui/build_ui.py` | `git mv tools/build_ui.py tools/ui/build_ui.py` |
| 1 | `docs/brand/painel-inicio.png` | `docs/screenshots/painel-inicio.png` | `git mv docs/brand/painel-inicio.png docs/screenshots/painel-inicio.png` |
| 1 | `docs/brand/painel-roteador.png` | `docs/screenshots/painel-roteador.png` | `git mv docs/brand/painel-roteador.png docs/screenshots/painel-roteador.png` |
| 2 | `app/obsidian.py` | `app/vault/obsidian.py` | `git mv app/obsidian.py app/vault/obsidian.py` |
| 2 | `app/vault_memory.py` | `app/vault/memory.py` | `git mv app/vault_memory.py app/vault/memory.py` |
| 2 | `app/vault_manager.py` | `app/vault/manager.py` | `git mv app/vault_manager.py app/vault/manager.py` |
| 2 | `app/chat_api.py` | `app/chat/api.py` | `git mv app/chat_api.py app/chat/api.py` |
| 2 | `app/conversations.py` | `app/chat/conversations.py` | `git mv app/conversations.py app/chat/conversations.py` |
| 2 | `app/uploads.py` | `app/chat/uploads.py` | `git mv app/uploads.py app/chat/uploads.py` |
| 2 | — | `app/vault/__init__.py`, `app/chat/__init__.py` | criar (vazios ou com docstring curta) |

Por que estes e não outros:

- **`docs/brand/` → `docs/screenshots/`**: o nome `brand` colide com `web/static/brand/` (onde estão os logos de verdade); ali só há capturas de tela do painel.
- **`build_ui.py` → `tools/ui/`**: o script e as duas entradas que ele consome (`input.css`, `tailwind.config.js`) ficam juntos.
- **`app/vault/`**: os três módulos do Obsidian formam um subsistema e têm poucos importadores (obsidian: 6 arquivos, vault_memory: 6, vault_manager: 2).
- **`app/chat/`**: as conversas do Plano (API, banco de conversas, uploads) têm 1–2 importadores cada e nenhum outro subsistema os usa.
- Nenhum dos 6 módulos usa `__file__` para achar a raiz do projeto e nenhum teste faz `monkeypatch`/`patch` com string `"app.<modulo>"` neles, então a profundidade nova não quebra caminhos em disco.

---

## 3. O que fica FIXO (e por quê)

| Item | Motivo para não mexer |
|---|---|
| `main.py` (raiz) | Ponto de entrada: `[project.scripts] hera = "main:cli"` e `py-modules = ["main"]` no `pyproject.toml`, CI (`bandit ... main.py`), README e hábitos (`python main.py web/gui`). Movê-lo exige reinstalar o pacote em toda máquina. |
| `pyproject.toml`, `config.example.yaml`, `README.md`, `LICENSE`, `CONTRIBUTING.md` | Convenção de raiz; `tests/test_portability.py` lê `ROOT / "config.example.yaml"`. |
| `config.yaml` (local) | `app/config_manager.py:81` procura em `Path(__file__).parent.parent / "config.yaml"`; é ignorado pelo git. |
| `DESIGN.md` (raiz) | O formato `DESIGN.md` é feito para ser achado na raiz por pessoas e agentes de IA (como `README.md`); `tests/test_ui_marca.py:13` lê `ROOT / "DESIGN.md"`. |
| `data/` (bancos, `.hera_*`, `logs/`, `runs/`) | Âncoras fixas: `app/state_store.py:8` (`.../data/orchestrator.db`) e `app/security/data_dir.py:14` (`parent.parent.parent / "data"`). Contém estado real do usuário; mover arrisca perder histórico e segredos. |
| `web/` (`index.html`, `static/**`) | `app/server.py:132` (`WEB_DIR = ROOT_DIR / "web"`), URLs `/static/...` com `?v=`, CSP e testes (`test_ui_marca.py`, `test_ui_conexoes.py`) amarrados a esses caminhos. |
| `tools/mcp_rea_bridge.py` e `tools/rea/` | O `config.yaml` de cada usuário pode ter `args: ["tools/mcp_rea_bridge.py"]` (servidor MCP) — mover quebra configurações já salvas fora do repositório. O teste importa via `sys.path.insert(0, ROOT / "tools")`. |
| `tools/ui/input.css`, `tools/ui/tailwind.config.js` | Já estão no lugar certo; `content: ['./web/index.html']` é relativo ao cwd (raiz), que não muda. |
| `policies/permissions.yaml` | `app/policy_engine.py:176` usa caminho **relativo ao cwd** (`os.path.join("policies", ...)`); mover agora seria frágil. |
| Pacotes de topo `app/`, `adapters/`, `models/` | `pyproject.toml` (`include = ["app*", "adapters*", "models*"]`), CI (`bandit -r app adapters models`) e centenas de imports. Unificar num namespace `hera/` seria o "redesenho total" que este plano evita. |
| `app/server.py` | Importado por 35 arquivos (quase todos os testes). |
| `app/config_manager.py` (18), `app/router.py` (12), `app/validator.py` (11), `app/state_store.py` (11, + âncora `__file__`), `app/privacy_guard.py` (10), `app/coordinator.py` (9), `app/dag_scheduler.py` (7), `app/locations.py`, `app/engine_profiles.py`, `app/console_bus.py`, `app/policy_engine.py`, `app/runtime_state.py`, `app/services_control.py`, `app/worktree_manager.py`, `app/auto_fix.py`, `app/orchestration.py`, `app/benchmark.py` | Núcleo da orquestração: muitos importadores e/ou baixo ganho. Agrupá-los (ex.: `app/core/`) fica para uma fase futura, se ainda fizer sentido. |
| Subpacotes já organizados (`app/gateway`, `hitl`, `mcp`, `arena`, `repo_map`, `security`) | Já estão separados por responsabilidade. |
| `tests/` (estrutura plana) | `testpaths = ["tests"]`; `conftest.py`, `helpers.py` e `fixtures/` são referenciados por caminho. |

Itens que **não são movimento** (limpeza local, fora do git, opcional):

- `hera_multiagent.egg-info/` — resíduo de `pip install -e .`, já ignorado (`*.egg-info/`); pode ser apagado e é recriado pela instalação.
- `.pytest_cache/`, `__pycache__/` — já ignorados.

---

## 4. Divisão em lotes

Cada lote é um commit/PR separado. Ordem obrigatória: **Lote 1 → testes verdes → Lote 2 → testes verdes**.

### Lote 1 — raiz, docs, tools e assets web (zero impacto em imports Python)

1. `mkdir docs/screenshots` e os dois `git mv` das capturas de tela.
2. `git mv tools/build_ui.py tools/ui/build_ui.py`.
3. Ajustar as referências listadas em §5.1.
4. `.gitignore`: acrescentar `data/runs/` (hoje os `run-*.jsonl` aparecem como não rastreados e fazem `data/` surgir no `git status`).
5. Assets web: **nenhum movimento** (ver §3). Só os comentários que citam `tools/build_ui.py` mudam.
6. Validar: `python -m pytest -q` (esperado 970 passed, 1 skipped); `python -c "import runpy; m = runpy.run_path('tools/ui/build_ui.py'); print(m['STATIC'])"` deve imprimir `...\web\static`; conferir que as imagens aparecem no README renderizado.

### Lote 2 — módulos Python (`app`; `adapters` e `models` ficam como estão)

1. Criar `app/vault/__init__.py` e `app/chat/__init__.py`.
2. Os seis `git mv` da tabela.
3. Ajustar imports e comentários listados em §5.2.
4. **Sem shims de compatibilidade** (`app/obsidian.py` reexportando): os importadores são todos internos ao repositório e são poucos; shims só deixariam o lixo de volta na raiz de `app/`.
5. Validar: `python -m pytest -q`; `pip install -e .` (o `include = ["app*"]` já cobre `app.vault` e `app.chat`); `python main.py --help`; subir `python main.py web` e abrir uma conversa do Plano (exercita `app.chat`) e a aba do Obsidian (exercita `app.vault`).
6. Verificação final de restos: `git grep -nE "app[./](obsidian|vault_memory|vault_manager|chat_api|conversations|uploads)\b"` deve voltar vazio (fora deste documento).

`adapters/` e `models/` **não mudam** no Lote 2: são pequenos, coesos e importados por todo o código.

---

## 5. Arquivos de configuração / caminhos a ajustar

### 5.1 Lote 1

| Arquivo | Linha (hoje) | Ajuste |
|---|---|---|
| `tools/ui/build_ui.py` | 17 (`ROOT = Path(__file__).resolve().parent.parent`) | **Obrigatório:** passar a `.parent.parent.parent` (o arquivo desce um nível). Sem isso `STATIC` aponta para `tools/web/static`. |
| `tools/ui/build_ui.py` | 1, 6 (docstring) | `python tools/build_ui.py` → `python tools/ui/build_ui.py` |
| `README.md` | 81, 148 | `python tools/build_ui.py` → `python tools/ui/build_ui.py` |
| `README.md` | 99, 143 | `docs/brand/painel-*.png` → `docs/screenshots/painel-*.png` |
| `DESIGN.md` | 5, 124 | `python tools/build_ui.py` → `python tools/ui/build_ui.py` |
| `app/server.py` | 135 (comentário) | `tools/build_ui.py` → `tools/ui/build_ui.py` |
| `web/index.html` | 10 (comentário HTML) | `tools/build_ui.py` → `tools/ui/build_ui.py` (só comentário; não muda nenhuma URL, então não precisa subir o `?v=`) |
| `.gitignore` | — | acrescentar `data/runs/` |
| `.github/workflows/ci.yml` | — | nada (não cita `tools/` nem `docs/`) |
| `pyproject.toml` | — | nada |

### 5.2 Lote 2

| Arquivo | Linha (hoje) | Ajuste |
|---|---|---|
| `app/__init__.py` | 4 | `from app.obsidian import` → `from app.vault.obsidian import` |
| `app/__init__.py` | 8 | `from app.vault_memory import` → `from app.vault.memory import` |
| `app/vault/obsidian.py` | 8 | `from app.vault_memory import` → `from app.vault.memory import` |
| `app/server.py` | 31 | `app.obsidian` → `app.vault.obsidian` |
| `app/server.py` | 32 | `app.vault_memory` → `app.vault.memory` |
| `app/server.py` | 42 | `app.vault_manager` → `app.vault.manager` |
| `app/server.py` | 808 (comentário), 810, 812 | `from app import chat_api` → `from app.chat import api as chat_api` (mantém o nome local, então a linha 812 `chat_api.install(...)` não muda); comentário `app/chat_api.py` → `app/chat/api.py` |
| `app/server.py` | 1039 (comentário) | `app/vault_manager.py` → `app/vault/manager.py` |
| `app/chat/api.py` | 17 | `from app import uploads` → `from app.chat import uploads` |
| `app/chat/api.py` | 18 | `from app.conversations import` → `from app.chat.conversations import` |
| `app/chat/conversations.py` | 7 (docstring) | `app/uploads.py` → `app/chat/uploads.py` |
| `main.py` | 14, 15 | `app.obsidian` → `app.vault.obsidian`; `app.vault_memory` → `app.vault.memory` |
| `tests/test_obsidian.py` | 4 | `app.obsidian` → `app.vault.obsidian` |
| `tests/test_vault_memory.py` | 3, 4 | `app.vault_memory` → `app.vault.memory`; `app.obsidian` → `app.vault.obsidian` |
| `tests/test_portability.py` | 11, 12 | idem |
| `tests/test_obsidian_manager.py` | 11 | `app.vault_manager` → `app.vault.manager` |
| `tests/test_chat.py` | 12, 13 | `from app import uploads` → `from app.chat import uploads`; `app.conversations` → `app.chat.conversations` |
| `README.md` | 446, 453 (árvore do projeto) | trocar `obsidian.py`/`vault_memory.py` por um bloco `vault/` (obsidian, memory, manager) e acrescentar `chat/` (api, conversations, uploads) |
| `pyproject.toml` | — | nada (`include = ["app*"]` já pega os subpacotes) |
| `.github/workflows/ci.yml` | — | nada (`bandit -r app` já cobre) |

Opcional no Lote 2 (correção, não movimento): ancorar `app/policy_engine.py:176` na raiz do projeto
(`Path(__file__).resolve().parent.parent / "policies" / "permissions.yaml"`) em vez do cwd, para que
`policies/` deixe de depender de onde o processo foi iniciado. Só fazer com teste próprio; se ficar para depois, não bloqueia nada.

---

## 6. Riscos e como reverter

- **Risco principal do Lote 1:** esquecer o `.parent` extra em `build_ui.py` → o script gera CSS no lugar errado. Coberto pelo passo de validação do `runpy`.
- **Risco principal do Lote 2:** um import esquecido → `ModuleNotFoundError` já na coleta do pytest (falha alta e imediata, não silenciosa). Coberto pela suíte e pelo `git grep` final.
- **Worktrees em `.worktrees/`:** branches abertas que mexem nos arquivos movidos vão ter conflito de rename no merge; o git costuma detectar o rename (`git mv` sem alterar conteúdo no mesmo commit ajuda). Fazer os `git mv` num commit e os ajustes de import em outro aumenta a detecção de renomeação.
- **Reverter:** cada lote é um commit isolado → `git revert <commit>`.
