from __future__ import annotations

import asyncio
from typing import Awaitable, Callable, Optional

from cli.chat_client import ClienteHera, ErroDoServidor
from cli.interrupcao import InterruptorSigint


class Sessao:
    '''Estado de uma conversa com o Hera.'''

    def __init__(self, modelo: str = 'default') -> None:
        self.mensagens: list[dict] = []
        self.modelo = modelo


async def rodar(
    cliente: ClienteHera,
    ler_linha: Callable[[], Awaitable[str]],
    escrever: Callable[[str], None],
    *,
    sessao: Optional[Sessao] = None,
    interruptor: Optional[InterruptorSigint] = None,
    ao_turno: Optional[Callable[[str, str], None]] = None,
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
        sessao = Sessao(modelo=getattr(cliente, 'modelo', 'default'))

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
        if texto in ('/exit', '/quit', '/sair'):
            break

        sessao.mensagens.append({'role': 'user', 'content': texto})
        resposta = ''
        interrompido = False

        async def consumir() -> None:
            nonlocal resposta
            async for pedaco in cliente.conversar(sessao.mensagens):
                resposta += pedaco
                escrever(pedaco)

        # A resposta roda numa tarefa propria: o Ctrl+C (interruptor) cancela so ela, quantas vezes for preciso.
        # O handler padrao do asyncio.run so cancela a tarefa principal na 1a vez e derruba o programa na 2a.
        tarefa = asyncio.ensure_future(consumir())
        try:
            with interruptor.vigiar(tarefa):
                await tarefa
        except ErroDoServidor as erro:
            escrever(f'Erro ({erro.status}): {erro.mensagem}\n')
            sessao.mensagens.pop()
            continue
        except asyncio.CancelledError:
            atual = asyncio.current_task()
            if atual is not None and atual.cancelling():
                tarefa.cancel()
                raise                  # cancelamento de quem chamou (encerrar o programa): nao engolir
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
            continue

        escrever('\n')
        sessao.mensagens.append({'role': 'assistant', 'content': resposta})
        salvar(texto, resposta)

    return sessao


async def principal(
    base_url: str,
    token: str,
    modelo: str,
    *,
    mensagens_iniciais: Optional[list[dict]] = None,
    ao_turno: Optional[Callable[[str, str], None]] = None,
) -> None:
    '''Ponto de entrada interativo usando prompt_toolkit.'''
    from prompt_toolkit import PromptSession

    sessao_prompt = PromptSession()

    async def ler_linha() -> str:
        return await sessao_prompt.prompt_async('> ')

    def escrever(texto: str) -> None:
        print(texto, end='', flush=True)

    sessao = Sessao(modelo)
    if mensagens_iniciais:
        sessao.mensagens = list(mensagens_iniciais)

    cliente = ClienteHera(base_url, token, modelo)
    await rodar(
        cliente,
        ler_linha,
        escrever,
        sessao=sessao,
        ao_turno=ao_turno,
    )
