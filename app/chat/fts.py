from __future__ import annotations

import re
import sqlite3

_TERMO_RE = re.compile(r"\w+", re.UNICODE)


def garantir_fts(db: sqlite3.Connection) -> None:
    """Cria tabela FTS5 de conteudo externo e gatilhos de sincronia."""
    existia = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='msg_fts'"
    ).fetchone() is not None

    db.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5("
        "text, content='messages', content_rowid='n', "
        "tokenize='unicode61 remove_diacritics 2')"
    )

    db.execute(
        "CREATE TRIGGER IF NOT EXISTS msg_fts_ai AFTER INSERT ON messages BEGIN "
        "INSERT INTO msg_fts(rowid, text) VALUES (new.n, new.text); "
        "END"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS msg_fts_ad AFTER DELETE ON messages BEGIN "
        "INSERT INTO msg_fts(msg_fts, rowid, text) "
        "VALUES ('delete', old.n, old.text); "
        "END"
    )
    db.execute(
        "CREATE TRIGGER IF NOT EXISTS msg_fts_au AFTER UPDATE OF text ON messages BEGIN "
        "INSERT INTO msg_fts(msg_fts, rowid, text) "
        "VALUES ('delete', old.n, old.text); "
        "INSERT INTO msg_fts(rowid, text) VALUES (new.n, new.text); "
        "END"
    )

    if not existia:
        db.execute("INSERT INTO msg_fts(msg_fts) VALUES('rebuild')")

    db.commit()


def buscar_mensagens(
    db: sqlite3.Connection,
    consulta: str,
    limite: int = 10,
    conversation_id: str | None = None,
) -> list[dict]:
    """Busca mensagens por termos, com prefixo, ordenadas por relevancia."""
    termos = _TERMO_RE.findall(consulta.lower())
    if not termos:
        return []

    fts_query = " OR ".join('"' + t + '"*' for t in termos)

    sql = (
        "SELECT m.conversation_id AS conversation_id, "
        "c.title AS conversation_title, "
        "m.id AS message_id, "
        "m.role AS role, "
        "m.kind AS kind, "
        "snippet(msg_fts, 0, '', '', '...', 20) AS trecho, "
        "-bm25(msg_fts) AS score "
        "FROM msg_fts "
        "JOIN messages m ON m.n = msg_fts.rowid "
        "JOIN conversations c ON c.id = m.conversation_id "
        "WHERE msg_fts MATCH ? "
    )
    params: list[object] = [fts_query]

    if conversation_id is not None:
        sql += "AND m.conversation_id = ? "
        params.append(conversation_id)

    sql += "ORDER BY bm25(msg_fts), m.n DESC LIMIT ?"
    params.append(limite)

    linhas = db.execute(sql, params).fetchall()

    resultado: list[dict] = []
    for linha in linhas:
        resultado.append(
            {
                "conversation_id": linha[0],
                "conversation_title": linha[1],
                "message_id": linha[2],
                "role": linha[3],
                "kind": linha[4],
                "trecho": linha[5],
                "score": float(linha[6]),
            }
        )
    return resultado
