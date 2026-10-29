from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import pytest

from app.agente.caminhos import Raiz
from app.agente.checkpoints import Checkpoints
from app.agente.escrita import Escrita
from app.agente.laco import FERRAMENTAS, Agente
from app.agente.leitura import Leitura
from app.skills import Skill
from cli.chat_ferramentas import ChamadaFerramenta, Evento


class ConversarFalso:
    def __init__(self, roteiros: list[list[Evento]]) -> None:
        self.roteiros = roteiros
        self.chamadas: list[tuple[list[dict[str, Any]], list[dict[str, Any]]]] = []

    async def __call__(
        self, mensagens: Sequence[dict[str, Any]], ferramentas: Sequence[dict[str, Any]]
    ) -> AsyncIterator[Evento]:
        self.chamadas.append((list(mensagens), list(ferramentas)))
        idx = len(self.chamadas) - 1
        roteiro = self.roteiros[idx] if idx < len(self.roteiros) else []
        for ev in roteiro:
            yield ev


def _texto(t: str) -> Evento:
    return Evento(tipo='texto', texto=t)


def _chamada(nome: str, args: dict[str, Any], id_: str = 'c1') -> Evento:
    ch = ChamadaFerramenta(id=id_, nome=nome, argumentos=json.dumps(args))
    return Evento(tipo='chamada', chamada=ch)


def _fim() -> Evento:
    return Evento(tipo='fim', fim='stop')


def _montar(tmp_path, roteiros, aprovar=None, escritos=None, skills=None, delegar=None):
    raiz = Raiz(tmp_path)
    leitura = Leitura(raiz)
    escrita = Escrita(raiz, Checkpoints(tmp_path / '.dados', raiz))
    conversar = ConversarFalso(roteiros)
    if escritos is None:
        escritos = []
    if aprovar is None:
        async def aprovar(p):  # type: ignore
            return 'sim'
    ag = Agente(
        conversar,
        leitura,
        escrita,
        aprovar=aprovar,
        escrever=escritos.append,
        skills=skills,
        delegar=delegar,
    )
    return ag, conversar, escrita, escritos


async def test_sem_ferramentas(tmp_path):
    ag, _, _, escritos = _montar(tmp_path, [[_texto('ola'), _fim()]])
    out = await ag.turno([])
    assert out == 'ola'
    assert escritos == ['ola']


async def test_ler_e_responder(tmp_path):
    (tmp_path / 'a.txt').write_text('conteudo\n', encoding='utf-8')
    roteiros = [
        [_chamada('ler_arquivo', {'caminho': 'a.txt'}), _fim()],
        [_texto('li o arquivo'), _fim()],
    ]
    ag, _, _, _ = _montar(tmp_path, roteiros)
    out = await ag.turno([])
    assert 'li o arquivo' in out


async def test_editar_sim_grava(tmp_path):
    (tmp_path / 'a.txt').write_text('antigo\n', encoding='utf-8')
    roteiros = [
        [_chamada('editar_arquivo', {'caminho': 'a.txt', 'antigo': 'antigo', 'novo': 'novo'}), _fim()],
        [_texto('feito'), _fim()],
    ]
    ag, _, _, _ = _montar(tmp_path, roteiros)
    await ag.turno([])
    assert (tmp_path / 'a.txt').read_text(encoding='utf-8') == 'novo\n'


async def test_editar_nao_nao_grava(tmp_path):
    (tmp_path / 'a.txt').write_text('antigo\n', encoding='utf-8')

    async def aprovar(p):
        return 'nao'

    roteiros = [
        [_chamada('editar_arquivo', {'caminho': 'a.txt', 'antigo': 'antigo', 'novo': 'novo'}), _fim()],
        [_texto('ok'), _fim()],
    ]
    ag, conversar, _, _ = _montar(tmp_path, roteiros, aprovar=aprovar)
    await ag.turno([])
    assert (tmp_path / 'a.txt').read_text(encoding='utf-8') == 'antigo\n'
    segunda = conversar.chamadas[1][0]
    assert any('recusou' in str(m.get('content', '')) for m in segunda)


async def test_editar_sempre_memoriza(tmp_path):
    (tmp_path / 'a.txt').write_text('antigo\n', encoding='utf-8')

    async def aprovar(p):
        return 'sempre'

    roteiros = [
        [_chamada('editar_arquivo', {'caminho': 'a.txt', 'antigo': 'antigo', 'novo': 'novo'}), _fim()],
        [_texto('ok'), _fim()],
    ]
    ag, _, escrita, _ = _montar(tmp_path, roteiros, aprovar=aprovar)
    await ag.turno([])
    assert (tmp_path / 'a.txt').read_text(encoding='utf-8') == 'novo\n'


