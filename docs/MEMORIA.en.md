# Hera Memory

Hera keeps typed Markdown notes so it can remember what matters across sessions. Everything stays local, in `~/.hera/memoria` (or the directory set by `HERA_MEMORY_DIR`).

## Note types

- `usuario`: preferences and facts about you.
- `feedback`: corrections and guidance given to Hera.
- `projeto`: context for work in progress.
- `referencia`: links, excerpts, and useful material.

## Commands

- `hera memory list` — lists the notes.
- `hera memory search` — searches the notes (FTS5 index).
- `hera memory show` — shows a note.
- `hera memory add` — adds a note.
- `hera memory forget` — removes a note.
- `hera memory export` — exports the notes.

## Search

Search uses FTS5, so it finds words and excerpts quickly, without leaving your PC.

## Snapshot

The snapshot is on by default (it only reads notes if the memory folder exists); `HERA_MEMORY_SNAPSHOT=0` turns it off. It respects a token limit, so it does not grow without control.

## Secret filter

Before any note is written and before anything is sent to the cloud, the content goes through the secret filter. Nothing is saved or sent without that check.

## Session summary

The session summary (`app/memory/resumo.py`) always shows a preview and asks for approval before becoming memory.

## Learned playbooks

Learned playbooks only become active after approval. They come from three similar successful runs.

## Deleting everything

To delete all memory, just delete the `~/.hera/memoria` folder (or the `HERA_MEMORY_DIR` directory).

## Privacy

Nothing leaves your PC without passing through the secret filter.

## How to use

- In `hera chat`, `/lembrar` previews the notes that would be saved (nothing is written); `/lembrar sim` saves all, `/lembrar sim 1 3` saves only the chosen ones and `/lembrar nao` discards them.
- `python main.py playbooks list|aprovar|recusar|desativar` manages learned playbooks. `tools/plano.py` records every executed plan and proposes a playbook after 3 identical successful runs.

## Current status

Session summaries and learned playbooks are wired to `hera chat` (`/lembrar`) and `tools/plano.py`. There is no automatic summary on chat exit: saving is always your explicit request.
