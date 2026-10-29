from __future__ import annotations

import difflib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

try:
    from app.goal import MAX_TURNOS_PADRAO
except ImportError:
    try:
        from goal import MAX_TURNOS_PADRAO  # type: ignore[no-redef]
    except ImportError:
        MAX_TURNOS_PADRAO = 5

try:
    from cli.memory_cmd import pasta_padrao
except ImportError:
    try:
        from memory_cmd import pasta_padrao  # type: ignore[no-redef]
    except ImportError:
        def pasta_padrao() -> Path:
            return Path.home() / '.hera' / 'memoria'

try:
    from app.memory.index import MemoryIndex
    from app.memory.store import MemoryStore
except ImportError:
    try:
        from memoria.index import MemoryIndex  # type: ignore[no-redef]
        from memoria.store import MemoryStore  # type: ignore[no-redef]
    except ImportError:
        MemoryIndex = Any  # type: ignore[misc,assignment]
        MemoryStore = Any  # type: ignore[misc,assignment]

try:
    from app.agente.caminhos import Raiz
    from app.agente.checkpoints import Checkpoints
    from app.agente.escrita import Escrita
    from app.agente.leitura import Leitura
except ImportError:
    try:
        from agente.caminhos import Raiz  # type: ignore[no-redef]
        from agente.checkpoints import Checkpoints  # type: ignore[no-redef]
        from agente.escrita import Escrita  # type: ignore[no-redef]
        from agente.leitura import Leitura  # type: ignore[no-redef]
    except ImportError:
        Raiz = Any  # type: ignore[misc,assignment]
        Checkpoints = Any  # type: ignore[misc,assignment]
        Escrita = Any  # type: ignore[misc,assignment]
        Leitura = Any  # type: ignore[misc,assignment]

try:
    from app.skills import Skill, resolver_skill
except ImportError:
    try:
        from skills import Skill, resolver_skill  # type: ignore[no-redef]
    except ImportError:
        Skill = Any  # type: ignore[misc,assignment]
        def resolver_skill(nome: str, s: Any) -> Any:  # type: ignore[misc]
            return None

try:
    from cli.skills_cmd import _descricao_curta, pastas_de_skills, texto_para_comando_skill
except ImportError:
    try:
        from skills_cmd import _descricao_curta, pastas_de_skills, texto_para_comando_skill  # type: ignore[no-redef]
    except ImportError:
        def _descricao_curta(t: str, limite: int = 60) -> str:  # type: ignore[misc]
            return t[:limite]
        def pastas_de_skills(*a: Any, **k: Any) -> list[Path]:  # type: ignore[misc]
            return []
        def texto_para_comando_skill(*a: Any, **k: Any) -> str | None:  # type: ignore[misc]
            return None

if TYPE_CHECKING:
    from cli.chat_repl import Sessao


@dataclass
class ConfigAgente:
    '''Configuracao do agente de codigo no chat.'''

    raiz: Any
    checkpoints: Any
    leitura: Any
    escrita: Any
    modo: str = 'guarded'
    skills: Any = None


@dataclass
class ComandoSkill:
    '''Sinaliza execucao de uma skill para virar turno do usuario.'''

    texto: str


@dataclass(frozen=True)
class ComandoGoal:
    '''Sinaliza execucao de um objetivo autonomo com teto de turnos.'''

    objetivo: str
    max_turnos: int = MAX_TURNOS_PADRAO


class EstadoChat:
    '''Estado compartilhado do chat e fabrica de sessoes sob demanda.'''

    def __init__(
        self,
        conversa_id: str | None = None,
        sessoes_de: Callable[[], Any] | None = None,
        instrucoes: Any | None = None,
        skills: Any | None = None,
    ) -> None:
        self.conversa_id = conversa_id
        self.sessoes_de = sessoes_de
        self.instrucoes = instrucoes
        self.skills = skills
        self._sessoes: Any = None

    def sessoes(self) -> Any:
        '''Cria a instancia de sessoes sob demanda e memoriza.'''
        if self._sessoes is None and self.sessoes_de is not None:
            self._sessoes = self.sessoes_de()
        return self._sessoes


def _formatar_custo(val: Any) -> str:
    try:
        f = float(val or 0)
    except (ValueError, TypeError):
        return '0'
    if f == 0:
        return '0'
    if f >= 1 and f == int(f):
        return str(int(f))
    s = f'{f:.4f}'.rstrip('0').rstrip('.')
    return s if s else '0'


