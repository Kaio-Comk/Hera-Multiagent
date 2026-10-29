# Chat no terminal

O `hera chat` abre uma conversa contínua no terminal. Se o servidor local estiver desligado, ele sobe sozinho.

## Instalação

```text
pipx install git+https://github.com/Kaio-Comk/Hera-Multiagent.git
hera chat
```

Instalado, o Hera guarda dados (banco, token, `config.yaml`) em `%USERPROFILE%\.hera`, fora da pasta do pacote. A variável `HERA_HOME` muda o local; `HERA_DATA_DIR` e `HERA_CONFIG` mudam só os dados ou só a configuração. Atualizar o pacote não mexe nos seus dados. O Windows é a plataforma suportada. A suíte de testes também passa em Linux e macOS no CI, mas o uso no dia a dia lá não foi testado.

## Início rápido

```text
hera chat
hera chat --agente
hera chat --modo read-only
hera chat --modelo gpt-4o-mini
hera chat --perfil economico
hera chat --resume
hera chat --listar
```

Opções principais:

- `--agente`: ativa o agente de código.
- `--modo read-only|guarded|workspace`: padrão `guarded`.
- `--modelo X`: força um modelo.
- `--sem-maestro`: desliga o Maestro.
- `--perfil economico|qualidade`: escolhe o perfil.
- `--janela N`: tamanho da janela em tokens (padrão 32000).
- `--resume [id|título]`: retoma uma conversa salva.
- `--listar`: lista conversas salvas.
- `--url`: define a URL do servidor.

## Maestro

O Maestro vem ligado por padrão. Ele classifica cada pedido em leve, médio ou pesado usando regras locais, sem gastar chamada de IA, e escolhe o modelo da faixa entre os disponíveis. Quando as regras não têm certeza (pedido sem nenhuma palavra reconhecida, ou palavra pesada isolada em pedido curto), um modelo leve decide a faixa em até 8 segundos; se falhar ou demorar, vale a regra local. O motivo aparece na linha `[maestro: ...]` (`juiz: pesado (regra local: ...)`). Se um modelo estiver indisponível (429/503), ele troca sozinho.

Você pode forçar um modelo com `--modelo` ou `/model nome`, voltar ao automático com `/model auto`, desligar com `/maestro off` e trocar o perfil com `/perfil`.

## Comandos

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
/goal [N] <objetivo>
/maestro
/perfil
/rollback [id]
/checkpoints
/modo
/exit
```

## Objetivos com /goal

`/goal 5 faça o teste passar` faz o agente trabalhar em turnos (no máximo N, padrão 5, teto 10) até um juiz barato dizer que o objetivo foi cumprido, que está bloqueado ou que o limite acabou. Cada ação continua pedindo a sua aprovação (modo `guarded`) e Ctrl+C interrompe. Se o juiz falhar, o goal para: sem juiz não há como saber quando parar.

## Anexar arquivos com @

Escreva `@arquivo`, `@pasta/x.py` ou `@x.py:10-30` na pergunta e o conteúdo vai junto para o modelo (até 5 anexos por mensagem). Pastas viram uma listagem. Caminhos fora da pasta de trabalho e arquivos sensíveis (`.env`, chaves) são recusados com aviso, e chaves dentro do conteúdo são mascaradas. O histórico gravado guarda o que você digitou, sem o conteúdo anexado.

## HERA.md

O `HERA.md` guarda as instruções do projeto, no estilo do `CLAUDE.md`. A busca ocorre em cada pasta, da raiz do git até a pasta atual, e também em `~/.hera/HERA.md`. A ordem é: `HERA.md`, senão `AGENTS.md`, senão `CLAUDE.md`. O `CLAUDE.md` é apenas leitura. Arquivos do projeto com frases de injeção de prompt são bloqueados com aviso.

## Skills

Skills usam o formato `SKILL.md` e ficam em `~/.hera/skills` e `./.hera/skills`.

```text
hera skills import
hera skills list
hera skills show <nome>
```

O `hera skills import` copia as skills de `~/.claude/skills` sem alterar as originais, pulando `.venv`, caches e arquivos com cara de segredo.

## Agente de código e segurança

Com `--agente`, ficam disponíveis as ferramentas: `ler_arquivo`, `listar_pasta`, `buscar`, `editar_arquivo`, `escrever_arquivo`, `executar_comando`, `usar_skill` e `delegar_tarefa`.

A `delegar_tarefa` chama sub-agentes: ajudantes de contexto limpo, num modelo mais barato (faixa leve, médio ou pesado), que **só leem** o projeto e devolvem um resumo. Até 3 rodam em paralelo; não editam, não executam comandos e não criam outros sub-agentes. Quem edita é sempre o agente principal, com a sua aprovação.

O agente mostra o diff e pede aprovação com `[s]im/[n]ao/[t]odos`. Só `sim` e `todos` aplicam a mudança.

Regras de execução:

- Comandos sem shell: `&&`, `|` e `>` são bloqueados.
- Bloqueados: `git push`, `curl`, `powershell` e remoção recursiva.
- Rodam sem perguntar: `git status`, `git diff`, `git log` e leitura segura.
- `pytest` pede aprovação.
- Nunca lê `.env`, chaves ou `.git`.

Checkpoints automáticos ficam em `~/.hera/checkpoints`. O `/rollback` desfaz sem apagar o que você editou depois.

## Memória

As notas ficam em Markdown em `~/.hera/memoria`. A variável `HERA_MEMORY_DIR` muda o diretório.

```text
hera memory list
hera memory search <termo>
hera memory show <nome>
hera memory add
hera memory forget <nome>
hera memory export
```

Por padrão (desligue com `HERA_MEMORY_SNAPSHOT=0`), notas relevantes são injetadas a cada pergunta e também no começo de cada passo dos planos executados pelo servidor (painel web e planos do Claude), sem ultrapassar o limite de 4.000 caracteres da instrução. As conversas ficam salvas em SQLite e podem ser retomadas.

## Provedores e custo

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

Há mais de 50 presets, vários gratuitos. O teto de gasto diário é `gateway.daily_budget_usd` no `config.yaml`. Com valor `0`, só gratuitos e locais são usados.

## Solução de problemas

- Servidor desligado: o `hera chat` sobe sozinho; se falhar, verifique `--url`.
- Modelo indisponível (429/503): o Maestro troca sozinho; ou use `/model auto`.
- Custo alto: use `--perfil economico` ou defina `gateway.daily_budget_usd`.
- Comando bloqueado: revise as regras do agente de código.
- Mudança indesejada: use `/rollback` ou `/checkpoints`.
- Instruções do projeto não aplicadas: confira `HERA.md`, `AGENTS.md` ou `CLAUDE.md`.
