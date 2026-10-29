from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from cli.chat_client import ClienteHera, ErroDoServidor
from cli.chat_comandos import (
    ComandoGoal,
    ComandoSkill,
    Comandos,
    ConfigAgente,
    EstadoChat,
)
from cli.chat_ferramentas import conversar_com_ferramentas
from cli.interrupcao import InterruptorSigint

try:
    from app.agente.laco import Agente
except ImportError:
    try:
        from agente.laco import Agente  # type: ignore[no-redef]
    except ImportError:
        Agente = Any  # type: ignore[misc,assignment]

try:
    from app.agente.escrita import Escrita
except ImportError:
    try:
        from agente.escrita import Escrita  # type: ignore[no-redef]
    except ImportError:
        Escrita = Any  # type: ignore[misc,assignment]

try:
    from app.memory.integracao import contexto_da_memoria
except ImportError:
    try:
        from app.memory_integracao import contexto_da_memoria  # type: ignore[no-redef]
    except ImportError:
        def contexto_da_memoria(instrucao: str, **kwargs: Any) -> str:  # type: ignore[misc]
            return ''

try:
    from app.maestro import Escolha, Maestro, escolher_modelo
except ImportError:
    Escolha = Any  # type: ignore[misc,assignment]
    Maestro = Any  # type: ignore[misc,assignment]
    escolher_modelo = Any  # type: ignore[misc,assignment]

try:
    from app.compressao import compactar as compactar_fn, estimar_tokens
except ImportError:
    compactar_fn = None  # type: ignore[assignment]
    estimar_tokens = None  # type: ignore[assignment]

try:
    from app.goal import perseguir_objetivo
except ImportError:
    try:
        from goal import perseguir_objetivo  # type: ignore[no-redef]
    except ImportError:
        perseguir_objetivo = Any  # type: ignore[misc,assignment]


class Sessao:
    '''Estado de uma conversa com o Hera.'''

    def __init__(
        self,
        modelo: str = 'default',
        sistema: str = '',
        agente: Agente | None = None,
    ) -> None:
        self.mensagens: list[dict] = []
        self.modelo = modelo
        self.sistema = sistema
        self.agente = agente


def criar_resumidor(
    cliente: ClienteHera,
    maestro: Maestro | None = None,
    sistema: str | None = None,
) -> Callable[[str], Awaitable[str]]:
    '''Cria funcao de resumo usando modelo leve do maestro ou modelo atual.'''

    async def resumir(texto: str) -> str:
        modelo_orig = getattr(cliente, 'modelo', 'default')
        modelo_leve = modelo_orig
        if maestro is not None:
            try:
                disp = await maestro._disponiveis()
                m = escolher_modelo(
                    'leve',
                    disp,
                    maestro._prefs_efetivas(),
                    indisponiveis=maestro._indisponiveis_ativos(),
                )
                if m:
                    modelo_leve = m
            except Exception:
                modelo_leve = modelo_orig
        cliente.modelo = modelo_leve
        try:
            msgs = [
                {
                    'role': 'system',
                    'content': sistema or 'Resuma em ate 12 linhas o trabalho abaixo, mantendo decisoes, arquivos tocados e pendencias.',
                },
                {'role': 'user', 'content': texto},
            ]
            partes: list[str] = []
            async for pedaco in cliente.conversar(msgs):
                partes.append(pedaco)
            return ''.join(partes)
        finally:
            cliente.modelo = modelo_orig

    return resumir


def criar_juiz(
    cliente: ClienteHera,
    maestro: Maestro | None = None,
) -> Callable[[str], Awaitable[str]]:
    '''Cria funcao de juiz usando modelo leve do maestro ou modelo atual.'''

    async def julgar(prompt: str) -> str:
        modelo_orig = getattr(cliente, 'modelo', 'default')
        modelo_leve = modelo_orig
        if maestro is not None:
            try:
                disp = await maestro._disponiveis()
                m = escolher_modelo(
                    'leve',
                    disp,
                    maestro._prefs_efetivas(),
                    indisponiveis=maestro._indisponiveis_ativos(),
                )
                if m:
                    modelo_leve = m
            except Exception:
                modelo_leve = modelo_orig
        cliente.modelo = modelo_leve
        try:
            msgs = [
                {
                    'role': 'system',
                    'content': 'Voce e um juiz objetivo e conservador.',
                },
                {'role': 'user', 'content': prompt},
            ]
            partes: list[str] = []
            async for pedaco in cliente.conversar(msgs):
                partes.append(pedaco)
            return ''.join(partes)
        finally:
            cliente.modelo = modelo_orig

    return julgar


