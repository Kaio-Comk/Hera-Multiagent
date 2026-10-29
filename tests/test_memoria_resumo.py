"""Testes de app.memory.resumo (M1b)."""

from __future__ import annotations

import json

import pytest

from app.memory.resumo import NotaProposta, propor_resumo, salvar_aprovadas
from app.memory.store import MemoryStore


def _msg(role: str, content: str) -> dict:
    return {'role': role, 'content': content}


def test_prompt_nao_contem_segredo():
    segredo = 'sk-' + 'a' * 30
    capturado: dict = {}

    def modelo(prompt: str) -> str:
        capturado['prompt'] = prompt
        return '{"notas":[]}'

    propor_resumo([_msg('user', f'minha chave é {segredo}')], modelo)
    assert segredo not in capturado['prompt']


def test_json_em_bloco_codigo():
    def modelo(prompt: str) -> str:
        return '```json\n{"notas":[{"nome":"pref-1","descricao":"d","tipo":"usuario","corpo":"c"}]}\n```'

    res = propor_resumo([_msg('user', 'oi')], modelo)
    assert len(res) == 1
    assert res[0].nome == 'pref-1'


def test_lixo_retorna_vazio():
    def modelo(prompt: str) -> str:
        return 'isso não é json'

    assert propor_resumo([_msg('user', 'oi')], modelo) == []


def test_nome_invalido_descartado():
    def modelo(prompt: str) -> str:
        return json.dumps(
            {
                'notas': [
                    {'nome': 'Nome Inválido', 'descricao': 'd', 'tipo': 'usuario', 'corpo': 'c'},
                    {'nome': 'ok-1', 'descricao': 'd', 'tipo': 'usuario', 'corpo': 'c'},
                ]
            }
        )

    res = propor_resumo([_msg('user', 'oi')], modelo)
    assert [p.nome for p in res] == ['ok-1']


def test_aprovar_false_nao_grava(tmp_path):
    store = MemoryStore(tmp_path)
    p = NotaProposta(nome='n1', descricao='d', tipo='usuario', corpo='c')
    out = salvar_aprovadas(store, [p], lambda _: False)
    assert out == {'salvas': [], 'recusadas': ['n1'], 'puladas': []}
    assert store.listar() == []


def test_duplicata_e_existente_viram_puladas(tmp_path):
    store = MemoryStore(tmp_path)
    store.save('existente', 'd', 'usuario', 'corpo original')
    p_existente = NotaProposta(nome='existente', descricao='d', tipo='usuario', corpo='outro')
    p_dup = NotaProposta(nome='nova', descricao='d', tipo='usuario', corpo='corpo original')
    out = salvar_aprovadas(store, [p_existente, p_dup], lambda _: True)
    assert out['salvas'] == []
    assert set(out['puladas']) == {'existente', 'nova'}


def test_limite_max_notas():
    def modelo(prompt: str) -> str:
        notas = [
            {'nome': f'n-{i}', 'descricao': 'd', 'tipo': 'usuario', 'corpo': 'c'}
            for i in range(10)
        ]
        return json.dumps({'notas': notas})

    res = propor_resumo([_msg('user', 'oi')], modelo, max_notas=3)
    assert len(res) == 3
