from __future__ import annotations

from pathlib import Path

import pytest

from app.chat.conversations import ConversationStore
from cli.chat_sessoes import Sessoes


@pytest.fixture()
def store(tmp_path: Path) -> ConversationStore:
    return ConversationStore(str(tmp_path / 'chat.db'))


def _sess(store: ConversationStore, pasta: Path) -> Sessoes:
    return Sessoes(store, pasta)


def test_nova_cria_projeto_uma_vez_por_pasta(store: ConversationStore, tmp_path: Path) -> None:
    a = tmp_path / 'a'
    b = tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    sa = _sess(store, a)
    sa.nova()
    sa.nova()
    assert len(store.list_projects()) == 1
    sb = _sess(store, b)
    sb.nova()
    assert len(store.list_projects()) == 2


def test_registrar_turno_grava_ordem_e_titulo(store: ConversationStore, tmp_path: Path) -> None:
    p = tmp_path / 'p'
    p.mkdir()
    s = _sess(store, p)
    cid = s.nova()
    s.registrar_turno(cid, 'primeira linha\nsegunda', 'resposta')
    msgs = store.list_messages(cid)
    assert [m['role'] for m in msgs] == ['user', 'assistant']
    assert msgs[0]['text'] == 'primeira linha\nsegunda'
    assert msgs[1]['text'] == 'resposta'
    assert store.get_conversation(cid)['title'] == 'primeira linha'


def test_registrar_turno_corta_titulo(store: ConversationStore, tmp_path: Path) -> None:
    p = tmp_path / 'p'
    p.mkdir()
    s = _sess(store, p)
    cid = s.nova()
    texto = 'x' * 80
    s.registrar_turno(cid, texto, 'ok')
    titulo = store.get_conversation(cid)['title']
    assert titulo == 'x' * 60 + '...'


def test_turno_assistente_vazio(store: ConversationStore, tmp_path: Path) -> None:
    p = tmp_path / 'p'
    p.mkdir()
    s = _sess(store, p)
    cid = s.nova()
    s.registrar_turno(cid, 'oi', '')
    msgs = store.list_messages(cid)
    assert len(msgs) == 1
    assert msgs[0]['role'] == 'user'


def test_carregar_formato_e_filtros(store: ConversationStore, tmp_path: Path) -> None:
    p = tmp_path / 'p'
    p.mkdir()
    s = _sess(store, p)
    cid = s.nova()
    store.add_message(cid, 'user', 'text', 'a')
    store.add_message(cid, 'assistant', 'plan', 'plano')
    store.add_message(cid, 'assistant', 'error', 'erro')
    store.add_message(cid, 'assistant', 'text', 'b')
    store.add_message(cid, 'user', 'text', '')
    out = s.carregar(cid)
    assert out == [{'role': 'user', 'content': 'a'}, {'role': 'assistant', 'content': 'b'}]


def test_resolver_none_prefere_pasta(store: ConversationStore, tmp_path: Path) -> None:
    a = tmp_path / 'a'
    b = tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    sa = _sess(store, a)
    sb = _sess(store, b)
    ca = sa.nova()
    sb.nova()
    r = sa.resolver(None)
    assert r is not None
    assert r['id'] == ca


def test_resolver_none_sem_pasta_cai_na_geral(store: ConversationStore, tmp_path: Path) -> None:
    a = tmp_path / 'a'
    b = tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    sb = _sess(store, b)
    cb = sb.nova()
    sa = _sess(store, a)
    r = sa.resolver(None)
    assert r is not None
    assert r['id'] == cb


def test_resolver_none_sem_conversas(store: ConversationStore, tmp_path: Path) -> None:
    p = tmp_path / 'p'
    p.mkdir()
    assert _sess(store, p).resolver(None) is None


def test_resolver_por_id(store: ConversationStore, tmp_path: Path) -> None:
    p = tmp_path / 'p'
    p.mkdir()
    s = _sess(store, p)
    cid = s.nova()
    r = s.resolver(cid)
    assert r is not None and r['id'] == cid


def test_resolver_por_titulo_parcial_maiusculo(store: ConversationStore, tmp_path: Path) -> None:
    p = tmp_path / 'p'
    p.mkdir()
    s = _sess(store, p)
    cid = s.nova('Relatorio Final')
    r = s.resolver('RELATORIO')
    assert r is not None and r['id'] == cid


def test_resolver_sem_casar(store: ConversationStore, tmp_path: Path) -> None:
    p = tmp_path / 'p'
    p.mkdir()
    s = _sess(store, p)
    s.nova('abc')
    assert s.resolver('zzz') is None


def test_listar_campos_contagem_e_limite(store: ConversationStore, tmp_path: Path) -> None:
    a = tmp_path / 'a'
    b = tmp_path / 'b'
    a.mkdir()
    b.mkdir()
    sa = _sess(store, a)
    sb = _sess(store, b)
    ca = sa.nova('um')
    sa.registrar_turno(ca, 'oi', 'ola')
    sb.nova('dois')
    sb.nova('tres')
    itens = sa.listar(limite=2)
    assert len(itens) == 2
    for it in itens:
        assert set(it) == {'id', 'titulo', 'atualizada', 'pasta', 'mensagens', 'desta_pasta'}
    todos = sa.listar(limite=10)
    alvo = [i for i in todos if i['id'] == ca][0]
    assert alvo['desta_pasta'] is True
    assert alvo['mensagens'] == 2
    assert alvo['pasta'] == str(a.resolve())


def test_carregar_id_inexistente(store: ConversationStore, tmp_path: Path) -> None:
    p = tmp_path / 'p'
    p.mkdir()
    s = _sess(store, p)
    with pytest.raises(LookupError):
        s.carregar('nao-existe')


def test_retomar_e_acumular(store: ConversationStore, tmp_path: Path) -> None:
    p = tmp_path / 'p'
    p.mkdir()
    s = _sess(store, p)
    cid = s.nova()
    s.registrar_turno(cid, 'oi', 'ola')
    r = s.resolver(None)
    assert r is not None and r['id'] == cid
    s.registrar_turno(cid, 'tudo bem?', 'sim')
    msgs = s.carregar(cid)
    assert len(msgs) == 4
    assert msgs[2] == {'role': 'user', 'content': 'tudo bem?'}
    assert msgs[3] == {'role': 'assistant', 'content': 'sim'}


def test_listar_mostra_data_legivel_e_nao_o_numero_cru(tmp_path):
    import re
    from app.chat.conversations import ConversationStore
    from cli.chat_sessoes import Sessoes
    pasta = tmp_path / 'proj'
    pasta.mkdir()
    s = Sessoes(ConversationStore(tmp_path / 'c.sqlite3'), pasta)
    cid = s.nova()
    s.registrar_turno(cid, 'oi', 'ola')
    item = s.listar()[0]
    assert re.fullmatch(r'\d{4}-\d\d-\d\d \d\d:\d\d', item['atualizada']), item['atualizada']