# Modelos para decidir a faixa, do mais rapido ao menos (medido em 29/10/2026: DeepSeek 2 s, Gemini lite 14 a 28 s).
MODELOS_JUIZ_FAIXA = ('deepseek-ai/DeepSeek-V4.1-Flash', 'gemini-flash-lite-latest', 'agnes-2.5-flash')


def criar_juiz_de_faixa(
    cliente: ClienteHera,
    maestro: Maestro,
) -> Callable[[str], Awaitable[str | None]]:
    '''Juiz do maestro: modelo rapido (cliente proprio, sem mexer no modelo da conversa) que responde so a faixa.'''
    from app.maestro import PROMPT_FAIXA

    async def julgar_faixa(pedido: str) -> str | None:
        disponiveis = await maestro._disponiveis()
        indisponiveis = maestro._indisponiveis_ativos()
        modelo = next((m for m in MODELOS_JUIZ_FAIXA if m in disponiveis and m not in indisponiveis), None)
        if modelo is None:
            return None
        irmao = ClienteHera(
            getattr(cliente, 'base_url', ''),
            getattr(cliente, 'token', ''),
            modelo,
            transporte=getattr(cliente, '_transporte', getattr(cliente, 'transporte', None)),
            timeout=getattr(cliente, '_timeout', getattr(cliente, 'timeout', 120.0)),
        )
        mensagens = [
            {'role': 'system', 'content': 'Voce e um juiz objetivo e conservador.'},
            {'role': 'user', 'content': PROMPT_FAIXA + pedido},
        ]
        partes: list[str] = []
        async for pedaco in irmao.conversar(mensagens):
            partes.append(pedaco)
        return ''.join(partes)

    return julgar_faixa