async def test_workspace_sem_perguntar(tmp_path):
    (tmp_path / 'a.txt').write_text('x\n', encoding='utf-8')
    chamadas_aprovar = []

    async def aprovar(p):
        chamadas_aprovar.append(p)
        return 'nao'

    roteiros = [
        [_chamada('ler_arquivo', {'caminho': 'a.txt'}), _fim()],
        [_texto('ok'), _fim()],
    ]
    ag, _, _, _ = _montar(tmp_path, roteiros, aprovar=aprovar)
    await ag.turno([])
    assert chamadas_aprovar == []


async def test_comando_leitura_sem_perguntar(tmp_path):
    chamadas_aprovar = []

    async def aprovar(p):
        chamadas_aprovar.append(p)
        return 'nao'

    roteiros = [
        [_chamada('executar_comando', {'comando': 'git status'}), _fim()],
        [_texto('ok'), _fim()],
    ]
    ag, _, escrita, _ = _montar(tmp_path, roteiros, aprovar=aprovar)
    executados = []
    escrita.executar = lambda args, cwd, timeout: (executados.append(args) or (0, 'limpo'))
    await ag.turno([])
    assert chamadas_aprovar == []
    assert executados == [('git', 'status')]


async def test_args_invalidos(tmp_path):
    ch = ChamadaFerramenta(id='c1', nome='ler_arquivo', argumentos='{invalido')
    roteiros = [
        [Evento(tipo='chamada', chamada=ch), _fim()],
        [_texto('ok'), _fim()],
    ]
    ag, conversar, _, _ = _montar(tmp_path, roteiros)
    await ag.turno([])
    segunda = conversar.chamadas[1][0]
    assert any('argumentos invalidos' in str(m.get('content', '')) for m in segunda)


async def test_ferramenta_desconhecida(tmp_path):
    roteiros = [
        [_chamada('nao_existe', {}), _fim()],
        [_texto('ok'), _fim()],
    ]
    ag, conversar, _, _ = _montar(tmp_path, roteiros)
    await ag.turno([])
    segunda = conversar.chamadas[1][0]
    assert any('ferramenta desconhecida' in str(m.get('content', '')) for m in segunda)


async def test_limite_por_passo(tmp_path):
    (tmp_path / 'a.txt').write_text('x\n', encoding='utf-8')
    chamadas = [_chamada('ler_arquivo', {'caminho': 'a.txt'}, id_=f'c{i}') for i in range(10)]
    roteiros = [
        chamadas + [_fim()],
        [_texto('ok'), _fim()],
    ]
    ag, conversar, _, _ = _montar(tmp_path, roteiros)
    ag.max_chamadas_por_passo = 3
    await ag.turno([])
    segunda = conversar.chamadas[1][0]
    assert any('limite de chamadas por passo' in str(m.get('content', '')) for m in segunda)


async def test_max_passos(tmp_path):
    (tmp_path / 'a.txt').write_text('x\n', encoding='utf-8')
    roteiros = [[_chamada('ler_arquivo', {'caminho': 'a.txt'}), _fim()] for _ in range(5)]
    ag, _, _, _ = _montar(tmp_path, roteiros)
    ag.max_passos = 2
    out = await ag.turno([])
    assert 'limite de 2 passos' in out


async def test_passagem_final(tmp_path):
    (tmp_path / 'a.txt').write_text('x\n', encoding='utf-8')
    roteiros = [
        [_chamada('ler_arquivo', {'caminho': 'a.txt'}), _fim()],
        [_fim()],
        [_texto('resumo final'), _fim()],
    ]
    ag, _, _, _ = _montar(tmp_path, roteiros)
    out = await ag.turno([])
    assert 'resumo final' in out


async def test_chamada_lida_do_texto(tmp_path):
    (tmp_path / 'a.txt').write_text('x\n', encoding='utf-8')
    texto = '{"name": "ler_arquivo", "arguments": {"caminho": "a.txt"}}'
    roteiros = [
        [_texto(texto), _fim()],
        [_texto('ok'), _fim()],
    ]
    ag, _, _, escritos = _montar(tmp_path, roteiros)
    await ag.turno([])
    assert any('lida do texto' in e for e in escritos)


