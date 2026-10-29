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

O snapshot vem ligado por padrão (só lê notas se a pasta de memória existir); `HERA_MEMORY_SNAPSHOT=0` desliga. Ele respeita um limite de tokens, para não crescer sem controle.

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

## Como usar

- No `hera chat`, `/lembrar` mostra uma previa das notas que seriam salvas (nada e gravado); `/lembrar sim` grava todas, `/lembrar sim 1 3` grava so as escolhidas e `/lembrar nao` descarta.
- `python main.py playbooks list|aprovar|recusar|desativar` gerencia os playbooks aprendidos. `tools/plano.py` registra cada plano executado e propoe um playbook apos 3 execucoes iguais com sucesso.

## Estado atual

Resumo de sessao e playbooks estao ligados ao `hera chat` (`/lembrar`) e ao `tools/plano.py`. Ainda nao ha resumo automatico ao fechar o chat: a gravacao e sempre um pedido seu.