async def rodar(
    cliente: ClienteHera,
    ler_linha: Callable[[], Awaitable[str]],
    escrever: Callable[[str], None],
    *,
    sessao: Optional[Sessao] = None,
    interruptor: Optional[InterruptorSigint] = None,
    ao_turno: Optional[Callable[[str, str], None]] = None,
    comandos: Optional[Comandos] = None,
    agente: Agente | None = None,
    memoria_de: Optional[Callable[[str], str] | Callable[[str], Awaitable[str]]] = None,
    maestro: Optional[Maestro] = None,
    janela: int = 32000,
    resumir: Optional[Callable[[str], Awaitable[str]]] = None,
    anexos: Optional[Callable[[str], tuple]] = None,
    imagens: Optional[Callable[[str], tuple]] = None,
    julgar: Optional[Callable[[str], Awaitable[str]]] = None,
) -> Sessao:
    '''Executa o laco de conversa ate o usuario encerrar.'''
    if interruptor is None:
        interruptor = InterruptorSigint()

    def salvar(pergunta: str, resposta: str) -> None:
        # Persistencia e opcional e nunca derruba a conversa: falhou, avisa e segue.
        if ao_turno is None:
            return
        try:
            ao_turno(pergunta, resposta)
        except Exception as exc:  # noqa: BLE001
            escrever(f'Aviso: nao foi possivel salvar a conversa ({exc}).\n')

    if sessao is None:
        sessao = Sessao(
            modelo=getattr(cliente, 'modelo', 'default'), agente=agente
        )
    elif agente is None and sessao.agente is not None:
        agente = sessao.agente
    elif sessao.agente is None and agente is not None:
        sessao.agente = agente

    resumidor_efetivo = resumir or criar_resumidor(cliente, maestro)
    juiz_do_chat = julgar is None
    if julgar is None:
        julgar = criar_juiz(cliente, maestro)
    if maestro is not None and hasattr(maestro, 'timeout_juiz') and getattr(maestro, 'juiz', None) is None:
        from app.maestro import PROMPT_FAIXA

        # Pedido que a regra local nao sabe pesar: um modelo rapido decide a faixa (falhou ou demorou, vale a regra local).
        if juiz_do_chat:
            maestro.juiz = criar_juiz_de_faixa(cliente, maestro)
        else:
            maestro.juiz = lambda pedido: julgar(PROMPT_FAIXA + pedido)

    async def oferecer_memoria() -> None:
        '''Ao sair de uma conversa de 3 ou mais perguntas, oferece guardar o que importa (com previa e aprovacao).'''
        perguntas = sum(1 for m in sessao.mensagens if isinstance(m, dict) and m.get('role') == 'user')
        if comandos is None or perguntas < 3:
            return
        try:
            escrever('Salvar o que importa desta conversa na memoria? [s/N]\n')
            if (await ler_linha()).strip().lower() not in ('s', 'sim'):
                return
            await comandos.executar('/lembrar', sessao, escrever)
            if not getattr(comandos, '_propostas', None):
                return
            escrever('Gravar tudo isso? [s/N]\n')
            resposta = (await ler_linha()).strip().lower()
            await comandos.executar('/lembrar sim' if resposta in ('s', 'sim') else '/lembrar nao', sessao, escrever)
        except (Exception, KeyboardInterrupt):  # noqa: BLE001 - sair nunca pode travar por causa da memoria
            escrever('Aviso: nao foi possivel salvar na memoria.\n')

    primeira_vez = True
    modelo_anterior: str | None = None

    async def executar_turno(texto: str) -> str:
        nonlocal primeira_vez, modelo_anterior

        # Imagens citadas na pergunta (@print.png, caminho arrastado, @clip): lidas antes de tudo; no texto fica '[imagem: nome]'.
        lista_imagens: list = []
        if imagens is not None:
            try:
                texto, lista_imagens, avisos_imagens = imagens(texto)
                for aviso in avisos_imagens:
                    escrever(f'[imagens: {aviso}]\n')
            except Exception:  # noqa: BLE001 - imagem e conveniencia: na duvida, segue so com o texto
                lista_imagens = []

        # Contexto de memoria por turno: executado em thread e nunca derruba o turno
        memoria = ''
        try:
            if memoria_de is not None:
                if asyncio.iscoroutinefunction(memoria_de):
                    memoria = await memoria_de(texto)
                else:
                    memoria = await asyncio.to_thread(memoria_de, texto)
            else:
                memoria = await asyncio.to_thread(contexto_da_memoria, texto)
        except Exception:
            memoria = ''

        memoria = memoria.strip() if memoria else ''

        # Escolha do maestro para perguntas normais
        escolha: Any = None
        if maestro is not None:
            escolha = await maestro.escolher(texto)
            cliente.modelo = escolha.modelo
            sessao.modelo = escolha.modelo
            mudou = modelo_anterior is not None and escolha.modelo != modelo_anterior
            deve_escrever = (
                (primeira_vez or mudou)
                and not getattr(escolha, 'forcada', False)
                and getattr(maestro, 'ativo', True)
            )
            if deve_escrever:
                por_juiz = ' (decidido pelo juiz)' if str(getattr(escolha, 'motivo', '')).startswith('juiz') else ''
                escrever(f'[maestro: faixa {escolha.peso}{por_juiz} -> {escolha.modelo}]\n')
            primeira_vez = False
            modelo_anterior = escolha.modelo

        if lista_imagens:
            from cli.chat_imagens import escolher_modelo_visao, modelo_enxerga

            escrever('[imagem: ' + ', '.join(f'{i.nome} ({max(1, round(i.tamanho_kb))} KB)' for i in lista_imagens) + ']\n')
            if not modelo_enxerga(str(getattr(cliente, 'modelo', ''))):
                novo = None
                if maestro is not None and getattr(maestro, 'ativo', True) and not getattr(escolha, 'forcada', False):
                    try:
                        novo = escolher_modelo_visao(await maestro._disponiveis(), maestro._indisponiveis_ativos())
                    except Exception:  # noqa: BLE001
                        novo = None
                if novo:
                    cliente.modelo = novo
                    sessao.modelo = novo
                    modelo_anterior = novo
                    escrever(f'[maestro: imagem -> {novo}]\n')
                else:
                    escrever(f'Aviso: o modelo {cliente.modelo} pode nao enxergar imagens.\n')

        texto_modelo = texto
        if anexos is not None:  # '@arquivo' na pergunta: o MODELO recebe o conteudo; o historico gravado guarda o que o usuario digitou
            try:
                texto_modelo, avisos_anexos, lista_anexos = anexos(texto)
                for aviso in avisos_anexos:
                    escrever(f'[anexos: {aviso}]\n')
                if lista_anexos:
                    from cli.chat_anexos import resumo_anexos

                    escrever(resumo_anexos(lista_anexos) + '\n')
            except Exception:  # noqa: BLE001 - anexar e conveniencia: na duvida, segue com a pergunta original
                texto_modelo = texto
        if lista_imagens:
            from cli.chat_imagens import montar_conteudo

            sessao.mensagens.append({'role': 'user', 'content': montar_conteudo(texto_modelo, lista_imagens)})
        else:
            sessao.mensagens.append({'role': 'user', 'content': texto_modelo})

        # Compactacao antes do envio
        if compactar_fn is not None and estimar_tokens is not None:
            try:
                if estimar_tokens(sessao.mensagens) > (0.5 * janela):
                    novas, rel = await compactar_fn(
                        sessao.mensagens,
                        janela_tokens=janela,
                        resumir=resumidor_efetivo,
                    )
                    sessao.mensagens[:] = novas
                    if rel.depois < rel.antes:
                        escrever(
                            f'[contexto compactado: {rel.antes} -> {rel.depois} tokens]\n'
                        )
            except Exception:
                pass

        resposta = ''
        interrompido = False

        sistemas: list[dict[str, str]] = []
        if sessao.sistema:
            sistemas.append({'role': 'system', 'content': sessao.sistema})
        if memoria:
            sistemas.append({'role': 'system', 'content': memoria})

        mensagens_a_enviar = sistemas + sessao.mensagens

        async def consumir() -> None:
            nonlocal resposta
            if agente is not None:
                sis_itens = [s for s in (sessao.sistema, memoria) if s]
                sis_arg: Any = (
                    sis_itens if len(sis_itens) > 1 else (sis_itens[0] if sis_itens else '')
                )
                resposta = await agente.turno(sessao.mensagens, sis_arg)
            else:
                async for pedaco in cliente.conversar(mensagens_a_enviar):
                    resposta += pedaco
                    escrever(pedaco)

        # A resposta roda numa tarefa propria: o Ctrl+C (interruptor) cancela so ela, quantas vezes for preciso.
        # O handler padrao do asyncio.run so cancela a tarefa principal na 1a vez e derruba o programa na 2a.
        tarefa = asyncio.ensure_future(consumir())
        try:
            with interruptor.vigiar(tarefa):
                await tarefa
        except ErroDoServidor as erro:
            status_retry = (0, 429, 502, 503, 504)
            if maestro is not None and erro.status in status_retry:
                modelo_falhou = (
                    escolha.modelo
                    if escolha is not None
                    else getattr(cliente, 'modelo', 'default')
                )
                maestro.marcar_indisponivel(modelo_falhou)
                nova_escolha = await maestro.escolher(texto)
                cliente.modelo = nova_escolha.modelo
                sessao.modelo = nova_escolha.modelo
                modelo_anterior = nova_escolha.modelo
                escrever(
                    f'[maestro: {modelo_falhou} indisponivel, tentando {nova_escolha.modelo}]\n'
                )
                resposta = ''
                tarefa_retry = asyncio.ensure_future(consumir())
                try:
                    with interruptor.vigiar(tarefa_retry):
                        await tarefa_retry
                except ErroDoServidor as erro2:
                    escrever(f'Erro ({erro2.status}): {erro2.mensagem}\n')
                    sessao.mensagens.pop()
                    raise RuntimeError(erro2.mensagem)
                except asyncio.CancelledError:
                    atual = asyncio.current_task()
                    if atual is not None and atual.cancelling():
                        tarefa_retry.cancel()
                        raise
                    interrompido = True
            else:
                escrever(f'Erro ({erro.status}): {erro.mensagem}\n')
                sessao.mensagens.pop()
                raise RuntimeError(erro.mensagem)
        except asyncio.CancelledError:
            atual = asyncio.current_task()
            if atual is not None and atual.cancelling():
                tarefa.cancel()
                raise  # cancelamento de quem chamou (encerrar o programa): nao engolir
            interrompido = True

        if interrompido:
            escrever('\n[interrompido]\n')
            if resposta:
                sessao.mensagens.append(
                    {'role': 'assistant', 'content': resposta}
                )
                salvar(texto, resposta)
            else:
                sessao.mensagens.pop()
            raise RuntimeError('interrompido')

        escrever('\n')
        sessao.mensagens.append({'role': 'assistant', 'content': resposta})
        salvar(texto, resposta)
        return resposta

    while True:
        try:
            linha = await ler_linha()
        except EOFError:
            break
        except KeyboardInterrupt:
            break

        texto = linha.strip()
        if not texto:
            continue

        if comandos is not None and texto.startswith('/'):
            resultado = await comandos.executar(texto, sessao, escrever)
            if resultado == 'sair':
                await oferecer_memoria()
                break
            if isinstance(resultado, ComandoSkill):
                texto = resultado.texto
            elif isinstance(resultado, ComandoGoal):
                cmd = resultado
                res = await perseguir_objetivo(
                    cmd.objetivo,
                    turno=executar_turno,
                    julgar=julgar,
                    max_turnos=cmd.max_turnos,
                    escrever=escrever,
                )
                escrever(
                    f'[goal encerrado: {res.estado} em {res.turnos} turno(s) - {res.motivo}]\n'
                )
                continue
            elif hasattr(resultado, 'texto') and not isinstance(resultado, str):
                texto = resultado.texto
            elif hasattr(resultado, 'texto_usuario'):
                texto = resultado.texto_usuario
            else:
                continue

        if texto in ('/exit', '/quit', '/sair'):
            await oferecer_memoria()
            break

        try:
            await executar_turno(texto)
        except RuntimeError:
            continue

    return sessao