async def test_historico_intacto(tmp_path):
    hist = [{'role': 'user', 'content': 'oi'}]
    copia = [dict(m) for m in hist]
    roteiros = [[_texto('ola'), _fim()]]
    ag, _, _, _ = _montar(tmp_path, roteiros)
    await ag.turno(hist)
    assert hist == copia


async def test_cancelamento_propaga(tmp_path):
    class Cancelar:
        async def __call__(self, mensagens, ferramentas):
            raise asyncio.CancelledError()
            yield  # pragma: no cover

    raiz = Raiz(tmp_path)
    ag = Agente(
        Cancelar(),
        Leitura(raiz),
        Escrita(raiz, Checkpoints(tmp_path / '.dados', raiz)),
        aprovar=lambda p: None,  # type: ignore
        escrever=lambda t: None,
    )
    with pytest.raises(asyncio.CancelledError):
        await ag.turno([])


async def test_excecao_na_ferramenta(tmp_path):
    class LeituraQuebrada(Leitura):
        def ler_arquivo(self, caminho, inicio=1, fim=None):
            raise RuntimeError('boom')

    raiz = Raiz(tmp_path)
    conversar = ConversarFalso([
        [_chamada('ler_arquivo', {'caminho': 'a.txt'}), _fim()],
        [_texto('ok'), _fim()],
    ])
    ag = Agente(
        conversar,
        LeituraQuebrada(raiz),
        Escrita(raiz, Checkpoints(tmp_path / '.dados', raiz)),
        aprovar=lambda p: None,  # type: ignore
        escrever=lambda t: None,
    )
    await ag.turno([])
    segunda = conversar.chamadas[1][0]
    assert any('erro: RuntimeError: boom' in str(m.get('content', '')) for m in segunda)


@pytest.mark.parametrize('resposta', ['', None, 'talvez', 'SIM!', 'y', 123])
async def test_aprovacao_invalida_nunca_aplica(tmp_path, resposta):
    '''Falha FECHADA: so sim e sempre aplicam; qualquer outra coisa equivale a recusar.'''
    alvo = tmp_path / 'a.txt'
    alvo.write_text('um\n', encoding='utf-8')

    async def aprovar(p):
        return resposta

    roteiros = [
        [_chamada('editar_arquivo', {'caminho': 'a.txt', 'antigo': 'um', 'novo': 'dois'}), _fim()],
        [_texto('ok'), _fim()],
    ]
    ag, _, _, _ = _montar(tmp_path, roteiros, aprovar=aprovar)
    await ag.turno([])
    assert alvo.read_text(encoding='utf-8') == 'um\n'


async def test_aprovar_que_levanta_excecao_nao_aplica(tmp_path):
    alvo = tmp_path / 'a.txt'
    alvo.write_text('um\n', encoding='utf-8')

    async def aprovar(p):
        raise RuntimeError('prompt quebrou')

    roteiros = [
        [_chamada('editar_arquivo', {'caminho': 'a.txt', 'antigo': 'um', 'novo': 'dois'}), _fim()],
        [_texto('ok'), _fim()],
    ]
    ag, _, _, _ = _montar(tmp_path, roteiros, aprovar=aprovar)
    await ag.turno([])
    assert alvo.read_text(encoding='utf-8') == 'um\n'


async def test_chamada_lida_do_texto_so_vale_para_ferramenta_conhecida(tmp_path):
    '''Um exemplo JSON dentro de uma resposta nao pode virar execucao.'''
    roteiros = [
        [_texto('Exemplo: ```json\n{"name": "apagar_tudo", "arguments": {"x": 1}}\n```'), _fim()],
    ]
    ag, conversar, _, escritos = _montar(tmp_path, roteiros)
    await ag.turno([])
    assert len(conversar.chamadas) == 1
    assert not any('lida do texto' in e for e in escritos)


async def test_timeout_do_comando_tem_teto(tmp_path):
    roteiros = [
        [_chamada('executar_comando', {'comando': 'git status', 'timeout': 10**9}), _fim()],
        [_texto('ok'), _fim()],
    ]
    ag, _, escrita, _ = _montar(tmp_path, roteiros)
    vistos = []
    escrita.executar = lambda args, cwd, timeout: (vistos.append(timeout) or (0, ''))
    await ag.turno([])
    assert vistos and vistos[0] <= 600


# ---- testes de usar_skill ------------------------------------------------------------------------------------------

