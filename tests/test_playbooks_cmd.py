"""Comando `hera playbooks`: lista, aprova, recusa e desativa."""
import argparse

from app.memory.playbooks_aprendidos import PlaybooksAprendidos
from cli import playbooks_cmd


def _args(arq, acao, id_=None):
    return argparse.Namespace(arquivo=str(arq), acao=acao, id=id_)


def _preparar(arq):
    pb = PlaybooksAprendidos(arq)
    passos = [{'engine': 'hera_router', 'model': 'm1', 'titulo': 't'}]
    pid = None
    for _ in range(3):
        pid = pb.registrar('docs', passos, True) or pid
    return pid


def test_lista_aprova_e_desativa(tmp_path):
    arq = tmp_path / 'pb.json'
    pid = _preparar(arq)
    saida = []
    assert playbooks_cmd.executar(_args(arq, 'list'), saida.append) == 0
    assert any(pid in s for s in saida)
    assert playbooks_cmd.executar(_args(arq, 'aprovar', pid), saida.append) == 0
    assert PlaybooksAprendidos(arq).sugerir('docs') is not None
    assert playbooks_cmd.executar(_args(arq, 'desativar', pid), saida.append) == 0
    assert PlaybooksAprendidos(arq).sugerir('docs') is None


def test_id_desconhecido_falha(tmp_path):
    saida = []
    assert playbooks_cmd.executar(_args(tmp_path / 'x.json', 'aprovar', 'nada'), saida.append) == 1
    assert 'nao encontrado' in saida[0]
