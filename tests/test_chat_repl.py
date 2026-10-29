from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable

import pytest

from cli.chat_client import ErroDoServidor
from cli.chat_comandos import ComandoSkill
from cli.chat_repl import Sessao, principal, rodar


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


class AgenteFalso:
    '''Agente simulado para testes do REPL.'''

    def __init__(
        self,
        respostas: list[Any],
        escrever: Callable[[str], None] | None = None,
    ) -> None:
        self.respostas = list(respostas)
        self.chamadas: list[tuple[list[dict], Any]] = []
        self.escrever = escrever

    async def turno(self, historico: list[dict], sistema: Any = '') -> str:
        self.chamadas.append(([dict(m) for m in historico], sistema))
        if not self.respostas:
            return ''
        resp = self.respostas.pop(0)
        if isinstance(resp, BaseException):
            raise resp
        if self.escrever is not None:
            self.escrever(resp)
        return resp


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
from pathlib import Path
try:
    import cli.memory_cmd
except ImportError:
    import types
    m = types.ModuleType('cli.memory_cmd')
    m.pasta_padrao = lambda: Path.home() / '.hera' / 'memoria'
    sys.modules['cli.memory_cmd'] = m
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
    '''Reproduz o defeito do asyncio.run: so o 1o Ctrl+C cancelava; o 2o levantava KeyboardInterrupt e matava o programa.'''
    import os
    import subprocess
    import sys
    from pathlib import Path
    raiz = str(Path(__file__).resolve().parent.parent)
    pypath = os.pathsep.join([raiz] + [p for p in sys.path if p])
    proc = subprocess.run([sys.executable, '-c', _SCRIPT_ASYNCIO_RUN], cwd=raiz, capture_output=True, text=True, timeout=60,
                          env={**os.environ, 'PYTHONPATH': pypath})
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


async def test_sistema_enviado_como_primeira_mensagem_e_nao_entra_no_historico() -> None:
    cliente = ClienteFalso([['resposta']])
    saida: list[str] = []
    turnos: list[tuple[str, str]] = []
    sessao = Sessao(sistema='Voce e o Hera.')
    sessao_fim = await rodar(
        cliente,
        _leitor(['ola', '/exit']),
        saida.append,
        sessao=sessao,
        ao_turno=lambda u, a: turnos.append((u, a)),
    )
    assert len(cliente.chamadas) == 1
    assert cliente.chamadas[0] == [
        {'role': 'system', 'content': 'Voce e o Hera.'},
        {'role': 'user', 'content': 'ola'},
    ]
    assert sessao_fim.mensagens == [
        {'role': 'user', 'content': 'ola'},
        {'role': 'assistant', 'content': 'resposta'},
    ]
    assert not any(m.get('role') == 'system' for m in sessao_fim.mensagens)
    assert turnos == [('ola', 'resposta')]


async def test_funciona_sem_sistema() -> None:
    cliente = ClienteFalso([['resposta']])
    saida: list[str] = []
    turnos: list[tuple[str, str]] = []
    sessao = Sessao()
    sessao_fim = await rodar(
        cliente,
        _leitor(['ola', '/exit']),
        saida.append,
        sessao=sessao,
        ao_turno=lambda u, a: turnos.append((u, a)),
    )
    assert len(cliente.chamadas) == 1
    assert cliente.chamadas[0] == [{'role': 'user', 'content': 'ola'}]
    assert not any(m.get('role') == 'system' for m in sessao_fim.mensagens)
    assert turnos == [('ola', 'resposta')]


