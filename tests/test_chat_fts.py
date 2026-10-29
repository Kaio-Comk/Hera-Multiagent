from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from app.chat.conversations import ConversationStore
from app.chat.fts import (
    desativar_fts,
    fts_desativado,
    garantir_fts,
    reconstruir_fts,
    _buscar_like,
)


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


def test_add_message_continua_funcionando_com_indice_quebrado(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conv = store.create_conversation(title="X")
    with closing(sqlite3.connect(store.path)) as db:
        db.execute("DROP TABLE msg_fts_data")
        db.commit()

    msg = store.add_message(conv["id"], "user", "text", "mensagem gravada sem fts")
    assert msg["text"] == "mensagem gravada sem fts"
    salva = store.get_message(msg["id"])
    assert salva is not None
    assert salva["text"] == "mensagem gravada sem fts"


def test_update_e_delete_com_indice_quebrado(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conv = store.create_conversation(title="X")
    msg = store.add_message(conv["id"], "user", "text", "texto original")

    with closing(sqlite3.connect(store.path)) as db:
        db.execute("DROP TABLE msg_fts")
        db.commit()

    atualizada = store.update_message(msg["id"], text="texto modificado")
    assert atualizada is not None
    assert atualizada["text"] == "texto modificado"
    assert store.get_message(msg["id"])["text"] == "texto modificado"

    caminhos = store.delete_conversation(conv["id"])
    assert caminhos == []
    assert store.get_conversation(conv["id"]) is None
    assert store.get_message(msg["id"]) is None


def test_delete_conversation_com_msg_fts_data_quebrado(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conv = store.create_conversation(title="X")
    store.add_message(conv["id"], "user", "text", "mensagem a deletar")

    with closing(sqlite3.connect(store.path)) as db:
        db.execute("DROP TABLE msg_fts_data")
        db.commit()

    caminhos = store.delete_conversation(conv["id"])
    assert caminhos == []
    assert store.get_conversation(conv["id"]) is None


def test_busca_cai_no_like_e_acha_mensagem(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conv = store.create_conversation(title="Relatorios")
    store.add_message(conv["id"], "user", "text", "documento confidencial financeiro")

    with closing(sqlite3.connect(store.path)) as db:
        db.execute("DROP TABLE msg_fts_idx")
        db.commit()

    res = _buscar(store, "confidencial")
    assert len(res) == 1
    assert res[0]["conversation_id"] == conv["id"]
    assert res[0]["conversation_title"] == "Relatorios"
    assert res[0]["trecho"] == "documento confidencial financeiro"
    assert res[0]["score"] == 1.0


def test_flag_fica_ativa_e_reabrir_store_nao_recria(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conv = store.create_conversation(title="X")
    store.add_message(conv["id"], "user", "text", "primeira")

    with closing(sqlite3.connect(store.path)) as db:
        db.execute("DROP TABLE msg_fts_data")
        db.commit()

    store.add_message(conv["id"], "user", "text", "segunda")

    with closing(sqlite3.connect(store.path)) as db:
        assert fts_desativado(db) is True

    store2 = ConversationStore(store.path)
    with closing(sqlite3.connect(store2.path)) as db:
        assert fts_desativado(db) is True
        gatilhos = db.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'msg_fts_%'"
        ).fetchone()[0]
        assert gatilhos == 0


def test_reconstruir_indice_restaura_fts_e_limpa_flag(tmp_path: Path) -> None:
    store = _store(tmp_path)
    conv = store.create_conversation(title="X")
    store.add_message(conv["id"], "user", "text", "mensagem antiga antes do crash")

    with closing(sqlite3.connect(store.path)) as db:
        db.execute("DROP TABLE msg_fts_data")
        db.commit()

    store.add_message(conv["id"], "user", "text", "mensagem gravada durante falha")
    with closing(sqlite3.connect(store.path)) as db:
        assert fts_desativado(db) is True

    store.reconstruir_indice()

    with closing(sqlite3.connect(store.path)) as db:
        assert fts_desativado(db) is False
        gatilhos = [
            r[0]
            for r in db.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'msg_fts_%'"
            ).fetchall()
        ]
        assert "msg_fts_ai" in gatilhos
        assert "msg_fts_ad" in gatilhos
        assert "msg_fts_au" in gatilhos

    assert len(_buscar(store, "crash")) == 1
    assert len(_buscar(store, "falha")) == 1


def test_like_respeita_limite_conversation_id_e_escapa_caracteres(tmp_path: Path) -> None:
    store = _store(tmp_path)
    c1 = store.create_conversation(title="Conv 1")
    c2 = store.create_conversation(title="Conv 2")

    store.add_message(c1["id"], "user", "text", "taxa de 100% aprovada")
    store.add_message(c1["id"], "user", "text", "taxa de 1000 reprovada")
    store.add_message(c1["id"], "user", "text", "variavel item_1 definida")
    store.add_message(c1["id"], "user", "text", "variavel itemX1 ignorada")
    store.add_message(c2["id"], "user", "text", "outro 100% na conv 2")

    with closing(sqlite3.connect(store.path)) as db:
        desativar_fts(db)

    # 1. Escape de '_': nao casa itemX1
    res_underscore = _buscar(store, "item_1")
    assert len(res_underscore) == 1
    assert "item_1" in res_underscore[0]["trecho"]

    # 2. Escape de '%' e '_': chamada direta a _buscar_like com '%'
    with closing(sqlite3.connect(store.path)) as db:
        res_pct = _buscar_like(db, ["100%"])
        textos = [r["trecho"] for r in res_pct]
        assert any("100%" in t for t in textos)
        assert not any("1000" in t for t in textos)

    # 3. Filtro por conversation_id
    res_c1 = _buscar(store, "100", conversation_id=c1["id"])
    assert all(r["conversation_id"] == c1["id"] for r in res_c1)
    assert len(res_c1) >= 1

    # 4. Limite
    res_lim = _buscar(store, "variavel", limite=1)
    assert len(res_lim) == 1

    # 5. Score por termos distintos presentes
    res_score = _buscar(store, "taxa aprovada", conversation_id=c1["id"])
    scores_por_msg = {r["trecho"]: r["score"] for r in res_score}
    assert scores_por_msg["taxa de 100% aprovada"] == 2.0
    assert scores_por_msg["taxa de 1000 reprovada"] == 1.0
    assert isinstance(scores_por_msg["taxa de 100% aprovada"], float)