def _encostar_no_fundo(sessao_prompt: Any) -> bool:
    '''Salva o cursor e desce o campo de entrada ate o fim da tela, sem criar linhas em branco (como o Claude Code).

    Devolve True se o cursor foi salvo: quem le a linha deve restaurar (ESC 8) e apagar o que ficou abaixo, para a
    conversa continuar logo depois do conteudo e nao do fundo da tela. Sem espaco ou sem suporte, nao faz nada.
    '''
    try:
        abaixo = sessao_prompt.output.get_rows_below_cursor_position()
    except Exception:  # noqa: BLE001
        return False
    if abaixo < 5:  # regua de cima, entrada, regua de baixo, rodape e uma folga: sem isso a tela rolaria
        return False
    print('\x1b7' + (f'\x1b[{abaixo - 4}B' if abaixo > 4 else ''), end='', flush=True)
    return True


async def principal(
    base_url: str,
    token: str,
    modelo: str = 'default',
    *,
    mensagens_iniciais: Optional[list[dict]] = None,
    ao_turno: Optional[Callable[[str, str], None]] = None,
    estado: Optional[EstadoChat] = None,
    sistema: str = '',
    agente_cfg: Optional[ConfigAgente] = None,
    skills: Any = None,
    maestro: Optional[Maestro] = None,
    janela: int = 32000,
    resumir: Optional[Callable[[str], Awaitable[str]]] = None,
    cliente: Optional[ClienteHera] = None,
    julgar: Optional[Callable[[str], Awaitable[str]]] = None,
) -> None:
    '''Ponto de entrada interativo usando prompt_toolkit.'''
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.styles import Style

    from cli.chat_ui import Visual

    visual = Visual()
    sessao_prompt = PromptSession(style=Style.from_dict({'bottom-toolbar': 'noreverse'}))
    modo_agente = getattr(agente_cfg, 'modo', None) if agente_cfg is not None else None
    pasta_atual = str(Path.cwd())

    def rodape() -> ANSI:
        forcado = getattr(maestro, 'forcado', None) if maestro is not None else None
        ativo = maestro is not None and getattr(maestro, 'ativo', True) and not forcado
        atual = forcado or getattr(cliente, 'modelo', modelo)
        return ANSI(visual.rodape(modelo=atual, modo=modo_agente or '', maestro=bool(ativo), pasta=pasta_atual))

    async def seletor(titulo: str, opcoes: list[str], atual: str) -> Optional[str]:
        from cli.chat_seletor import selecionar

        itens = [(o, 'auto (o maestro escolhe)' if o == 'auto' else o) for o in opcoes]
        try:
            return await selecionar(titulo, itens, atual if atual in opcoes else None)
        except (KeyboardInterrupt, EOFError):
            return None

    async def ler_linha() -> str:
        if visual.pensando_visivel:
            print(visual.apagar_linha(), end='', flush=True)
        visual.novo_turno()
        no_fundo = visual.cor and _encostar_no_fundo(sessao_prompt)
        try:
            texto = await sessao_prompt.prompt_async(
                ANSI(visual.prompt()), bottom_toolbar=rodape, placeholder=ANSI(visual.placeholder())
            )
        finally:
            if no_fundo:
                print('\x1b8\x1b[J', end='', flush=True)  # volta ao fim do conteudo e apaga campo e rodape
        if no_fundo:
            print(visual.eco(texto), end='', flush=True)
        if texto.strip() and not texto.lstrip().startswith('/'):
            print(visual.marcar_pensando(), end='', flush=True)
            asyncio.ensure_future(animar_pensando())
        return texto

    async def animar_pensando() -> None:
        '''Atualiza o indicador a cada meio segundo (simbolo e tempo) ate a resposta comecar; para sozinho.'''
        while visual.pensando_visivel:
            await asyncio.sleep(0.5)
            quadro = visual.tick_pensando()
            if quadro:
                print(quadro, end='', flush=True)

    def escrever(texto: str) -> None:
        print(visual.estilizar(texto), end='', flush=True)

    async def aprovar_interativo(p: Any) -> str:
        if visual.pensando_visivel:  # o pedido de aprovacao nao pode dividir a linha com o indicador
            print(visual.apagar_linha(), end='', flush=True)
            visual.pensando_visivel = False
        try:
            escolha = await sessao_prompt.prompt_async(
                ANSI(visual.aprovacao('Aplicar? [s]im / [n]ao / [t]odos (lembrar nesta sessão): '))
            )
        except (KeyboardInterrupt, EOFError):
            return 'nao'
        v = escolha.strip().lower()
        if v in ('s', 'sim'):
            return 'sim'
        if v in ('t', 'todos'):
            return 'sempre'
        return 'nao'

    if cliente is None:
        cliente = ClienteHera(base_url, token, modelo)

    skills_agente = skills
    if skills_agente is None and agente_cfg is not None:
        skills_agente = getattr(agente_cfg, 'skills', None)
    if skills_agente is None and estado is not None:
        skills_agente = getattr(estado, 'skills', None)
    if skills_agente is None:
        try:
            from app.skills import descobrir_skills
            from cli.skills_cmd import pastas_de_skills

            skills_agente = descobrir_skills(pastas_de_skills())
        except Exception:
            skills_agente = None

    if isinstance(skills_agente, (list, tuple)):
        skills_map = {s.nome: s for s in skills_agente}
    elif isinstance(skills_agente, dict) or callable(skills_agente):
        skills_map = skills_agente
    else:
        skills_map = None

    delegar_fn = None
    if agente_cfg is not None and maestro is not None:
        sistema_filho = (
            'Voce e um ajudante de leitura. Leia, busque e analise o projeto para responder a tarefa. '
            'Nao edite nada. Responda com um resumo objetivo (ate 25 linhas) citando arquivos e linhas.'
        )

        raiz = getattr(agente_cfg, 'raiz', getattr(agente_cfg.escrita, 'raiz', None))
        checkpoints = getattr(
            agente_cfg, 'checkpoints', getattr(agente_cfg.escrita, 'checkpoints', None)
        )
        try:
            escrita_filho = Escrita(raiz, checkpoints, modo='read-only')
        except Exception:
            escrita_filho = agente_cfg.escrita

        async def _executar_com_modelo(tarefa: str, mod: str) -> str:
            cli_irmao = ClienteHera(
                getattr(cliente, 'base_url', base_url),
                getattr(cliente, 'token', token),
                mod,
                transporte=getattr(
                    cliente, '_transporte', getattr(cliente, 'transporte', None)
                ),
                timeout=getattr(cliente, '_timeout', getattr(cliente, 'timeout', 120.0)),
            )

            async def aprovar_nao(_p: Any) -> str:
                return 'nao'

            filho = Agente(
                conversar=lambda msgs, tools: conversar_com_ferramentas(
                    cli_irmao, msgs, tools
                ),
                leitura=agente_cfg.leitura,
                escrita=escrita_filho,
                aprovar=aprovar_nao,
                escrever=lambda _t: None,
                max_passos=8,
                skills=skills_map,
                delegar=None,
            )
            return await filho.turno(
                [{'role': 'user', 'content': tarefa}], sistema_filho
            )

        async def delegar(tarefa: str, faixa: str) -> str:
            escolha = await maestro.escolher_faixa(faixa)
            try:
                return await _executar_com_modelo(tarefa, escolha.modelo)
            except ErroDoServidor as erro:
                status_indisp = (0, 429, 502, 503, 504)
                if erro.status in status_indisp:
                    maestro.marcar_indisponivel(escolha.modelo)
                    nova_escolha = await maestro.escolher_faixa(faixa)
                    return await _executar_com_modelo(tarefa, nova_escolha.modelo)
                raise

        delegar_fn = delegar

    agente = None
    if agente_cfg is not None:
        agente = Agente(
            conversar=lambda msgs, tools: conversar_com_ferramentas(
                cliente, msgs, tools
            ),
            leitura=agente_cfg.leitura,
            escrita=agente_cfg.escrita,
            aprovar=aprovar_interativo,
            escrever=escrever,
            skills=skills_map,
            delegar=delegar_fn,
        )
        pasta = getattr(agente_cfg.raiz, 'pasta', agente_cfg.raiz)

    try:
        from importlib.metadata import version as _versao
        versao_hera = _versao('hera-multiagent')
    except Exception:
        versao_hera = ''
    fontes = getattr(getattr(estado, 'instrucoes', None), 'fontes', None) if estado is not None else None
    print(visual.limpar_tela(), end='')
    print(visual.banner(
        versao=versao_hera, modelo=modelo, pasta=pasta_atual, modo=modo_agente or '',
        maestro=bool(maestro is not None and getattr(maestro, 'ativo', True)),
        instrucoes=len(fontes or ()),
        retomando=getattr(estado, 'retomando', '') or '',
        arquivos=tuple(
            str(getattr(f, 'caminho', '')).replace(str(Path.home()), '~') for f in (fontes or ())
        ),
    ))
    sessao = Sessao(modelo, sistema=sistema, agente=agente)
    if mensagens_iniciais:
        sessao.mensagens = list(mensagens_iniciais)

    resumir_fn = resumir or criar_resumidor(cliente, maestro)

    comandos = (
        Comandos(
            cliente,
            estado,
            agente_cfg=agente_cfg,
            skills=skills_map,
            maestro=maestro,
            janela=janela,
            resumir=resumir_fn,
        )
        if estado is not None
        else (
            Comandos(
                cliente,
                EstadoChat(skills=skills_map),
                agente_cfg=agente_cfg,
                skills=skills_map,
                maestro=maestro,
                janela=janela,
                resumir=resumir_fn,
            )
            if agente_cfg is not None
            else None
        )
    )

    if comandos is not None:
        comandos.seletor = seletor

    from app.agente.caminhos import Raiz
    from cli.chat_anexos import expandir_anexos
    raiz_anexos = (getattr(agente_cfg, 'raiz', None) if agente_cfg is not None else None) or Raiz(Path.cwd())

    def anexos_fn(texto_usuario: str):
        return expandir_anexos(texto_usuario, raiz_anexos)

    from cli.chat_imagens import extrair_imagens, imagem_da_area_de_transferencia

    def imagens_fn(texto_usuario: str):
        return extrair_imagens(
            texto_usuario, Path.cwd(), exportar_clipboard=imagem_da_area_de_transferencia
        )

    await rodar(
        cliente,
        ler_linha,
        escrever,
        sessao=sessao,
        ao_turno=ao_turno,
        comandos=comandos,
        agente=agente,
        maestro=maestro,
        janela=janela,
        resumir=resumir_fn,
        anexos=anexos_fn,
        imagens=imagens_fn,
        julgar=julgar,
    )
