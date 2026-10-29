"""Testes dos playbooks aprendidos com aprovacao."""

import json

from app.memory.playbooks_aprendidos import PlaybooksAprendidos


def _passos(engine='e1', model='m1'):
    return [{'engine': engine, 'model': model, 'titulo': 'Passo 1'}]


def _pb(tmp_path, minimo=3):
    return PlaybooksAprendidos(tmp_path / 'pb.json', minimo=minimo)


def test_duas_execucoes_nao_propoem(tmp_path):
    pb = _pb(tmp_path)
    assert pb.registrar('tarefa', _passos(), True) is None
    assert pb.registrar('tarefa', _passos(), True) is None
    assert pb.propostas() == []


def test_tres_execucoes_propoem_uma_vez(tmp_path):
    pb = _pb(tmp_path)
    pb.registrar('tarefa', _passos(), True)
    pb.registrar('tarefa', _passos(), True)
    pid = pb.registrar('tarefa', _passos(), True)
    assert pid is not None
    assert len(pid) == 12
    assert len(pb.propostas()) == 1
    # quarta execucao nao cria nova proposta
    assert pb.registrar('tarefa', _passos(), True) is None
    assert len(pb.propostas()) == 1


def test_falha_nao_conta(tmp_path):
    pb = _pb(tmp_path)
    pb.registrar('tarefa', _passos(), True)
    pb.registrar('tarefa', _passos(), False)
    pb.registrar('tarefa', _passos(), False)
    assert pb.registrar('tarefa', _passos(), True) is None
    assert pb.propostas() == []


def test_assinatura_diferente_nao_mistura(tmp_path):
    pb = _pb(tmp_path)
    pb.registrar('tarefa', _passos('e1', 'm1'), True)
    pb.registrar('tarefa', _passos('e1', 'm1'), True)
    pb.registrar('tarefa', _passos('e2', 'm2'), True)
    assert pb.registrar('tarefa', _passos('e2', 'm2'), True) is None
    assert pb.registrar('tarefa', _passos('e1', 'm1'), True) is not None


def test_sugerir_so_apos_aprovar(tmp_path):
    pb = _pb(tmp_path)
    for _ in range(3):
        pb.registrar('tarefa', _passos(), True)
    assert pb.sugerir('tarefa') is None
    pid = pb.propostas()[0]['id']
    assert pb.aprovar(pid) is True
    sugestao = pb.sugerir('tarefa')
    assert sugestao is not None
    assert sugestao['id'] == pid


def test_recusar_impede_nova_proposta(tmp_path):
    pb = _pb(tmp_path)
    for _ in range(3):
        pb.registrar('tarefa', _passos(), True)
    pid = pb.propostas()[0]['id']
    assert pb.recusar(pid) is True
    assert pb.propostas() == []
    assert pb.registrar('tarefa', _passos(), True) is None
    assert pb.propostas() == []


def test_desativar_desfaz(tmp_path):
    pb = _pb(tmp_path)
    for _ in range(3):
        pb.registrar('tarefa', _passos(), True)
    pid = pb.propostas()[0]['id']
    pb.aprovar(pid)
    assert pb.sugerir('tarefa') is not None
    assert pb.desativar(pid) is True
    assert pb.sugerir('tarefa') is None
    assert pb.desativar(pid) is False


def test_arquivo_corrompido_nao_quebra(tmp_path):
    caminho = tmp_path / 'pb.json'
    caminho.write_text('{isso nao e json', encoding='utf-8')
    pb = PlaybooksAprendidos(caminho, minimo=3)
    assert pb.propostas() == []
    assert pb.registrar('tarefa', _passos(), True) is None


def test_titulo_com_chave_falsa_sanitizado(tmp_path):
    pb = _pb(tmp_path)
    passos = [{'engine': 'e1', 'model': 'm1', 'titulo': 'chave sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'}]
    for _ in range(3):
        pb.registrar('tarefa', passos, True)
    texto = (tmp_path / 'pb.json').read_text(encoding='utf-8')
    assert 'sk-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' not in texto
    dados = json.loads(texto)
    assert dados['propostas']