def formatar_uso(dados: dict[str, Any], perguntas: int | None = None) -> list[str]:
    '''Formata estatisticas de uso em linhas para exibicao.'''
    totals = dados.get('totals') if isinstance(dados.get('totals'), dict) else dados
    req = totals.get('requests', totals.get('requisicoes', 0)) or 0
    tok = totals.get('tokens')
    if tok is None:
        tok = (totals.get('prompt_tokens', 0) or 0) + (totals.get('completion_tokens', 0) or 0)
    custo = totals.get('cost_usd', totals.get('cost', totals.get('custo', totals.get('total_cost', totals.get('gasto', 0.0))))) or 0.0
    try:
        custo_f = float(custo)
    except (ValueError, TypeError):
        custo_f = 0.0

    if custo_f == 0:
        gasto_str = 'gasto real: US$ 0 (cota gratuita)'
    else:
        gasto_str = f'gasto US$ {custo_f:.4f}'

    linhas = [f'Hoje: {req} requisicoes, {tok} tokens, {gasto_str}']

    by_provider = (
        dados.get('by_provider')
        or dados.get('por_provedor')
        or dados.get('modelos')
        or dados.get('models')
        or []
    )
    if isinstance(by_provider, dict):
        for chave, item in sorted(by_provider.items()):
            if isinstance(item, dict):
                r_req = item.get('requests', item.get('requisicoes', item.get('req', 0))) or 0
                r_tok = item.get('tokens')
                if r_tok is None:
                    r_tok = (item.get('prompt_tokens', 0) or 0) + (item.get('completion_tokens', 0) or 0)
                r_custo = item.get('cost_usd', item.get('cost', item.get('custo', item.get('gasto', 0.0)))) or 0.0
                linhas.append(f'{chave}: {r_req} req, {r_tok} tok, US$ {_formatar_custo(r_custo)}')
    elif isinstance(by_provider, list):
        for item in by_provider:
            if not isinstance(item, dict):
                continue
            prov = str(item.get('provider', item.get('provedor', '')) or '').strip()
            mod = str(item.get('model', item.get('modelo', '')) or '').strip()
            if prov and mod:
                if mod.startswith(f'{prov}/'):
                    pm = mod
                else:
                    pm = f'{prov}/{mod}'
            else:
                pm = prov or mod or 'desconhecido'

            r_req = item.get('requests', item.get('requisicoes', item.get('req', 0))) or 0
            r_tok = item.get('tokens')
            if r_tok is None:
                r_tok = (item.get('prompt_tokens', 0) or 0) + (item.get('completion_tokens', 0) or 0)
            r_custo = item.get('cost_usd', item.get('cost', item.get('custo', item.get('gasto', 0.0)))) or 0.0
            linhas.append(f'{pm}: {r_req} req, {r_tok} tok, US$ {_formatar_custo(r_custo)}')

    p = perguntas
    if p is None:
        p = dados.get('perguntas', dados.get('perguntas_sessao'))
    if p is not None:
        linhas.append(f'Nesta sessao: {p} perguntas')

    return linhas


