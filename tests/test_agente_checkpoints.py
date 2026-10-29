from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.agente.checkpoints import APAGADO, Checkpoints


def _cp(tmp_path: Path) -> Checkpoints:
    return Checkpoints(tmp_path / 'dados', tmp_path / 'raiz')


def test_salvar_confirmar_reverter_restaura(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    raiz = tmp_path / 'raiz'
    raiz.mkdir()
    alvo = raiz / 'a.txt'
    alvo.write_text('antes', encoding='utf-8')
    cid = cp.salvar(alvo)
    alvo.write_text('depois', encoding='utf-8')
    cp.confirmar(cid, alvo)
    rel = cp.reverter(cid)
    assert rel.restaurados == (cid,)
    assert alvo.read_text(encoding='utf-8') == 'antes'


def test_arquivo_novo_apagado(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    raiz = tmp_path / 'raiz'
    raiz.mkdir()
    alvo = raiz / 'novo.txt'
    cid = cp.salvar(alvo)
    alvo.write_text('criado', encoding='utf-8')
    cp.confirmar(cid, alvo)
    cp.reverter(cid)
    assert not alvo.exists()


def test_editado_depois_pulado(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    raiz = tmp_path / 'raiz'
    raiz.mkdir()
    alvo = raiz / 'a.txt'
    alvo.write_text('antes', encoding='utf-8')
    cid = cp.salvar(alvo)
    alvo.write_text('agente', encoding='utf-8')
    cp.confirmar(cid, alvo)
    alvo.write_text('usuario', encoding='utf-8')
    rel = cp.reverter(cid)
    assert rel.pulados == (cid,)
    assert alvo.read_text(encoding='utf-8') == 'usuario'
    assert any('editado depois' in a for a in rel.avisos)


def test_forcar_restaura(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    raiz = tmp_path / 'raiz'
    raiz.mkdir()
    alvo = raiz / 'a.txt'
    alvo.write_text('antes', encoding='utf-8')
    cid = cp.salvar(alvo)
    alvo.write_text('agente', encoding='utf-8')
    cp.confirmar(cid, alvo)
    alvo.write_text('usuario', encoding='utf-8')
    rel = cp.reverter(cid, forcar=True)
    assert rel.restaurados == (cid,)
    assert alvo.read_text(encoding='utf-8') == 'antes'


def test_reverter_sem_cid_pega_ultimo(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    raiz = tmp_path / 'raiz'
    raiz.mkdir()
    a = raiz / 'a.txt'
    a.write_text('v0', encoding='utf-8')
    c1 = cp.salvar(a)
    a.write_text('v1', encoding='utf-8')
    cp.confirmar(c1, a)
    c2 = cp.salvar(a)
    a.write_text('v2', encoding='utf-8')
    cp.confirmar(c2, a)
    rel = cp.reverter()                       # sem id: desfaz so o ULTIMO passo
    assert rel.restaurados == (c2,)
    assert a.read_text(encoding='utf-8') == 'v1'
    rel2 = cp.reverter()                      # de novo: agora o anterior
    assert rel2.restaurados == (c1,)
    assert a.read_text(encoding='utf-8') == 'v0'


def test_duas_edicoes_revertem_em_ordem(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    raiz = tmp_path / 'raiz'
    raiz.mkdir()
    a = raiz / 'a.txt'
    a.write_text('v0', encoding='utf-8')
    c1 = cp.salvar(a)
    a.write_text('v1', encoding='utf-8')
    cp.confirmar(c1, a)
    c2 = cp.salvar(a)
    a.write_text('v2', encoding='utf-8')
    cp.confirmar(c2, a)
    rel = cp.reverter(c1)
    assert rel.restaurados == (c2, c1)
    assert a.read_text(encoding='utf-8') == 'v0'


def test_arquivos_diferentes_nao_interferem(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    raiz = tmp_path / 'raiz'
    raiz.mkdir()
    a = raiz / 'a.txt'
    b = raiz / 'b.txt'
    a.write_text('a0', encoding='utf-8')
    b.write_text('b0', encoding='utf-8')
    ca = cp.salvar(a)
    a.write_text('a1', encoding='utf-8')
    cp.confirmar(ca, a)
    cb = cp.salvar(b)
    b.write_text('b1', encoding='utf-8')
    cp.confirmar(cb, b)
    rel = cp.reverter(ca)
    assert rel.restaurados == (ca,)
    assert a.read_text(encoding='utf-8') == 'a0'
    assert b.read_text(encoding='utf-8') == 'b1'


def test_listar_ordenado_e_limite(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    raiz = tmp_path / 'raiz'
    raiz.mkdir()
    ids = []
    for i in range(3):
        p = raiz / f'f{i}.txt'
        p.write_text('x', encoding='utf-8')
        ids.append(cp.salvar(p))
    lst = cp.listar(limite=2)
    assert len(lst) == 2
    assert lst[0].id == ids[-1]
    assert lst[1].id == ids[-2]


def test_revertido_nao_reverte_de_novo(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    raiz = tmp_path / 'raiz'
    raiz.mkdir()
    a = raiz / 'a.txt'
    a.write_text('antes', encoding='utf-8')
    cid = cp.salvar(a)
    a.write_text('depois', encoding='utf-8')
    cp.confirmar(cid, a)
    cp.reverter(cid)
    rel = cp.reverter(cid)
    assert rel.restaurados == ()
    assert any('ja revertido' in x for x in rel.avisos)


def test_alvo_fora_da_raiz(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    raiz = tmp_path / 'raiz'
    raiz.mkdir()
    fora = tmp_path / 'fora.txt'
    fora.write_text('x', encoding='utf-8')
    with pytest.raises(ValueError):
        cp.salvar(fora)


def test_ledger_sobrevive_reabrir(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    raiz = tmp_path / 'raiz'
    raiz.mkdir()
    a = raiz / 'a.txt'
    a.write_text('antes', encoding='utf-8')
    cid = cp.salvar(a)
    a.write_text('depois', encoding='utf-8')
    cp.confirmar(cid, a)
    cp2 = Checkpoints(tmp_path / 'dados', raiz)
    rel = cp2.reverter(cid)
    assert rel.restaurados == (cid,)
    assert a.read_text(encoding='utf-8') == 'antes'


def test_id_inexistente(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    rel = cp.reverter('naoexiste')
    assert rel.restaurados == ()
    assert any('inexistente' in x for x in rel.avisos)


def test_arquivo_apagado_pelo_agente_restaurado(tmp_path: Path) -> None:
    cp = _cp(tmp_path)
    raiz = tmp_path / 'raiz'
    raiz.mkdir()
    a = raiz / 'a.txt'
    a.write_text('antes', encoding='utf-8')
    cid = cp.salvar(a)
    a.unlink()
    cp.confirmar(cid, a)
    entradas = cp.listar()
    assert entradas[0].hash_depois == APAGADO
    rel = cp.reverter(cid)
    assert rel.restaurados == (cid,)
    assert a.read_text(encoding='utf-8') == 'antes'


def test_edicoes_no_mesmo_instante_revertem_na_ordem_certa(tmp_path: Path, monkeypatch) -> None:
    """Regressao: o relogio do Windows empata; a ordem tem de vir da posicao no livro-razao, nao do ts."""
    import time
    monkeypatch.setattr(time, 'time', lambda: 1000.0)
    cp = _cp(tmp_path)
    raiz = tmp_path / 'raiz'
    raiz.mkdir()
    a = raiz / 'a.txt'
    a.write_text('v0', encoding='utf-8')
    c1 = cp.salvar(a)
    a.write_text('v1', encoding='utf-8')
    cp.confirmar(c1, a)
    c2 = cp.salvar(a)
    a.write_text('v2', encoding='utf-8')
    cp.confirmar(c2, a)
    assert [e.id for e in cp.listar()] == [c2, c1]
    rel = cp.reverter(c1)
    assert rel.restaurados == (c2, c1) and a.read_text(encoding='utf-8') == 'v0'
