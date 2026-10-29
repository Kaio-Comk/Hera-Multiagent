from __future__ import annotations

import asyncio

import pytest

from cli.chat_client import ErroDoServidor
from cli.chat_repl import Sessao, rodar


class ClienteFalso:
    '''Cliente de teste com conversar como async generator.'''

    def __init__(self, roteiro: list) -> None:
        self.roteiro = list(roteiro)
        self.chamadas: list[list[dict]] = []
        self.modelo = 'default'

    async def conversar(self, mensagens):
        self.chamadas.append([dict(m) for m in mensagens])
        acao = self.roteiro.pop(0) if self.roteiro else []
        if isinstance(acao, BaseException):
            raise acao
        for item in acao:
            if isinstance(item, BaseException):
                raise item
            yield item


def _leitor(linhas: list[str]):
    fila = list(linhas)

    async def ler() -> str:
        if not fila:
            raise EOFError
        return fila.pop(0)

    return ler


async def test_dois_turnos_acumulam_historico() -> None:
    cliente = ClienteFalso([['a'], ['b']])
    saida: list[str] = []
    sessao = await rodar(
        cliente, _leitor(['oi', 'tudo bem', '/exit']), saida.append
    )
    assert len(cliente.chamadas) == 2
    assert cliente.chamadas[1] == [
        {'role': 'user', 'content': 'oi'},
        {'role': 'assistant', 'content': 'a'},
        {'role': 'user', 'content': 'tudo bem'},
    ]
    assert sessao.mensagens[-1] == {'role': 'assistant', 'content': 'b'}


async def test_linha_vazia_ignorada() -> None:
    cliente = ClienteFalso([['a']])
    saida: list[str] = []
    await rodar(cliente, _leitor(['', '   ', 'oi', '/exit']), saida.append)
    assert len(cliente.chamadas) == 1


async def test_exit_e_eof_encerram() -> None:
    cliente = ClienteFalso([])
    saida: list[str] = []
    await rodar(cliente, _leitor(['/exit']), saida.append)
    await rodar(cliente, _leitor([]), saida.append)
    assert cliente.chamadas == []


async def test_erro_remove_mensagem_e_continua() -> None:
    cliente = ClienteFalso([ErroDoServidor(401, 'x'), ['ok']])
    saida: list[str] = []
    sessao = await rodar(
        cliente, _leitor(['oi', 'de novo', '/exit']), saida.append
    )
    assert 'Erro (401): x\n' in saida
    assert cliente.chamadas[1] == [{'role': 'user', 'content': 'de novo'}]
    assert sessao.mensagens == [
        {'role': 'user', 'content': 'de novo'},
        {'role': 'assistant', 'content': 'ok'},
    ]


async def test_interrupcao_guarda_parcial() -> None:
    cliente = ClienteFalso([['par', asyncio.CancelledError()]])
    saida: list[str] = []
    sessao = await rodar(cliente, _leitor(['oi', '/exit']), saida.append)
    assert '\n[interrompido]\n' in saida
    assert sessao.mensagens[-1] == {'role': 'assistant', 'content': 'par'}


async def test_interrupcao_sem_texto_remove_mensagem() -> None:
    cliente = ClienteFalso([asyncio.CancelledError()])
    saida: list[str] = []
    sessao = await rodar(cliente, _leitor(['oi', '/exit']), saida.append)
    assert '\n[interrompido]\n' in saida
    assert sessao.mensagens == []


async def test_quebra_de_linha_ao_fim_de_cada_resposta() -> None:
    cliente = ClienteFalso([['a'], ['b']])
    saida: list[str] = []
    await rodar(cliente, _leitor(['oi', 'ola', '/exit']), saida.append)
    assert saida == ['a', '\n', 'b', '\n']


class ClienteLento:
    '''Resposta longa em pedacos lentos; deixa a tarefa cancelavel entre um pedaco e outro.'''

    def __init__(self) -> None:
        self.modelo = 'default'
        self.chamadas = 0

    async def conversar(self, mensagens):
        self.chamadas += 1
        for i in range(50):
            await asyncio.sleep(0.01)
            yield f'p{i} '


