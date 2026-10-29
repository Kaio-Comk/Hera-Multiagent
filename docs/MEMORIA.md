# Memória do Hera

O Hera guarda notas Markdown tipadas para lembrar do que importa entre sessões. Tudo fica local, em `~/.hera/memoria` (ou no diretório indicado por `HERA_MEMORY_DIR`).

## Tipos de nota

- `usuario`: preferências e fatos sobre você.
- `feedback`: correções e orientações dadas ao Hera.
- `projeto`: contexto de trabalho em andamento.
- `referencia`: links, trechos e materiais úteis.

## Comandos

- `hera memory list` — lista as notas.
- `hera memory search` — busca nas notas (índice FTS5).
- `hera memory show` — mostra uma nota.
- `hera memory add` — adiciona uma nota.
- `hera memory forget` — remove uma nota.
- `hera memory export` — exporta as notas.

## Busca

A busca usa FTS5, então encontra por palavras e trechos com rapidez, sem sair do PC.

## Snapshot

O snapshot é opt-in: só acontece com `HERA_MEMORY_SNAPSHOT=1`. Ele respeita um limite de tokens, para não crescer sem controle.

## Filtro de segredos

Antes de gravar qualquer nota e antes de enviar qualquer coisa para a nuvem, o conteúdo passa pelo filtro de segredos. Nada é salvo nem enviado sem essa checagem.

## Resumo de sessão

O resumo de sessão (`app/memory/resumo.py`) sempre mostra uma prévia e pede aprovação antes de virar memória.

## Playbooks aprendidos

Playbooks aprendidos só ficam ativos após aprovação. Eles surgem de três execuções bem-sucedidas parecidas.

## Apagar tudo

Para apagar toda a memória, basta apagar a pasta `~/.hera/memoria` (ou o diretório de `HERA_MEMORY_DIR`).

## Privacidade

Nada sai do seu PC sem passar pelo filtro de segredos.

## Estado atual

O resumo de sessão (`app/memory/resumo.py`) e os playbooks aprendidos (`app/memory/playbooks_aprendidos.py`) já existem como bibliotecas testadas, mas o `hera chat` ainda não os chama sozinho: a ligação automática vem numa próxima versão.
