import argparse
from pathlib import Path

import pytest

from cli import chat_cmd


async def _rodar(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, agente: bool) -> dict:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    recebido: dict = {}

    async def principal(*a, **kw):
        recebido.update(kw)

    args = argparse.Namespace(
        url='http://127.0.0.1:8000', modelo='default', resume=None, listar=False, agente=agente, modo='guarded'
    )
    await chat_cmd.executar(
        args, raiz=Path('.'), garantir=lambda u, r: True, principal=principal, token_de=lambda r: 't'
    )
    return recebido


async def test_sem_agente_o_modelo_sabe_que_nao_tem_ferramentas_e_nao_recebe_o_catalogo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pasta_skills = tmp_path / '.hera' / 'skills' / 'diagnosing-bugs'
    pasta_skills.mkdir(parents=True)
    (pasta_skills / 'SKILL.md').write_text('---\nname: diagnosing-bugs\ndescription: acha bugs\n---\ncorpo\n', encoding='utf-8')
    sistema = (await _rodar(monkeypatch, tmp_path, agente=False)).get('sistema', '')
    assert 'NAO tem ferramentas' in sistema
    assert 'hera chat --agente' in sistema
    assert 'Catalogo de skills' not in sistema


async def test_com_agente_continua_com_o_catalogo_e_sem_o_aviso(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pasta_skills = tmp_path / '.hera' / 'skills' / 'diagnosing-bugs'
    pasta_skills.mkdir(parents=True)
    (pasta_skills / 'SKILL.md').write_text('---\nname: diagnosing-bugs\ndescription: acha bugs\n---\ncorpo\n', encoding='utf-8')
    sistema = (await _rodar(monkeypatch, tmp_path, agente=True)).get('sistema', '')
    assert 'NAO tem ferramentas' not in sistema
    assert 'ferramentas para ler antes de editar' in sistema
