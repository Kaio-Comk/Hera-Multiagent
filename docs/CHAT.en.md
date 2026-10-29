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

- `--agente`: enables the code agent.
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

With `--agente`, these tools are available: `ler_arquivo`, `listar_pasta`, `buscar`, `editar_arquivo`, `escrever_arquivo`, `executar_comando`, `usar_skill`, and `delegar_tarefa`.

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
