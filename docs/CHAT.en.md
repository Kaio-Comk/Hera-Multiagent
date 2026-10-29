# Terminal chat

`hera chat` opens a continuous conversation in the terminal. If the local server is off, it starts it automatically.

## Installation

```text
pipx install git+https://github.com/Kaio-Comk/Hera-Multiagent.git
hera chat
```

When installed, Hera keeps its data (database, token, `config.yaml`) in `%USERPROFILE%\.hera`, outside the package folder. `HERA_HOME` moves it; `HERA_DATA_DIR` and `HERA_CONFIG` move only the data or only the configuration. Upgrading the package does not touch your data. Windows is the supported platform. The test suite also passes on Linux and macOS in CI, but day-to-day use there is untested.

## Quick start

```text
hera chat
hera chat --agente
hera chat --modo read-only
hera chat --modelo gpt-4o-mini
hera chat --perfil economico
hera chat --resume
hera chat --listar
```

Main options:

- `--agente`: code agent (already the default): reads, edits and runs commands in the current folder, with approval. `--sem-agente` turns it off and leaves chat only.
- `--modo read-only|guarded|workspace`: default is `guarded`.
- `--modelo X`: forces a model.
- `--sem-maestro`: disables the Maestro.
- `--perfil economico|qualidade`: selects the profile.
- `--janela N`: token window size (default 32000).
- `--resume [id|título]`: resumes a saved conversation.
- `--listar`: lists saved conversations.
- `--url`: sets the server URL.

## Maestro

The Maestro is on by default. It classifies each request as light, medium, or heavy using local rules, without spending an AI call, and picks the model for that tier among the available ones. If a model is unavailable (429/503), it switches automatically.

You can force a model with `--modelo` or `/model nome`, return to automatic with `/model auto`, turn it off with `/maestro off`, and change the profile with `/perfil`.

## Chat interface

### Opening and input field
- Opening: block mascot, "Hera vX", model (or "automatic model (maestro)"), current folder and a line with the loaded instruction files (e.g. ~/.hera/HERA.md).
- The screen is cleared on open.
- Input field pinned to the bottom of the screen between two rules, with dimmed placeholder: "Pergunte algo, / para comandos, @ para anexar arquivo".
- On submit, the question stays in history with the ❯ symbol and the conversation continues right after the content.
- Footer under the field: agent mode (yellow when on), "maestro" or the chosen model, and shortcuts (/ commands, @file, ctrl+c interrupts).

