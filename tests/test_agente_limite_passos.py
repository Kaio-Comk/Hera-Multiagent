import json
from pathlib import Path
from typing import Any

import pytest

from app.agente.caminhos import Raiz
from app.agente.checkpoints import Checkpoints
from app.agente.escrita import Escrita
from app.agente.laco import Agente
from app.agente.leitura import Leitura
from cli.chat_ferramentas import ChamadaFerramenta, Evento


def _chamada(nome: str, args: dict[str, Any]) -> Evento:
    return Evento(tipo='chamada', chamada=ChamadaFerramenta(id='c1', nome=nome, argumentos=json.dumps(args)))


class _Conversa:
    def __init__(self, roteiros):
        self.roteiros = roteiros
        self.chamadas: list[tuple[list, list]] = []

    async def __call__(self, mensagens, ferramentas):
        self.chamadas.append((list(mensagens), list(ferramentas)))
        idx = len(self.chamadas) - 1
        for ev in self.roteiros[idx] if idx < len(self.roteiros) else []:
            yield ev


def _agente(tmp_path: Path, roteiros, escritos: list[str], **kw) -> tuple[Agente, _Conversa]:
    raiz = Raiz(tmp_path)

    async def aprovar(_p):
        return 'sim'

    conversa = _Conversa(roteiros)
    ag = Agente(conversa, Leitura(raiz), Escrita(raiz, Checkpoints(tmp_path / '.dados', raiz)),
                aprovar=aprovar, escrever=escritos.append, **kw)
    return ag, conversa


async def test_ao_estourar_o_limite_ele_avisa_e_resume_sem_ferramentas(tmp_path: Path) -> None:
    (tmp_path / 'a.txt').write_text('x\n', encoding='utf-8')
    passo = [_chamada('ler_arquivo', {'caminho': 'a.txt'})]
    resumo = [Evento(tipo='texto', texto='Causa provavel: onClick nao chama handlePlay.')]
    escritos: list[str] = []
    ag, conversa = _agente(tmp_path, [passo, passo, resumo], escritos, max_passos=2)
    out = await ag.turno([{'role': 'user', 'content': 'ache o bug'}])
    mensagens_finais, ferramentas_finais = conversa.chamadas[-1]
    assert ferramentas_finais == []  # a ultima chamada nao tem ferramentas
    assert 'limite de passos' in str(mensagens_finais[-1]['content'])
    assert 'Causa provavel' in out and 'limite de 2 passos atingido' in out
    assert any('limite de 2 passos atingido' in e for e in escritos)  # aparece na tela, nao so no retorno
    assert any('Causa provavel' in e for e in escritos)


async def test_falha_no_resumo_do_limite_nao_derruba_o_turno(tmp_path: Path) -> None:
    (tmp_path / 'a.txt').write_text('x\n', encoding='utf-8')

    class _Quebra(_Conversa):
        async def __call__(self, mensagens, ferramentas):
            if not ferramentas:
                raise RuntimeError('boom')
            async for ev in super().__call__(mensagens, ferramentas):
                yield ev

    raiz = Raiz(tmp_path)

    async def aprovar(_p):
        return 'sim'

    passo = [_chamada('ler_arquivo', {'caminho': 'a.txt'})]
    ag = Agente(_Quebra([passo, passo]), Leitura(raiz), Escrita(raiz, Checkpoints(tmp_path / '.d', raiz)),
                aprovar=aprovar, escrever=lambda _t: None, max_passos=2)
    assert 'limite de 2 passos atingido' in await ag.turno([])


def test_limite_padrao_e_variavel_de_ambiente(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('HERA_MAX_PASSOS', raising=False)
    assert _agente(tmp_path, [], [])[0].max_passos == 40
    monkeypatch.setenv('HERA_MAX_PASSOS', '90')
    assert _agente(tmp_path, [], [])[0].max_passos == 90
    monkeypatch.setenv('HERA_MAX_PASSOS', 'abc')
    assert _agente(tmp_path, [], [])[0].max_passos == 40
    assert _agente(tmp_path, [], [], max_passos=3)[0].max_passos == 3
