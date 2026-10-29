# Code Agent Security Model

This document describes the security model of the Hera code agent (`hera chat --agente`). It is a terminal agent that proposes edits, writes, and commands, and applies them only after explicit approval. The model is designed to fail closed, but it is not a sandbox and does not replace human judgment.

## Core principle: nothing happens without `apply`

The agent never writes to disk or executes a command on its own. It only *proposes* actions:

- `edit` / `write` proposals are shown as a diff.
- `command` proposals are shown as the exact command line.

The user is then prompted for approval: `[s]im` / `[n]ao` / `[t]odos` (yes / no / all). The decision logic fails closed:

- Only `sim` and `todos` apply the action.
- Empty input, `Ctrl+C`, EOF, a prompt error, or any other response is treated as a refusal.

## Modes

- **read-only**: no writes and no commands. The agent can only read and reason.
- **guarded** (default): every write and every command requires approval.
- **workspace**: edits inside the working folder are applied after the diff is shown; commands still require approval.

## Path confinement

Every path is resolved (symlinks and junctions are followed) and must remain inside the working folder. Paths that escape it are refused.

Sensitive files are refused even when they are inside the working folder, including:

- `.env*`, `*.pem`, `*.key`, `id_rsa*`, `*.pfx`, `.hera_token`
- `secrets*`, `credentials*`, `.npmrc`, `.pypirc`
- anything under `.git`, `.ssh`, `.aws`
- names that Windows normalizes, such as trailing dot or space, NTFS alternate data streams (`name:stream`), and `::$DATA`

## Secret handling

- Tool output passes through a secret filter before reaching the model. Detected keys are replaced with `[REDACTED_SECRET:type]`.
- The environment of executed commands strips variables whose names contain `KEY`, `TOKEN`, `SECRET`, `PASSWORD`, or `API`.

## Command execution

Commands run without a shell (`shell=False`). Shell operators are blocked: `&&`, `||`, `|`, `>`, `<`, `;`, backtick, `$(`, `%x%`.

Also blocked: `git push`, `git reset --hard`, `git clean`, `curl`, `wget`, `iwr`, `Invoke-WebRequest`, `powershell`, `cmd`, `rm -rf`, `del /s`, `rd /s`, `format`, `shutdown`, `reg delete`, `diskpart`.

Without approval, only these run:

- `git status`, `git diff`, `git log`, `git show` (without options that write or execute, and without `rev:path`)
- `git branch` (list only)
- `python --version`, `pip list`, `where`

`pytest` and everything else require approval, because they execute project code.

## Edits

- An edit is applied only if the file has not changed since the proposal (hash check).
- Writes are atomic.
- A checkpoint is taken before each write.
- `/rollback` restores state without discarding edits the user made afterwards, using a hash ledger.
- Undo follows the ledger order, not wall-clock time.

## Project instruction files

Instruction files from the repository (`HERA.md`, `AGENTS.md`, `CLAUDE.md`) that contain prompt-injection phrases are blocked with a warning. Invisible characters are removed.

## Sub-agents

When available, sub-agents are read-only: no edits, no commands, no recursion, and at most 3 run in parallel.

## Honest limits

- Human approval is the primary barrier. A command that is approved runs with the user's permissions.
- Approving `todos` applies to the same command in the same folder until the end of the session.
- The secret filter is pattern-based and does not catch everything.
- Models can be wrong or can be induced by malicious text in files they read.
- The current version is Windows-only and there is no process sandbox.
- Use git and guarded mode in untrusted repositories.

## Reporting a vulnerability

Please report vulnerabilities according to the repository's `SECURITY.md`.