async def test_ctrl_c_real_interrompe_varias_vezes_na_mesma_sessao() -> None:
    '''SIGINT de verdade: o asyncio.run sozinho so aguentaria o primeiro; aqui cada um cancela so a resposta.'''
    import signal
    saida: list[str] = []

    def escrever(texto: str) -> None:
        saida.append(texto)
        if texto.startswith('p0'):
            signal.raise_signal(signal.SIGINT)

    cliente = ClienteLento()
    sessao = await rodar(cliente, _leitor(['um', 'dois', 'tres', '/exit']), escrever)
    assert cliente.chamadas == 3
    assert saida.count('\n[interrompido]\n') == 3
    # cada turno guardou o parcial 'p0 ' como resposta do assistente
    assert [m['content'] for m in sessao.mensagens if m['role'] == 'assistant'] == ['p0 ', 'p0 ', 'p0 ']


async def test_cancelamento_externo_da_sessao_nao_e_engolido() -> None:
    '''Se quem chamou cancelar o rodar() (ex.: encerramento do programa), o cancelamento propaga.'''
    cliente = ClienteLento()
    tarefa = asyncio.ensure_future(rodar(cliente, _leitor(['um', '/exit']), lambda _t: None))
    await asyncio.sleep(0.05)          # no meio da resposta
    tarefa.cancel()
    with pytest.raises(asyncio.CancelledError):
        await tarefa


_SCRIPT_ASYNCIO_RUN = r'''
import asyncio, signal, sys, threading
from cli.chat_repl import rodar

class Lento:
    modelo = "default"
    async def conversar(self, mensagens):
        for i in range(400):
            await asyncio.sleep(0.01)
            yield f"p{i} "

def _leitor(linhas):
    fila = list(linhas)
    async def ler():
        if not fila:
            raise EOFError
        return fila.pop(0)
    return ler

saida = []
def escrever(t):
    saida.append(t)
    if t.startswith("p3 "):          # 3o pedaco: o sinal chega DE FORA, com o loop esperando
        threading.Timer(0.0, lambda: signal.raise_signal(signal.SIGINT)).start()

async def principal():
    sessao = await rodar(Lento(), _leitor(["a", "b", "c", "/exit"]), escrever)
    print("INTERROMPIDOS", saida.count("\n[interrompido]\n"), "TURNOS", len(sessao.mensagens))

asyncio.run(principal())
'''


def test_ctrl_c_varias_vezes_sob_asyncio_run_nao_derruba_o_programa() -> None:
    """Reproduz o defeito do asyncio.run: so o 1o Ctrl+C cancelava; o 2o levantava KeyboardInterrupt e matava o programa."""
    import subprocess
    import sys
    from pathlib import Path
    raiz = str(Path(__file__).resolve().parent.parent)
    proc = subprocess.run([sys.executable, '-c', _SCRIPT_ASYNCIO_RUN], cwd=raiz, capture_output=True, text=True, timeout=60,
                          env={**__import__('os').environ, 'PYTHONPATH': raiz})
    assert proc.returncode == 0, proc.stderr[-800:]
    assert 'INTERROMPIDOS 3' in proc.stdout, (proc.stdout, proc.stderr[-800:])


async def test_ao_turno_recebe_pergunta_e_resposta_de_cada_turno() -> None:
    cliente = ClienteFalso([['ola ', 'mundo'], ['de nada']])
    turnos: list[tuple[str, str]] = []
    await rodar(cliente, _leitor(['oi', 'valeu', '/exit']), lambda _t: None, ao_turno=lambda u, a: turnos.append((u, a)))
    assert turnos == [('oi', 'ola mundo'), ('valeu', 'de nada')]


async def test_ao_turno_guarda_parcial_quando_interrompido_e_nada_quando_vazio_ou_erro() -> None:
    cliente = ClienteFalso([['par', asyncio.CancelledError()], asyncio.CancelledError(), ErroDoServidor(503, 'x'), ['ok']])
    turnos: list[tuple[str, str]] = []
    await rodar(cliente, _leitor(['a', 'b', 'c', 'd', '/exit']), lambda _t: None, ao_turno=lambda u, a: turnos.append((u, a)))
    assert turnos == [('a', 'par'), ('d', 'ok')]


async def test_falha_ao_salvar_so_avisa_e_a_conversa_continua() -> None:
    cliente = ClienteFalso([['um'], ['dois']])
    saida: list[str] = []

    def quebra(_u: str, _a: str) -> None:
        raise RuntimeError('disco cheio')

    sessao = await rodar(cliente, _leitor(['a', 'b', '/exit']), saida.append, ao_turno=quebra)
    assert len(sessao.mensagens) == 4
    assert sum('nao foi possivel salvar' in t for t in saida) == 2
