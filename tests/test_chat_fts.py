from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.chat.conversations import ConversationStore
from app.chat.fts import garantir_fts


def _store(tmp_path: Path) -> ConversationStore:
    return ConversationStore(tmp_path / "chat.db")


def _buscar(store: ConversationStore, consulta: str, **kwargs):
    return store.search_messages(consulta, **kwargs)


def test_achar_mensagem_pela_palavra(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conv = store.create_conversation(title="Projeto")
    store.add_message(conv["id"], "user", "text", "preciso de um relatorio")
    res = _buscar(store, "relatorio")
    assert len(res) == 1
    assert res[0]["conversation_id"] == conv["id"]


def test_acento_e_maiuscula_nao_importam(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conv = store.create_conversation(title="X")
    store.add_message(conv["id"], "user", "text", "Relatório de Vendas")
    assert len(_buscar(store, "relatorio")) == 1
    assert len(_buscar(store, "RELATORIO")) == 1
    assert len(_buscar(store, "vendas")) == 1


def test_update_reflete_na_busca(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conv = store.create_conversation(title="X")
    msg = store.add_message(conv["id"], "user", "text", "palavra antiga")
    assert len(_buscar(store, "antiga")) == 1
    store.update_message(msg["id"], text="palavra nova")
    assert _buscar(store, "antiga") == []
    assert len(_buscar(store, "nova")) == 1


def test_delete_conversa_cascata(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conv = store.create_conversation(title="X")
    store.add_message(conv["id"], "user", "text", "conteudo removivel")
    assert len(_buscar(store, "removivel")) == 1
    store.delete_conversation(conv["id"])
    assert _buscar(store, "removivel") == []


def test_filtro_por_conversation_id(tmp_path: Path) -> None:
    store = _store(tmp_path)
    c1 = store.create_conversation(title="A")
    c2 = store.create_conversation(title="B")
    store.add_message(c1["id"], "user", "text", "termo comum")
    store.add_message(c2["id"], "user", "text", "termo comum")
    res = _buscar(store, "comum", conversation_id=c1["id"])
    assert len(res) == 1
    assert res[0]["conversation_id"] == c1["id"]


def test_limite(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conv = store.create_conversation(title="X")
    for i in range(5):
        store.add_message(conv["id"], "user", "text", f"repetido {i}")
    assert len(_buscar(store, "repetido", limite=2)) == 2


@pytest.mark.parametrize("consulta", ['"', "a AND", "-x", "*", "NOT", "()"])
def test_consultas_estranhas_sem_excecao(tmp_path: Path, consulta: str) -> None:
    store = _store(tmp_path)
    conv = store.create_conversation(title="X")
    store.add_message(conv["id"], "user", "text", "algum texto")
    res = _buscar(store, consulta)
    assert isinstance(res, list)


def test_consulta_vazia(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conv = store.create_conversation(title="X")
    store.add_message(conv["id"], "user", "text", "algum texto")
    assert _buscar(store, "") == []
    assert _buscar(store, "   ") == []


def test_banco_antigo_backfill(tmp_path: Path) -> None:
    caminho = tmp_path / "chat.db"
    store = ConversationStore(caminho)
    conv = store.create_conversation(title="Antigo")
    store.add_message(conv["id"], "user", "text", "mensagem historica")

    raw = sqlite3.connect(caminho)
    raw.execute("DROP TRIGGER IF EXISTS msg_fts_ai")
    raw.execute("DROP TRIGGER IF EXISTS msg_fts_ad")
    raw.execute("DROP TRIGGER IF EXISTS msg_fts_au")
    raw.execute("DROP TABLE IF EXISTS msg_fts")
    raw.commit()
    raw.close()

    store2 = ConversationStore(caminho)
    res = _buscar(store2, "historica")
    assert len(res) == 1
    assert res[0]["conversation_id"] == conv["id"]


def test_garantir_fts_idempotente(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conv = store.create_conversation(title="X")
    store.add_message(conv["id"], "user", "text", "unico termo")
    with closing(sqlite3.connect(store.path)) as db:
        garantir_fts(db)
        garantir_fts(db)
    res = _buscar(store, "unico")
    assert len(res) == 1


def test_ordenacao_por_relevancia(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conv = store.create_conversation(title="X")
    store.add_message(conv["id"], "user", "text", "python")
    store.add_message(
        conv["id"], "user", "text", "python python python python python"
    )
    res = _buscar(store, "python")
    assert len(res) == 2
    assert res[0]["score"] >= res[1]["score"]


def test_resultado_traz_titulo_e_role(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conv = store.create_conversation(title="Meu Titulo")
    store.add_message(conv["id"], "assistant", "text", "resposta marcante")
    res = _buscar(store, "marcante")
    assert len(res) == 1
    assert res[0]["conversation_title"] == "Meu Titulo"
    assert res[0]["role"] == "assistant"