class Comandos:
    '''Interpretador de comandos barra para o REPL do chat.'''

    _COMANDOS = [
        '/help',
        '/exit',
        '/quit',
        '/sair',
        '/clear',
        '/model',
        '/maestro',
        '/perfil',
        '/compact',
        '/resume',
        '/listar',
        '/memory',
        '/lembrar',
        '/skills',
        '/cost',
        '/instrucoes',
        '/rollback',
        '/checkpoints',
        '/modo',
        '/goal',
    ]

    def __init__(
        self,
        cliente: Any,
        estado: EstadoChat,
        agente_cfg: ConfigAgente | None = None,
        *,
        pasta_memoria: Path | None = None,
        skills: Any | None = None,
        maestro: Any | None = None,
        janela: int = 32000,
        resumir: Any | None = None,
        extrator: Any | None = None,
    ) -> None:
        self.cliente = cliente
        self.estado = estado
        self.agente_cfg = agente_cfg
        self.pasta_memoria = pasta_memoria
        self._skills = skills
        self.maestro = maestro
        self.janela = janela
        self.resumir = resumir
        self.extrator = extrator
        self._propostas: list[Any] = []

    def _obter_skills(self) -> list[Any]:
        if self._skills is not None:
            if callable(self._skills):
                res = self._skills()
                return list(res.values()) if isinstance(res, dict) else list(res)
            if isinstance(self._skills, dict):
                return list(self._skills.values())
            return list(self._skills)
        if getattr(self.estado, 'skills', None) is not None:
            s = self.estado.skills
            if callable(s):
                res = s()
                return list(res.values()) if isinstance(res, dict) else list(res)
            if isinstance(s, dict):
                return list(s.values())
            return list(s)
        try:
            from app.skills import descobrir_skills

            return descobrir_skills(pastas_de_skills())
        except Exception:
            return []

    async def executar(
        self,
        linha: str,
        sessao: Sessao,
        escrever: Callable[[str], None],
    ) -> Any:
        '''Executa um comando de barra. Devolve sair, ComandoSkill, ComandoGoal ou None.'''
        try:
            texto = linha.strip()
            if not texto:
                return None

            partes = texto.split(maxsplit=1)
            cmd = partes[0].lower()
            arg = partes[1].strip() if len(partes) > 1 else ''

            if cmd in ('/exit', '/quit', '/sair'):
                return 'sair'

            if cmd == '/help':
                linhas = [
                    '/help - Mostra todos os comandos disponiveis.',
                    '/exit - Encerra o chat.',
                    '/quit - Encerra o chat.',
                    '/sair - Encerra o chat.',
                    '/clear - Esvazia o historico e inicia nova conversa.',
                    '/model [nome|auto] - Exibe ou altera o modelo atual.',
                    '/maestro [on|off|status] - Controla o orquestrador de modelos.',
                    '/perfil [economico|qualidade|padrao] - Exibe ou altera o perfil do maestro.',
                    '/compact - Compacta o contexto da conversa.',
                    '/resume [ref] - Retoma uma conversa salva.',
                    '/listar - Lista conversas salvas.',
                    '/memory [consulta] - Consulta ou lista a memoria.',
                    '/lembrar [sim [numeros]|nao] - Propoe notas da conversa para a memoria; so grava o que voce aprovar.',
                    '/skills - Mostra as skills disponiveis.',
                    '/cost - Mostra o custo da sessao.',
                    '/instrucoes - Lista os arquivos de instrucoes carregados.',
                    '/rollback [id] - Reverte alteracoes de arquivo feitas pelo agente.',
                    '/checkpoints - Lista os ultimos checkpoints de arquivos.',
                    '/modo [read-only|guarded|workspace] - Exibe ou altera o modo do agente.',
                    '/goal [N] <objetivo> - Trabalha em turnos ate concluir o objetivo.',
                ]
                for item in linhas:
                    escrever(f'{item}\n')
                return None

            if cmd == '/clear':
                sessao.mensagens.clear()
                self.estado.conversa_id = None
                escrever('Conversa nova.\n')
                return None

            if cmd == '/model':
                atual = getattr(self.cliente, 'modelo', getattr(sessao, 'modelo', 'default'))
                if not arg:
                    lista: list[str] | None
                    try:
                        lista = await self.cliente.modelos()
                    except Exception:
                        lista = None
                    seletor = getattr(self, 'seletor', None)
                    if seletor is not None and lista:
                        opcoes = ['auto'] + [m for m in lista if m != 'auto']
                        escolha = await seletor('Escolha o modelo (setas e Enter; Ctrl+C cancela)', opcoes, atual)
                        if escolha is None:
                            escrever('Troca de modelo cancelada.\n')
                            return None
                        arg = escolha
                    else:
                        escrever(f'Modelo atual: {atual}\n')
                        if lista:
                            for mid in lista[:15]:
                                escrever(f'  {mid}\n')
                        escrever('Para trocar: /model <nome>  ou  /model auto\n')
                        return None

                if arg.lower() == 'auto':
                    if self.maestro is not None:
                        self.maestro.forcado = None
                        self.maestro.ativo = True
                    escrever('Maestro ativado (selecao automatica de modelo).\n')
                    return None

                nome = arg
                try:
                    lista = await self.cliente.modelos()
                except Exception:
                    lista = None

                if lista is not None and nome not in lista:
                    escrever(f'Modelo desconhecido: {nome}\n')
                    return None

                self.cliente.modelo = nome
                sessao.modelo = nome
                if self.maestro is not None:
                    self.maestro.forcado = nome
                escrever(f'Modelo alterado para {nome}.\n')
                return None

            if cmd == '/maestro':
                if self.maestro is None:
                    escrever('Maestro nao configurado nesta sessao.\n')
                    return None

                sub = arg.lower()
                if sub == 'on':
                    self.maestro.ativo = True
                    escrever('Maestro ativado.\n')
                    return None

                if sub == 'off':
                    self.maestro.ativo = False
                    escrever('Maestro desativado.\n')
                    return None

                if sub in ('status', ''):
                    ativo_str = 'sim' if self.maestro.ativo else 'nao'
                    perfil_str = self.maestro.perfil or 'padrao'
                    forcado_str = self.maestro.forcado or 'nenhum'
                    escrever(
                        f'Maestro:\n  Ativo: {ativo_str}\n  Perfil: {perfil_str}\n  Forcado: {forcado_str}\n'
                    )
                    escrever('Modelos por faixa:\n')
                    prefs = (
                        self.maestro._prefs_efetivas()
                        if hasattr(self.maestro, '_prefs_efetivas')
                        else {}
                    )
                    for faixa in ('leve', 'medio', 'pesado'):
                        mods = ', '.join(prefs.get(faixa, []))
                        escrever(f'  {faixa}: {mods}\n')
                    return None

                escrever(f'Subcomando invalido: {arg}. Use: /maestro [on|off|status]\n')
                return None

            if cmd == '/perfil':
                if not arg:
                    atual = (
                        self.maestro.perfil
                        if self.maestro and self.maestro.perfil
                        else 'padrao'
                    )
                    escrever(f'Perfil atual: {atual}\n')
                    return None

                sub = arg.lower()
                if sub not in ('economico', 'qualidade', 'padrao'):
                    escrever(
                        f'Perfil invalido: {arg}. Validos: economico, qualidade, padrao\n'
                    )
                    return None

                novo_perfil = None if sub == 'padrao' else sub
                if self.maestro is not None:
                    self.maestro.perfil = novo_perfil
                escrever(f'Perfil alterado para {sub}.\n')
                return None

            if cmd == '/compact':
                if not sessao.mensagens:
                    escrever('Nenhuma mensagem para compactar.\n')
                    return None
                try:
                    from app.compressao import compactar as compactar_fn
                    from cli.chat_repl import criar_resumidor

                    resumir_fn = self.resumir or criar_resumidor(
                        self.cliente, self.maestro
                    )
                    novas, rel = await compactar_fn(
                        sessao.mensagens,
                        janela_tokens=self.janela,
                        gatilho=0.0,
                        resumir=resumir_fn,
                    )
                    sessao.mensagens[:] = novas
                    escrever(
                        f'Contexto compactado: {rel.antes} -> {rel.depois} tokens ({rel.resumidas} mensagens resumidas).\n'
                    )
                except Exception as exc:
                    escrever(f'Nao foi possivel compactar: {exc}\n')
                return None

            if cmd == '/resume':
                sessoes = self.estado.sessoes()
                if sessoes is None:
                    escrever('Nenhuma conversa encontrada.\n')
                    return None
                conv = sessoes.resolver(arg or None)
                if conv is None:
                    escrever('Nenhuma conversa encontrada.\n')
                    return None
                cid = conv['id']
                mensagens = sessoes.carregar(cid)
                sessao.mensagens[:] = mensagens
                self.estado.conversa_id = cid
                titulo = conv.get('title') or conv.get('titulo') or ''
                escrever(f'Retomando: {titulo} ({len(mensagens)} mensagens)\n')
                return None

            if cmd == '/listar':
                sessoes = self.estado.sessoes()
                if sessoes is None:
                    escrever('Nenhuma conversa salva.\n')
                    return None
                itens = sessoes.listar()
                if not itens:
                    escrever('Nenhuma conversa salva.\n')
                    return None
                for item in itens:
                    cid = item.get('id', '')
                    atualizada = item.get('atualizada', '')
                    titulo = item.get('titulo', '')
                    msgs = item.get('mensagens', 0)
                    pasta = item.get('pasta', '')
                    escrever(f'{cid} | {atualizada} | {titulo} | {msgs} msgs | {pasta}\n')
                return None

            if cmd == '/memory':
                pasta = self.pasta_memoria if self.pasta_memoria is not None else pasta_padrao()
                if not pasta.exists():
                    escrever('Memoria vazia.\n')
                    return None

                store = MemoryStore(pasta)
                if not arg:
                    notas = store.listar()[:20]
                    if not notas:
                        escrever('Memoria vazia.\n')
                        return None
                    for nota in notas:
                        escrever(f'{nota.nome} | {nota.tipo} | {nota.descricao}\n')
                    return None

                index = MemoryIndex(store, pasta / '.indice.sqlite')
                try:
                    resultados = index.buscar(arg, limite=5)
                    if not resultados:
                        escrever('Nada encontrado.\n')
                        return None
                    for res in resultados:
                        escrever(f'{res.nome} [{res.tipo}] {res.trecho}\n')
                finally:
                    index.close()
                return None

            if cmd == '/lembrar':
                from app.memory.resumo import propor_resumo_async, salvar_aprovadas

                pasta = self.pasta_memoria if self.pasta_memoria is not None else pasta_padrao()
                palavras = arg.lower().split()
                if palavras and palavras[0] in ('nao',):
                    self._propostas = []
                    escrever('Propostas descartadas.\n')
                    return None
                if palavras and palavras[0] in ('sim', 's'):
                    if not self._propostas:
                        escrever('Nao ha propostas. Use /lembrar primeiro.\n')
                        return None
                    escolhidas = {int(x) for x in palavras[1:] if x.isdigit()}
                    pasta.mkdir(parents=True, exist_ok=True)
                    numeradas = {id(p): i for i, p in enumerate(self._propostas, 1)}
                    rel = salvar_aprovadas(
                        MemoryStore(pasta),
                        self._propostas,
                        lambda p: not escolhidas or numeradas[id(p)] in escolhidas,
                    )
                    self._propostas = []
                    escrever(
                        f"Salvas: {', '.join(rel['salvas']) or 'nenhuma'}. "
                        f"Puladas (ja existiam): {', '.join(rel['puladas']) or 'nenhuma'}.\n"
                    )
                    return None
                if not sessao.mensagens:
                    escrever('Nenhuma mensagem para resumir.\n')
                    return None
                from cli.chat_repl import criar_resumidor

                chamar = self.extrator or criar_resumidor(
                    self.cliente, self.maestro,
                    sistema='Responda somente com JSON valido, sem texto fora do JSON.',
                )
                self._propostas = await propor_resumo_async(sessao.mensagens, chamar)
                if not self._propostas:
                    escrever('Nada duradouro para lembrar nesta conversa.\n')
                    return None
                escrever('Previa do que seria salvo na memoria (nada foi gravado):\n')
                for i, p in enumerate(self._propostas, 1):
                    escrever(f'{i}. {p.nome} [{p.tipo}] {p.descricao}\n   {p.corpo[:200]}\n')
                escrever('Para gravar: /lembrar sim (todas) ou /lembrar sim 1 3. Para descartar: /lembrar nao.\n')
                return None

            if cmd == '/skills':
                skills = self._obter_skills()
                if not skills:
                    escrever('Nenhuma skill disponivel.\n')
                    return None
                for s in skills:
                    desc = getattr(s, 'descricao', '') or ''
                    if desc:
                        desc_curta = _descricao_curta(desc)
                        escrever(f'{s.nome} - {desc_curta}\n')
                    else:
                        escrever(f'{s.nome}\n')
                    for av in getattr(s, 'avisos', ()):
                        escrever(f'  aviso: {av}\n')
                return None

            if cmd == '/cost':
                try:
                    if hasattr(self.cliente, 'uso'):
                        dados = await self.cliente.uso(1)
                    else:
                        dados = {}
                except Exception as exc:
                    escrever(f'Nao foi possivel obter custo: {exc}\n')
                    return None
                perguntas = sum(1 for m in getattr(sessao, 'mensagens', []) if m.get('role') == 'user')
                for linha in formatar_uso(dados, perguntas=perguntas):
                    escrever(f'{linha}\n')
                return None

            if cmd == '/instrucoes':
                instrucoes = getattr(self.estado, 'instrucoes', None)
                fontes = getattr(instrucoes, 'fontes', ()) if instrucoes is not None else ()
                avisos = getattr(instrucoes, 'avisos', ()) if instrucoes is not None else ()

                if not fontes:
                    escrever('Nenhuma instrucao carregada. Crie um HERA.md na pasta do projeto.\n')
                    for av in avisos:
                        prefixo = '' if av.startswith('Aviso:') else 'Aviso: '
                        escrever(f'{prefixo}{av}\n')
                    return None

                for f in fontes:
                    cortado = ', cortado' if getattr(f, 'truncada', False) else ''
                    escrever(f'{f.caminho} ({f.origem}, {f.bytes_lidos} bytes{cortado})\n')
                for av in avisos:
                    prefixo = '' if av.startswith('Aviso:') else 'Aviso: '
                    escrever(f'{prefixo}{av}\n')
                return None

            if cmd == '/rollback':
                if self.agente_cfg is None:
                    escrever('Agente desligado. Use: hera chat --agente\n')
                    return None
                cid = arg if arg else None
                relatorio = self.agente_cfg.checkpoints.reverter(cid)
                for r in relatorio.restaurados:
                    escrever(f'Restaurado: {r}\n')
                for p in relatorio.pulados:
                    escrever(f'Pulado: {p}\n')
                for av in relatorio.avisos:
                    escrever(f'Aviso: {av}\n')
                return None

            if cmd == '/checkpoints':
                if self.agente_cfg is None:
                    escrever('Agente desligado. Use: hera chat --agente\n')
                    return None
                entradas = self.agente_cfg.checkpoints.listar(limite=10)
                if not entradas:
                    escrever('Nenhum checkpoint registrado.\n')
                    return None
                for e in entradas[:10]:
                    revertido = 'sim' if getattr(e, 'revertido', False) else 'nao'
                    escrever(f'{e.id} | {e.arquivo} | {revertido}\n')
                return None

            if cmd == '/modo':
                if self.agente_cfg is None:
                    escrever('Agente desligado. Use: hera chat --agente\n')
                    return None
                if not arg:
                    modo_atual = getattr(self.agente_cfg.escrita, 'modo', getattr(self.agente_cfg, 'modo', 'guarded'))
                    escrever(f'Modo atual: {modo_atual}\n')
                    return None
                novo_modo = arg.lower()
                modos_validos = ('read-only', 'guarded', 'workspace')
                if novo_modo not in modos_validos:
                    validos_str = ', '.join(modos_validos)
                    escrever(f'Modo invalido: {arg}. Validos: {validos_str}\n')
                    return None
                self.agente_cfg.escrita.modo = novo_modo
                self.agente_cfg.modo = novo_modo
                escrever(f'Modo alterado para {novo_modo}.\n')
                return None

            if cmd == '/goal':
                uso_goal = (
                    '/goal [N] <objetivo>: o agente trabalha em turnos '
                    '(no maximo N, padrao 5) ate um juiz dizer que acabou; '
                    'cada acao continua pedindo sua aprovacao; Ctrl+C interrompe'
                )
                if not arg:
                    escrever(f'{uso_goal}\n')
                    return None

                partes_arg = arg.split(maxsplit=1)
                try:
                    n = int(partes_arg[0])
                    if n < 1 or n > 10:
                        escrever(f'{uso_goal}\n')
                        return None
                    if len(partes_arg) < 2 or not partes_arg[1].strip():
                        escrever(f'{uso_goal}\n')
                        return None
                    return ComandoGoal(partes_arg[1].strip(), n)
                except ValueError:
                    return ComandoGoal(arg, MAX_TURNOS_PADRAO)

            # Linha /nome que case com uma skill vira comando de skill
            nome_candidato = cmd.lstrip('/')
            skills = self._obter_skills()
            skill_resolvida = None
            if skills:
                try:
                    skill_resolvida = resolver_skill(nome_candidato, skills)
                except Exception:
                    skill_resolvida = None

            if skill_resolvida is not None:
                texto_skill = texto_para_comando_skill(
                    nome_candidato, skills, arg
                )
                if texto_skill:
                    return ComandoSkill(texto=texto_skill)

            sugestoes = difflib.get_close_matches(cmd, self._COMANDOS, n=1, cutoff=0.5)
            if sugestoes:
                escrever(f'Comando desconhecido: {cmd}. Quis dizer {sugestoes[0]}?\n')
            else:
                escrever(f'Comando desconhecido: {cmd}. Use /help para ver os comandos disponiveis.\n')
            return None

        except Exception as exc:
            msg = str(exc) or type(exc).__name__
            escrever(f'Erro: {msg}\n')
            return None
