import argparse
from pathlib import Path

import sys

import pytest

from app.agente import escrita as modulo
from app.agente.caminhos import Raiz
from app.agente.checkpoints import Checkpoints
from app.agente.escrita import Escrita
from cli import chat_cmd


def _escrita(tmp_path: Path, modo: str = 'auto') -> Escrita:
    trabalho = tmp_path / 'trabalho'
    trabalho.mkdir(exist_ok=True)
    raiz = Raiz(trabalho, acesso_total=True)
    return Escrita(raiz, Checkpoints(tmp_path / 'dados', raiz), modo)


def _pendencia(e: Escrita, resultado):
    pid = resultado.texto.splitlines()[0]
    return [x for x in e.pendentes() if x.id == pid][0]


def test_auto_grava_sem_perguntar_em_qualquer_pasta(tmp_path: Path) -> None:
    e = _escrita(tmp_path)
    (tmp_path / 'fora.txt').write_text('x\n', encoding='utf-8')
    p = _pendencia(e, e.propor_edicao(str(tmp_path / 'fora.txt'), 'x', 'y'))
    assert e.precisa_aprovacao(p) is False  # o usuario abre o chat na pasta pessoal: o auto vale no PC todo


@pytest.mark.skipif(sys.platform != 'win32', reason='caminhos de unidade do Windows (letra de unidade)')
def test_auto_pergunta_para_gravar_em_pastas_do_sistema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    sistema = tmp_path / 'Windows'
    sistema.mkdir()
    (sistema / 'hosts').write_text('x\n', encoding='utf-8')
    monkeypatch.setattr(modulo, '_PASTAS_DO_SISTEMA', (str(sistema).lower(),))
    e = _escrita(tmp_path)
    assert e.precisa_aprovacao(_pendencia(e, e.propor_edicao(str(sistema / 'hosts'), 'x', 'y'))) is True


def test_workspace_continua_confinado_a_pasta_de_partida(tmp_path: Path) -> None:
    e = _escrita(tmp_path, modo='workspace')
    (tmp_path / 'fora.txt').write_text('x\n', encoding='utf-8')
    assert e.precisa_aprovacao(_pendencia(e, e.propor_edicao(str(tmp_path / 'fora.txt'), 'x', 'y'))) is True


@pytest.mark.parametrize('comando', ['dir /b', 'type a.txt', 'cd /d B:/x', 'echo oi', 'ls', 'findstr x a.txt'])
def test_comandos_internos_do_shell_sao_recusados_sem_perguntar_ao_usuario(tmp_path: Path, comando: str) -> None:
    e = _escrita(tmp_path)
    r = e.propor_comando(comando)
    assert not r.ok and 'NAO usa shell' in r.texto and 'listar_pasta' in r.texto
    assert e.pendentes() == []


def test_o_padrao_do_chat_e_o_auto_mode() -> None:
    parser = argparse.ArgumentParser()
    chat_cmd.registrar(parser.add_subparsers())
    assert parser.parse_args(['chat']).modo == 'auto'
    assert parser.parse_args(['chat', '--modo', 'guarded']).modo == 'guarded'