def _skill_mock(tmp_path: Path, nome: str, conteudo: str) -> Skill:
    pasta = tmp_path / nome
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / 'SKILL.md'
    caminho.write_text(f'---\nname: {nome}\n---\n{conteudo}', encoding='utf-8')
    return Skill(
        nome=nome,
        descricao='skill de teste',
        caminho=caminho,
        pasta=pasta,
        origem='teste',
        avisos=(),
    )


async def test_usar_skill_sem_skills_responde_nenhuma_skill_instalada(tmp_path: Path) -> None:
    roteiros = [
        [_chamada('usar_skill', {'nome': 'qualquer'}), _fim()],
        [_texto('ok'), _fim()],
    ]
    ag, conversar, _, _ = _montar(tmp_path, roteiros, skills=None)
    await ag.turno([])
    resultado = conversar.chamadas[1][0][-1]['content']
    assert resultado == 'nenhuma skill instalada'


async def test_usar_skill_com_dict_sucesso(tmp_path: Path) -> None:
    s = _skill_mock(tmp_path, 'analise', 'Instrucoes completas de analise.')
    skills = {'analise': s}
    roteiros = [
        [_chamada('usar_skill', {'nome': 'analise'}), _fim()],
        [_texto('ok'), _fim()],
    ]
    ag, conversar, _, _ = _montar(tmp_path, roteiros, skills=skills)
    await ag.turno([])
    resultado = conversar.chamadas[1][0][-1]['content']
    assert 'Instrucoes completas de analise.' in resultado


async def test_usar_skill_com_funcao_resolucao_sucesso(tmp_path: Path) -> None:
    s = _skill_mock(tmp_path, 'formatador', 'Instrucoes de formatacao.')

    def resolver(nome: str) -> Skill | None:
        return s if nome == 'formatador' else None

    roteiros = [
        [_chamada('usar_skill', {'nome': 'formatador'}), _fim()],
        [_texto('ok'), _fim()],
    ]
    ag, conversar, _, _ = _montar(tmp_path, roteiros, skills=resolver)
    await ag.turno([])
    resultado = conversar.chamadas[1][0][-1]['content']
    assert 'Instrucoes de formatacao.' in resultado


async def test_usar_skill_nome_desconhecido_retorna_erro_com_validas(tmp_path: Path) -> None:
    s1 = _skill_mock(tmp_path, 'lint', 'Instrucoes de lint.')
    s2 = _skill_mock(tmp_path, 'teste', 'Instrucoes de teste.')
    skills = {'lint': s1, 'teste': s2}
    roteiros = [
        [_chamada('usar_skill', {'nome': 'inexistente'}), _fim()],
        [_texto('ok'), _fim()],
    ]
    ag, conversar, _, _ = _montar(tmp_path, roteiros, skills=skills)
    await ag.turno([])
    resultado = conversar.chamadas[1][0][-1]['content']
    assert 'skill desconhecida: inexistente' in resultado
    assert 'lint, teste' in resultado


# ---- testes de delegar_tarefa --------------------------------------------------------------------------------------

async def test_delegar_paralelo_ordem_e_concorrencia(tmp_path: Path) -> None:
    ativo = 0
    pico_ativo = 0
    ordem_inicio: list[str] = []
    ordem_fim: list[str] = []

    async def delegar_mock(tarefa: str, faixa: str) -> str:
        nonlocal ativo, pico_ativo
        ordem_inicio.append(tarefa)
        ativo += 1
        pico_ativo = max(pico_ativo, ativo)
        if tarefa == 't1':
            await asyncio.sleep(0.04)
        elif tarefa == 't2':
            await asyncio.sleep(0.01)
        else:
            await asyncio.sleep(0.02)
        ativo -= 1
        ordem_fim.append(tarefa)
        return f'resumo {tarefa}'

    roteiros = [
        [
            _chamada('delegar_tarefa', {'tarefa': 't1', 'faixa': 'leve'}, id_='c1'),
            _chamada('delegar_tarefa', {'tarefa': 't2', 'faixa': 'medio'}, id_='c2'),
            _chamada('delegar_tarefa', {'tarefa': 't3', 'faixa': 'pesado'}, id_='c3'),
            _fim(),
        ],
        [_texto('fim'), _fim()],
    ]
    ag, conversar, _, escritos = _montar(tmp_path, roteiros, delegar=delegar_mock)
    await ag.turno([])

    assert pico_ativo == 3
    assert ordem_fim == ['t2', 't3', 't1']
    msgs_passo = conversar.chamadas[1][0]
    res_conteudos = [m['content'] for m in msgs_passo if m.get('role') == 'tool']
    assert res_conteudos == ['resumo t1', 'resumo t2', 'resumo t3']
    assert any('-> delegar_tarefa [leve] t1' in e for e in escritos)
    assert any('-> delegar_tarefa [medio] t2' in e for e in escritos)
    assert any('-> delegar_tarefa [pesado] t3' in e for e in escritos)


