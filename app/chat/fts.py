from __future__ import annotations

import re
import sqlite3

_TERMO_RE = re.compile(r"\w+", re.UNICODE)


def desativar_fts(db: sqlite3.Connection) -> None:
    """Apaga os 3 gatilhos, tenta dropar msg_fts e grava flag de desativado."""
    db.execute("DROP TRIGGER IF EXISTS msg_fts_ai")
    db.execute("DROP TRIGGER IF EXISTS msg_fts_ad")
    db.execute("DROP TRIGGER IF EXISTS msg_fts_au")
    try:
        db.execute("DROP TABLE IF EXISTS msg_fts")
    except Exception:
        pass
    db.execute(
        "CREATE TABLE IF NOT EXISTS fts_estado (chave TEXT PRIMARY KEY, valor TEXT)"
    )
    db.execute(
        "INSERT OR REPLACE INTO fts_estado (chave, valor) VALUES ('desativado', '1')"
    )
    try:
        db.commit()
    except Exception:
        pass


def fts_desativado(db: sqlite3.Connection) -> bool:
    """Indica se o indice FTS5 esta desativado (tabela ausente => False)."""
    try:
        row = db.execute(
            "SELECT valor FROM fts_estado WHERE chave = 'desativado'"
        ).fetchone()
        return bool(row and str(row[0]) == "1")
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return False


def reconstruir_fts(db: sqlite3.Connection) -> None:
    """Apaga a flag, recria tabela e gatilhos via garantir_fts e reindexa tudo com rebuild."""
    try:
        db.execute("DELETE FROM fts_estado WHERE chave = 'desativado'")
        db.commit()
    except Exception:
        pass

    db.execute("DROP TRIGGER IF EXISTS msg_fts_ai")
    db.execute("DROP TRIGGER IF EXISTS msg_fts_ad")
    db.execute("DROP TRIGGER IF EXISTS msg_fts_au")

    try:
        db.execute(
            "CREATE TABLE IF NOT EXISTS msg_fts_data(id INTEGER PRIMARY KEY, block BLOB)"
        )
        db.execute(
            "INSERT OR IGNORE INTO msg_fts_data VALUES (1, X''), (10, X'00000000000000')"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS msg_fts_config(k PRIMARY KEY, v) WITHOUT ROWID"
        )
        db.execute("INSERT OR IGNORE INTO msg_fts_config VALUES ('version', 4)")
    except Exception:
        pass

    try:
        db.execute("DROP TABLE IF EXISTS msg_fts")
    except Exception:
        pass

    for tbl in (
        "msg_fts",
        "msg_fts_data",
        "msg_fts_idx",
        "msg_fts_content",
        "msg_fts_docsize",
        "msg_fts_config",
    ):
        try:
            db.execute(f"DROP TABLE IF EXISTS {tbl}")
        except Exception:
            pass

    garantir_fts(db)
    db.execute("INSERT INTO msg_fts(msg_fts) VALUES('rebuild')")
    try:
        db.commit()
    except Exception:
        pass


def garantir_fts(db: sqlite3.Connection) -> None:
    """Cria tabela FTS5 de conteudo externo e gatilhos de sincronia."""
    if fts_desativado(db):
        return

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


def _escapar_like(termo: str) -> str:
    """Escapa caracteres especiais de LIKE (\\, %, _)."""
    return termo.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _buscar_like(
    db: sqlite3.Connection,
    termos: list[str],
    limite: int = 10,
    conversation_id: str | None = None,
) -> list[dict]:
    """Fallback por LIKE quando o FTS5 estiver indisponivel."""
    if not termos:
        return []

    termos_unicos = list(dict.fromkeys(termos))
    clausulas = []
    params: list[object] = []
    for t in termos_unicos:
        clausulas.append("m.text LIKE ? ESCAPE '\\'")
        params.append(f"%{_escapar_like(t)}%")

    sql = (
        "SELECT m.conversation_id AS conversation_id, "
        "c.title AS conversation_title, "
        "m.id AS message_id, "
        "m.role AS role, "
        "m.kind AS kind, "
        "m.text AS text "
        "FROM messages m "
        "JOIN conversations c ON c.id = m.conversation_id "
        "WHERE (" + " OR ".join(clausulas) + ") "
    )
    if conversation_id is not None:
        sql += "AND m.conversation_id = ? "
        params.append(conversation_id)

    sql += "ORDER BY m.n DESC LIMIT ?"
    params.append(limite)

    linhas = db.execute(sql, params).fetchall()

    resultado: list[dict] = []
    for linha in linhas:
        texto = linha[5] or ""
        texto_lower = texto.lower()
        score = float(sum(1 for t in termos_unicos if t.lower() in texto_lower))
        resultado.append(
            {
                "conversation_id": linha[0],
                "conversation_title": linha[1],
                "message_id": linha[2],
                "role": linha[3],
                "kind": linha[4],
                "trecho": texto[:160],
                "score": score,
            }
        )
    return resultado


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

    if fts_desativado(db):
        return _buscar_like(db, termos, limite, conversation_id)

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

    try:
        linhas = db.execute(sql, params).fetchall()
    except (sqlite3.DatabaseError, sqlite3.OperationalError):
        try:
            desativar_fts(db)
        except Exception:
            pass
        return _buscar_like(db, termos, limite, conversation_id)

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