### Responses and Markdown
- While the model thinks: animated "✻ Pensando…" indicator; after 2 s it shows the seconds and "ctrl+c interrompe".
- It disappears when the response starts; the input field disappears during the response and returns at the end.
- The response appears line by line, with rendered Markdown: headings (#, ##) in colored bold without the symbols, bullet lists, **bold**, *italic*, `inline code` in gold.
- Code blocks with the "╌╌ language" fence and "│" bar (no syntax highlighting), quotes "▎", links as "text (url)".
- Tables stay as plain text.

### Switching model and maestro
- /model with no argument opens a selector: arrows navigate, type to filter, PageUp, PageDown, Home, End, Enter selects, Esc or Ctrl+C cancels.
- A 10-item window with "above/below"; "auto" hands control back to the maestro.
- /model <name> and /model auto still work.
- Maestro status line: "⎿ maestro: faixa X -> modelo"; "(decidido pelo juiz)" when the judge decided.
- It appears on the first message and when the model changes.
- Maestro judge: only when the local rule is unsure; uses a fast model (DeepSeek-V4.1-Flash, then gemini-flash-lite, then agnes-2.5-flash); 8 s limit; on failure it rests 5 min.
- The maestro light tier now starts with DeepSeek-V4.1-Flash (River), then gemini-flash-lite, gpt-4o-mini, agnes-2.5-flash.

### Colors
- NO_COLOR or non-terminal output turn off colors and screen effects (text comes out plain, without clearing the screen).

### Instructions and memory
- Global instructions: ~/.hera/HERA.md takes priority over ~/.claude/CLAUDE.md (which is only read if the global HERA.md does not exist).
- Memory snapshot is ON by default (HERA_MEMORY_SNAPSHOT=0 turns it off).
- Memory search reports a clear error (IndiceIndisponivel) when the index fails, ignores short/common words (up to 8 terms) and removes unreadable notes from the index.

## Commands

```text
/help
/clear
/model [nome|auto]
/resume [ref]
/listar
/memory [consulta]
/skills
/<nome-da-skill>
/cost
/instrucoes
/compact
/goal [N] <goal>
/maestro
/perfil
/rollback [id]
/checkpoints
/modo
/exit
```

## Goals with /goal

`/goal 5 make the test pass` makes the agent work in turns (at most N, default 5, hard cap 10) until a cheap judge says the goal is met, blocked, or the limit is reached. Every action still asks for your approval (`guarded` mode) and Ctrl+C interrupts. If the judge fails, the goal stops: without a judge there is no way to know when to stop.

## Images

`hera chat` can read images (screenshots, photos, error captures):

- `@print.png` or `@"my folder/print.png"` attaches a file; relative paths start at the current folder.
- Dragging the file into the terminal pastes its full path (quoted or not), which also works.
- `@clip` attaches the image on the clipboard (for example after `Win+Shift+S`). Windows only.
- Formats: PNG, JPEG, GIF and WebP, up to 4 images per message and 5 MB each. The format is checked from the file contents, not the extension.
- The maestro picks a vision-capable model on its own (DeepSeek-V4.1-Flash, then gemini-flash-lite and Nemotron VL). If you force a model that cannot see images, the chat only warns you.
- The image goes to the chosen model (including cloud providers): do not attach screens with passwords or personal data. The saved history keeps only the text `[imagem: name]`, not the pixels.

## Attaching files with @

Write `@file`, `@folder/x.py`, or `@x.py:10-30` in your message and the content is sent to the model along with it (up to 5 attachments per message). Folders become a listing. Paths outside the working folder and sensitive files (`.env`, keys) are refused with a warning, and keys inside the content are masked. The saved history keeps what you typed, not the attached content.

## HERA.md

`HERA.md` holds project instructions, similar to `CLAUDE.md`. It is searched in each folder from the git root to the current folder, plus `~/.hera/HERA.md`. Order: `HERA.md`, then `AGENTS.md`, then `CLAUDE.md`. `CLAUDE.md` is read-only. Project files containing prompt injection phrases are blocked with a warning.

## Skills

Skills use the `SKILL.md` format and live in `~/.hera/skills` and `./.hera/skills`.

```text
hera skills import
hera skills list
hera skills show <nome>
```

`hera skills import` copies skills from `~/.claude/skills` without changing the originals, skipping `.venv`, caches, and files that look like secrets.

## Code agent and safety

The agent is on by default (`guarded` mode: it asks before writing or running). It only sees the folder where the chat was opened; sensitive files (`.env`, keys, sessions, credentials, `AppData`) are always refused. Available tools: `ler_arquivo`, `listar_pasta`, `buscar`, `editar_arquivo`, `escrever_arquivo`, `executar_comando`, `usar_skill`, and `delegar_tarefa`.

`delegar_tarefa` calls sub-agents: clean-context helpers on a cheaper model (light, medium, or heavy tier) that **only read** the project and return a summary. Up to 3 run in parallel; they cannot edit, run commands, or spawn other sub-agents. Only the main agent edits, with your approval.

The agent shows the diff and asks for approval with `[s]im/[n]ao/[t]odos`. Only `sim` and `todos` apply the change.

Execution rules:

- No shell: `&&`, `|`, and `>` are blocked.
- Blocked: `git push`, `curl`, `powershell`, and recursive removal.
- Run without asking: `git status`, `git diff`, `git log`, and safe reads.
- `pytest` asks for approval.
- Never reads `.env`, keys, or `.git`.

Automatic checkpoints are stored in `~/.hera/checkpoints`. `/rollback` undoes without deleting what you edited afterwards.

## Memory

On `/exit` after 3 or more questions, the chat offers to save what matters to memory: it shows a preview and only writes after you confirm twice. When resuming a conversation (`--resume`), the opening shows `Retomando: title (N messages)`.

Notes are stored as Markdown in `~/.hera/memoria`. The `HERA_MEMORY_DIR` variable changes the directory.

```text
hera memory list
hera memory search <termo>
hera memory show <name>
hera memory add
hera memory forget <name>
hera memory export
```

By default (turn off with `HERA_MEMORY_SNAPSHOT=0`; also applied to the start of every step of plans run by the server: web panel and Claude plans, within the 4,000-character instruction limit), relevant notes are injected on each question. Conversations are saved in SQLite and can be resumed.

## Providers and cost

```text
hera providers list [--catalogo] [--gratis]
hera providers add
hera providers key
hera providers test
hera providers enable
hera providers disable
hera providers remove
hera providers models
hera providers limits
```

There are more than 50 presets, several free. The daily spend cap is `gateway.daily_budget_usd` in `config.yaml`. With value `0`, only free and local providers are used.

## Troubleshooting

- Server off: `hera chat` starts it automatically; if it fails, check `--url`.
- Model unavailable (429/503): the Maestro switches automatically; or use `/model auto`.
- High cost: use `--perfil economico` or set `gateway.daily_budget_usd`.
- Blocked command: review the code agent rules.
- Unwanted change: use `/rollback` or `/checkpoints`.
- Project instructions not applied: check `HERA.md`, `AGENTS.md`, or `CLAUDE.md`.