async def test_delegar_semaforo_max_3_concorrentes(tmp_path: Path) -> None:
    ativo = 0
    pico_ativo = 0

    async def delegar_mock(tarefa: str, faixa: str) -> str:
        nonlocal ativo, pico_ativo
        ativo += 1
        pico_ativo = max(pico_ativo, ativo)
        await asyncio.sleep(0.02)
        ativo -= 1
        return f'ok {tarefa}'

    roteiros = [
        [
            _chamada('delegar_tarefa', {'tarefa': f't{i}'}, id_=f'c{i}')
            for i in range(5)
        ]
        + [_fim()],
        [_texto('fim'), _fim()],
    ]
    ag, conversar, _, _ = _montar(tmp_path, roteiros, delegar=delegar_mock)
    await ag.turno([])

    assert pico_ativo == 3


async def test_delegar_corte_4000(tmp_path: Path) -> None:
    async def delegar_longo(tarefa: str, faixa: str) -> str:
        return 'x' * 6000

    roteiros = [
        [_chamada('delegar_tarefa', {'tarefa': 'analisar'}, id_='c1'), _fim()],
        [_texto('ok'), _fim()],
    ]
    ag, conversar, _, _ = _montar(tmp_path, roteiros, delegar=delegar_longo)
    await ag.turno([])

    res = conversar.chamadas[1][0][-1]['content']
    assert len(res) == 4000
    assert res == 'x' * 4000


async def test_delegar_excecao_isolada(tmp_path: Path) -> None:
    async def delegar_mock(tarefa: str, faixa: str) -> str:
        if tarefa == 't2':
            raise RuntimeError('falha na leitura')
        return f'sucesso {tarefa}'

    roteiros = [
        [
            _chamada('delegar_tarefa', {'tarefa': 't1'}, id_='c1'),
            _chamada('delegar_tarefa', {'tarefa': 't2'}, id_='c2'),
            _chamada('delegar_tarefa', {'tarefa': 't3'}, id_='c3'),
            _fim(),
        ],
        [_texto('ok'), _fim()],
    ]
    ag, conversar, _, _ = _montar(tmp_path, roteiros, delegar=delegar_mock)
    await ag.turno([])

    respostas = [m['content'] for m in conversar.chamadas[1][0] if m.get('role') == 'tool']
    assert respostas[0] == 'sucesso t1'
    assert 'erro: RuntimeError: falha na leitura' in respostas[1]
    assert respostas[2] == 'sucesso t3'


async def test_delegar_cancelled_propaga_e_cancela_irmas(tmp_path: Path) -> None:
    irmas_canceladas: list[str] = []

    async def delegar_mock(tarefa: str, faixa: str) -> str:
        if tarefa == 't1':
            await asyncio.sleep(0.01)
            raise asyncio.CancelledError()
        try:
            await asyncio.sleep(0.1)
            return f'sucesso {tarefa}'
        except asyncio.CancelledError:
            irmas_canceladas.append(tarefa)
            raise

    roteiros = [
        [
            _chamada('delegar_tarefa', {'tarefa': 't1'}, id_='c1'),
            _chamada('delegar_tarefa', {'tarefa': 't2'}, id_='c2'),
            _chamada('delegar_tarefa', {'tarefa': 't3'}, id_='c3'),
            _fim(),
        ],
    ]
    ag, _, _, _ = _montar(tmp_path, roteiros, delegar=delegar_mock)

    with pytest.raises(asyncio.CancelledError):
        await ag.turno([])

    assert 't2' in irmas_canceladas
    assert 't3' in irmas_canceladas


async def test_delegar_sem_delegar_indisponivel(tmp_path: Path) -> None:
    roteiros = [
        [_chamada('delegar_tarefa', {'tarefa': 'analisar modulo'}), _fim()],
        [_texto('ok'), _fim()],
    ]
    ag, conversar, _, escritos = _montar(tmp_path, roteiros, delegar=None)
    await ag.turno([])

    res = conversar.chamadas[1][0][-1]['content']
    assert res == 'sub-agentes indisponiveis'
    assert any('-> delegar_tarefa [leve] analisar modulo' in e for e in escritos)