async def test_principal_aceita_sistema(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli import chat_repl

    captura: list[Sessao] = []

    async def rodar_simulado(cliente, ler_linha, escrever, *, sessao=None, **kwargs):
        if sessao is not None:
            captura.append(sessao)
        return sessao

    monkeypatch.setattr(chat_repl, 'rodar', rodar_simulado)
    monkeypatch.setattr('prompt_toolkit.PromptSession', lambda *a, **k: object())   # sem console real em CI/Git Bash
    await principal(
        'http://127.0.0.1:8000',
        'token-teste',
        'modelo-teste',
        sistema='Sistema de teste',
    )
    assert len(captura) == 1
    assert captura[0].sistema == 'Sistema de teste'
    assert captura[0].modelo == 'modelo-teste'


async def test_rodar_com_agente_usa_texto_devolvido() -> None:
    cliente = ClienteFalso([])
    saida: list[str] = []
    agente = AgenteFalso(['resposta do agente'])
    sessao = await rodar(
        cliente,
        _leitor(['minha pergunta', '/exit']),
        saida.append,
        agente=agente,
    )
    assert len(agente.chamadas) == 1
    assert agente.chamadas[0][0][-1] == {
        'role': 'user',
        'content': 'minha pergunta',
    }
    assert sessao.mensagens[-1] == {
        'role': 'assistant',
        'content': 'resposta do agente',
    }


async def test_rodar_com_agente_ao_turno_recebe_pergunta_e_resposta() -> None:
    cliente = ClienteFalso([])
    saida: list[str] = []
    turnos: list[tuple[str, str]] = []
    agente = AgenteFalso(['resultado do agente'])
    await rodar(
        cliente,
        _leitor(['como vai?', '/exit']),
        saida.append,
        ao_turno=lambda u, a: turnos.append((u, a)),
        agente=agente,
    )
    assert turnos == [('como vai?', 'resultado do agente')]


async def test_rodar_com_agente_nao_duplica_escrita() -> None:
    cliente = ClienteFalso([])
    saida: list[str] = []
    agente = AgenteFalso(['saida direta do agente'], escrever=saida.append)
    await rodar(
        cliente,
        _leitor(['executar acao', '/exit']),
        saida.append,
        agente=agente,
    )
    # o agente escreve o conteudo; rodar escreve apenas o '\n' no final
    assert saida == ['saida direta do agente', '\n']


async def test_rodar_com_agente_interrupcao() -> None:
    cliente = ClienteFalso([])
    saida: list[str] = []
    agente = AgenteFalso([asyncio.CancelledError(), 'resposta depois'])
    sessao = await rodar(
        cliente,
        _leitor(['interromper', 'tentar de novo', '/exit']),
        saida.append,
        agente=agente,
    )
    assert '\n[interrompido]\n' in saida
    assert sessao.mensagens == [
        {'role': 'user', 'content': 'tentar de novo'},
        {'role': 'assistant', 'content': 'resposta depois'},
    ]


async def test_rodar_com_agente_erro_do_servidor() -> None:
    cliente = ClienteFalso([])
    saida: list[str] = []
    agente = AgenteFalso([ErroDoServidor(500, 'falha interna'), 'sucesso'])
    sessao = await rodar(
        cliente,
        _leitor(['provocar erro', 'seguir', '/exit']),
        saida.append,
        agente=agente,
    )
    assert 'Erro (500): falha interna\n' in saida
    assert sessao.mensagens == [
        {'role': 'user', 'content': 'seguir'},
        {'role': 'assistant', 'content': 'sucesso'},
    ]


async def test_principal_com_agente_cfg(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from cli import chat_repl
    from cli.chat_comandos import ConfigAgente

    captura: list[dict] = []

    async def rodar_simulado(
        cliente, ler_linha, escrever, *, sessao=None, agente=None, **kwargs
    ):
        captura.append({'sessao': sessao, 'agente': agente})
        return sessao

    monkeypatch.setattr(chat_repl, 'rodar', rodar_simulado)
    monkeypatch.setattr('prompt_toolkit.PromptSession', lambda *a, **k: object())

    cfg = ConfigAgente(
        raiz=Path('/projeto'),
        checkpoints=object(),
        leitura=object(),
        escrita=object(),
        modo='guarded',
    )
    await principal(
        'http://127.0.0.1:8000',
        'tok',
        'default',
        agente_cfg=cfg,
    )
    saida = capsys.readouterr().out
    assert 'Agente de código ligado (modo guarded)' in saida
    assert len(captura) == 1
    assert captura[0]['agente'] is not None


# ---- memoria por turno e comando de skill --------------------------------------------------------------------------

async def test_memoria_por_turno_como_segunda_mensagem_de_sistema() -> None:
    cliente = ClienteFalso([['resposta']])
    sessao = Sessao(sistema='Prompt do sistema.')
    saida: list[str] = []
    turnos: list[tuple[str, str]] = []

    def memoria_de(texto: str) -> str:
        return 'Contexto relevante da memoria.'

    sessao_fim = await rodar(
        cliente,
        _leitor(['como faco isso?', '/exit']),
        saida.append,
        sessao=sessao,
        ao_turno=lambda u, a: turnos.append((u, a)),
        memoria_de=memoria_de,
    )

    assert len(cliente.chamadas) == 1
    assert cliente.chamadas[0] == [
        {'role': 'system', 'content': 'Prompt do sistema.'},
        {'role': 'system', 'content': 'Contexto relevante da memoria.'},
        {'role': 'user', 'content': 'como faco isso?'},
    ]
    # memoria nao entra no historico
    assert not any('Contexto relevante' in m['content'] for m in sessao_fim.mensagens)
    assert sessao_fim.mensagens == [
        {'role': 'user', 'content': 'como faco isso?'},
        {'role': 'assistant', 'content': 'resposta'},
    ]
    # memoria nao vai ao ao_turno
    assert turnos == [('como faco isso?', 'resposta')]


async def test_memoria_vazia_ou_excecao_nao_adiciona_segundo_sistema() -> None:
    cliente = ClienteFalso([['r1'], ['r2']])
    sessao = Sessao(sistema='Sistema base')
    saida: list[str] = []

    respostas_memoria = ['', RuntimeError('falha no banco de memoria')]

    def memoria_quebrada(_texto: str) -> str:
        item = respostas_memoria.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    await rodar(
        cliente,
        _leitor(['p1', 'p2', '/exit']),
        saida.append,
        sessao=sessao,
        memoria_de=memoria_quebrada,
    )

    assert len(cliente.chamadas) == 2
    # nenhum turno incluiu segunda mensagem de sistema
    assert cliente.chamadas[0][0] == {'role': 'system', 'content': 'Sistema base'}
    assert cliente.chamadas[0][1] == {'role': 'user', 'content': 'p1'}
    assert cliente.chamadas[1][0] == {'role': 'system', 'content': 'Sistema base'}
    assert cliente.chamadas[1][1] == {'role': 'user', 'content': 'p1'}


async def test_comando_skill_vira_mensagem_do_usuario() -> None:
    cliente = ClienteFalso([['resposta sobre skill']])
    saida: list[str] = []
    turnos: list[tuple[str, str]] = []

    class ComandosFalsos:
        async def executar(self, texto: str, sessao: Any, escrever: Any) -> Any:
            if texto.startswith('/minha_skill'):
                return ComandoSkill(texto='Use a skill minha_skill. Faca algo.')
            return None

    sessao = await rodar(
        cliente,
        _leitor(['/minha_skill arg', '/exit']),
        saida.append,
        comandos=ComandosFalsos(),
        ao_turno=lambda u, a: turnos.append((u, a)),
    )

    assert len(cliente.chamadas) == 1
    assert cliente.chamadas[0][-1] == {
        'role': 'user',
        'content': 'Use a skill minha_skill. Faca algo.',
    }
    assert sessao.mensagens[0] == {
        'role': 'user',
        'content': 'Use a skill minha_skill. Faca algo.',
    }
    assert turnos == [('Use a skill minha_skill. Faca algo.', 'resposta sobre skill')]


# ---- maestro e compactacao -----------------------------------------------------------------------------------------

async def test_maestro_anuncio_primeira_vez_e_mudanca_de_faixa() -> None:
    from app.maestro import Escolha

    class FalsoMaestro:
        def __init__(self) -> None:
            self.ativo = True
            self.forcado = None
            self.respostas = [
                Escolha('modelo-leve', 'leve', 'curto', False),
                Escolha('modelo-leve', 'leve', 'curto', False),
                Escolha('modelo-pesado', 'pesado', 'longo', False),
            ]

        async def escolher(self, texto: str) -> Escolha:
            return self.respostas.pop(0)

    cliente = ClienteFalso([['r1'], ['r2'], ['r3']])
    saida: list[str] = []
    maestro = FalsoMaestro()
    await rodar(
        cliente,
        _leitor(['p1', 'p2', 'p3', '/exit']),
        saida.append,
        maestro=maestro,
    )

    texto_saida = ''.join(saida)
    assert '[maestro: faixa leve -> modelo-leve]\n' in texto_saida
    # p2 repete modelo-leve: nao deve haver segundo anuncio de modelo-leve
    assert texto_saida.count('[maestro: faixa leve -> modelo-leve]\n') == 1
    # p3 muda para modelo-pesado: deve anunciar
    assert '[maestro: faixa pesado -> modelo-pesado]\n' in texto_saida


async def test_maestro_forcado_nao_escreve_anuncio() -> None:
    from app.maestro import Escolha

    class FalsoMaestroForcado:
        def __init__(self) -> None:
            self.ativo = True
            self.forcado = 'modelo-fixo'

        async def escolher(self, texto: str) -> Escolha:
            return Escolha(self.forcado, 'medio', 'forcado', True)

    cliente = ClienteFalso([['r1']])
    saida: list[str] = []
    await rodar(
        cliente,
        _leitor(['oi', '/exit']),
        saida.append,
        maestro=FalsoMaestroForcado(),
    )
    texto_saida = ''.join(saida)
    assert '[maestro:' not in texto_saida
    assert cliente.modelo == 'modelo-fixo'


async def test_maestro_retry_em_erro_do_servidor() -> None:
    from app.maestro import Escolha

    class FalsoMaestroComFalha:
        def __init__(self) -> None:
            self.ativo = True
            self.forcado = None
            self.modelos = ['m1', 'm2']
            self.indisponiveis: list[str] = []

        def marcar_indisponivel(self, modelo: str) -> None:
            self.indisponiveis.append(modelo)

        async def escolher(self, texto: str) -> Escolha:
            m = self.modelos.pop(0) if self.modelos else 'm-extra'
            return Escolha(m, 'leve', 'teste', False)

    # Chamada 1 falha com 503; chamada 2 (retry) tem sucesso
    cliente = ClienteFalso([ErroDoServidor(503, 'indisponivel'), ['sucesso']])
    saida: list[str] = []
    maestro = FalsoMaestroComFalha()

    sessao = await rodar(
        cliente,
        _leitor(['minha pergunta', '/exit']),
        saida.append,
        maestro=maestro,
    )

    texto_saida = ''.join(saida)
    assert maestro.indisponiveis == ['m1']
    assert '[maestro: m1 indisponivel, tentando m2]\n' in texto_saida
    assert sessao.mensagens[-1] == {'role': 'assistant', 'content': 'sucesso'}


async def test_maestro_retry_falha_segunda_vez_mostra_erro() -> None:
    from app.maestro import Escolha

    class FalsoMaestro:
        def __init__(self) -> None:
            self.ativo = True
            self.forcado = None
            self.modelos = ['m1', 'm2']

        def marcar_indisponivel(self, modelo: str) -> None:
            pass

        async def escolher(self, texto: str) -> Escolha:
            m = self.modelos.pop(0) if self.modelos else 'm-extra'
            return Escolha(m, 'leve', 'teste', False)

    cliente = ClienteFalso([ErroDoServidor(502, 'bad gateway'), ErroDoServidor(502, 'ainda ruim')])
    saida: list[str] = []
    sessao = await rodar(
        cliente,
        _leitor(['pergunta', '/exit']),
        saida.append,
        maestro=FalsoMaestro(),
    )

    texto_saida = ''.join(saida)
    assert '[maestro: m1 indisponivel, tentando m2]\n' in texto_saida
    assert 'Erro (502): ainda ruim\n' in texto_saida
    assert sessao.mensagens == []


async def test_maestro_retry_com_agente() -> None:
    from app.maestro import Escolha

    class FalsoMaestro:
        def __init__(self) -> None:
            self.ativo = True
            self.forcado = None
            self.modelos = ['m1', 'm2']

        def marcar_indisponivel(self, modelo: str) -> None:
            pass

        async def escolher(self, texto: str) -> Escolha:
            m = self.modelos.pop(0) if self.modelos else 'm-extra'
            return Escolha(m, 'medio', 'teste', False)

    cliente = ClienteFalso([])
    agente = AgenteFalso([ErroDoServidor(504, 'gateway timeout'), 'resposta agente'])
    saida: list[str] = []
    sessao = await rodar(
        cliente,
        _leitor(['acao do agente', '/exit']),
        saida.append,
        agente=agente,
        maestro=FalsoMaestro(),
    )

    texto_saida = ''.join(saida)
    assert '[maestro: m1 indisponivel, tentando m2]\n' in texto_saida
    assert sessao.mensagens[-1] == {'role': 'assistant', 'content': 'resposta agente'}


async def test_compactacao_ao_passar_50_porcento_da_janela() -> None:
    cliente = ClienteFalso([['resposta curta']])
    saida: list[str] = []
    sessao = Sessao()
    # Adiciona mensagens para passar de 50 tokens (janela=100)
    for _ in range(10):
        sessao.mensagens.append({'role': 'user', 'content': 'mensagem relativamente longa para gastar tokens'})
        sessao.mensagens.append({'role': 'assistant', 'content': 'resposta relativamente longa para gastar tokens'})

    async def meu_resumidor(texto: str) -> str:
        return 'resumo compacto'

    await rodar(
        cliente,
        _leitor(['novo turno', '/exit']),
        saida.append,
        sessao=sessao,
        janela=100,
        resumir=meu_resumidor,
    )

    texto_saida = ''.join(saida)
    assert '[contexto compactado:' in texto_saida


async def test_compactacao_fail_open_nao_derruba_o_chat() -> None:
    cliente = ClienteFalso([['resposta ok']])
    saida: list[str] = []
    sessao = Sessao()
    sessao.mensagens.append({'role': 'user', 'content': 'x' * 200})

    async def resumidor_com_erro(texto: str) -> str:
        raise RuntimeError('falha inesperada')

    fim = await rodar(
        cliente,
        _leitor(['pergunta', '/exit']),
        saida.append,
        sessao=sessao,
        janela=100,
        resumir=resumidor_com_erro,
    )
    assert fim.mensagens[-1] == {'role': 'assistant', 'content': 'resposta ok'}


# ---- testes de delegar montado no repl ------------------------------------------------------------------------------

async def test_delegar_montado_so_com_agente_e_maestro(monkeypatch: pytest.MonkeyPatch) -> None:
    from cli import chat_repl
    from cli.chat_comandos import ConfigAgente
    from app.maestro import Maestro

    captura: list[dict] = []

    async def rodar_simulado(cliente, ler_linha, escrever, *, sessao=None, agente=None, **kwargs):
        captura.append({'sessao': sessao, 'agente': agente})
        return sessao

    monkeypatch.setattr(chat_repl, 'rodar', rodar_simulado)
    monkeypatch.setattr('prompt_toolkit.PromptSession', lambda *a, **k: object())

    cfg = ConfigAgente(
        raiz=Path('/projeto'),
        checkpoints=object(),
        leitura=object(),
        escrita=object(),
        modo='guarded',
    )
    m = Maestro(lambda: asyncio.sleep(0, result=['m1']))

    captura.clear()
    await principal('http://127.0.0.1:8000', 'tok', 'default', agente_cfg=cfg, maestro=m)
    assert captura[0]['agente'].delegar is not None

    captura.clear()
    await principal('http://127.0.0.1:8000', 'tok', 'default', agente_cfg=cfg, maestro=None)
    assert captura[0]['agente'].delegar is None

    captura.clear()
    await principal('http://127.0.0.1:8000', 'tok', 'default', agente_cfg=None, maestro=m)
    assert captura[0]['agente'] is None


async def test_filho_read_only_sem_recursao_e_sem_escrever_efeitos(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli import chat_repl
    from cli.chat_comandos import ConfigAgente
    from app.maestro import Escolha
    from app.agente.caminhos import Raiz
    from app.agente.checkpoints import Checkpoints
    from app.agente.leitura import Leitura
    from app.agente.escrita import Escrita

    captura_principal: list[dict] = []

    async def rodar_simulado(cliente, ler_linha, escrever, *, sessao=None, agente=None, **kwargs):
        captura_principal.append({'agente': agente})
        return sessao

    monkeypatch.setattr(chat_repl, 'rodar', rodar_simulado)
    monkeypatch.setattr('prompt_toolkit.PromptSession', lambda *a, **k: object())

    filhos_criados: list[Any] = []
    orig_agente_init = chat_repl.Agente.__init__

    def agente_init_spy(self, *args, **kwargs):
        orig_agente_init(self, *args, **kwargs)
        filhos_criados.append(self)

    monkeypatch.setattr(chat_repl.Agente, '__init__', agente_init_spy)

    raiz = Raiz(Path('.'))
    checkpoints = Checkpoints(Path('.dados'), raiz)
    leitura = Leitura(raiz)
    escrita = Escrita(raiz, checkpoints, modo='guarded')

    cfg = ConfigAgente(
        raiz=raiz,
        checkpoints=checkpoints,
        leitura=leitura,
        escrita=escrita,
        modo='guarded',
    )

    class MaestroFalso:
        ativo = True
        forcado = None

        async def escolher_faixa(self, peso: str) -> Escolha:
            return Escolha('modelo-sub', peso, 'teste')

        def marcar_indisponivel(self, modelo: str) -> None:
            pass

    saida_usuario: list[str] = []
    cliente = ClienteFalso([])

    await principal(
        'http://127.0.0.1:8000',
        'tok',
        'default',
        agente_cfg=cfg,
        maestro=MaestroFalso(),
        cliente=cliente,
    )

    agente_principal = captura_principal[0]['agente']
    assert agente_principal.delegar is not None

    async def turno_filho(self, msgs, sistema):
        filhos_criados[-1].escrever('texto confidencial do subagente')
        assert 'Voce e um ajudante de leitura' in sistema
        return 'resumo do filho'

    monkeypatch.setattr(chat_repl.Agente, 'turno', turno_filho)

    res = await agente_principal.delegar('analisar auth', 'leve')
    assert res == 'resumo do filho'

    filho = filhos_criados[-1]
    assert filho.escrita.modo == 'read-only'
    assert filho.delegar is None
    assert filho.max_passos == 8
    assert await filho.aprovar(object()) == 'nao'
    assert 'texto confidencial do subagente' not in saida_usuario


async def test_delegar_retry_quando_servidor_indisponivel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli import chat_repl
    from cli.chat_comandos import ConfigAgente
    from app.maestro import Escolha

    monkeypatch.setattr(chat_repl, 'rodar', lambda *a, sessao=None, agente=None, **k: sessao)
    monkeypatch.setattr('prompt_toolkit.PromptSession', lambda *a, **k: object())

    cfg = ConfigAgente(
        raiz=Path('.'),
        checkpoints=object(),
        leitura=object(),
        escrita=object(),
        modo='guarded',
    )

    modelos_escolhidos = ['mod1', 'mod2']
    indisponiveis: list[str] = []

    class MaestroRetry:
        ativo = True
        forcado = None

        async def escolher_faixa(self, peso: str) -> Escolha:
            m = modelos_escolhidos.pop(0)
            return Escolha(m, peso, 'teste')

        def marcar_indisponivel(self, modelo: str) -> None:
            indisponiveis.append(modelo)

    tentativas = 0

    async def turno_com_falha(self, msgs, sistema):
        nonlocal tentativas
        tentativas += 1
        if tentativas == 1:
            raise ErroDoServidor(503, 'indisponivel')
        return 'sucesso no retry'

    monkeypatch.setattr(chat_repl.Agente, 'turno', turno_com_falha)

    captura_agente: list[Any] = []

    async def rodar_spy(cli, ler, esc, *, sessao=None, agente=None, **k):
        captura_agente.append(agente)
        return sessao

    monkeypatch.setattr(chat_repl, 'rodar', rodar_spy)

    await principal(
        'http://127.0.0.1:8000',
        'tok',
        'default',
        agente_cfg=cfg,
        maestro=MaestroRetry(),
    )

    agente = captura_agente[0]
    res = await agente.delegar('tarefa', 'leve')
    assert res == 'sucesso no retry'
    assert indisponiveis == ['mod1']
    assert tentativas == 2


async def test_anexos_expandem_a_pergunta_para_o_modelo_mas_o_historico_gravado_guarda_o_original() -> None:
    cliente = ClienteFalso([['ok']])
    turnos: list[tuple[str, str]] = []
    saida: list[str] = []

    def anexos(texto: str):
        class A:
            caminho = 'a.py'
            linhas = 'todas'
        return texto + '\n\n[Arquivos anexados]\nconteudo-de-a', ['nao encontrado: x.py'], [A()]

    sessao = await rodar(cliente, _leitor(['explique @a.py', '/exit']), saida.append,
                         ao_turno=lambda u, a: turnos.append((u, a)), anexos=anexos)
    assert 'conteudo-de-a' in cliente.chamadas[0][-1]['content']           # o modelo recebe o conteudo anexado
    assert turnos == [('explique @a.py', 'ok')]                           # o que se grava e o que o usuario digitou
    assert any('nao encontrado: x.py' in t for t in saida)                 # avisos aparecem
    assert any('anexado' in t and 'a.py' in t for t in saida)             # e um resumo do que foi anexado
    assert sessao.mensagens[0]['content'].startswith('explique @a.py')


async def test_falha_ao_expandir_anexos_envia_a_pergunta_original() -> None:
    cliente = ClienteFalso([['ok']])

    def anexos(_t: str):
        raise RuntimeError('quebrou')

    await rodar(cliente, _leitor(['oi @a.py', '/exit']), lambda _t: None, anexos=anexos)
    assert cliente.chamadas[0][-1]['content'] == 'oi @a.py'
