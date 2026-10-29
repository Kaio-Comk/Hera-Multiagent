"""Conversas, projetos e anexos do Plano (SQLite local, só metadados e texto; as imagens ficam em arquivos).

Modelo:
  projeto  -> nome + pasta de trabalho opcional (validada pelo servidor contra `allowed_repo_roots`)
  conversa -> título + projeto opcional; contém mensagens em ordem
  mensagem -> papel (user|assistant), tipo (text|plan|run|error), texto, `data` (JSON: plano, execução, avisos) e anexos
  anexo    -> imagem enviada na conversa (caminho no disco, tipo e tamanho já validados por `app/chat/uploads.py`)

Tudo é parametrizado (nenhum SQL montado por texto) e limitado em tamanho, porque o conteúdo vem do navegador.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.chat.fts import buscar_mensagens, garantir_fts

MAX_PROJECTS = 100
MAX_CONVERSATIONS = 1000
MAX_MESSAGES_PER_CONVERSATION = 2000
MAX_NAME_CHARS = 60
MAX_TITLE_CHARS = 120
MAX_FOLDER_CHARS = 500
MAX_TEXT_CHARS = 20_000
MAX_DATA_BYTES = 400_000
ROLES = ("user", "assistant")
KINDS = ("text", "plan", "run", "error")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, folder TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS conversations (
  id TEXT PRIMARY KEY, project_id TEXT REFERENCES projects(id) ON DELETE SET NULL, title TEXT NOT NULL,
  created REAL NOT NULL, updated REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_conv_project ON conversations(project_id, updated DESC);
CREATE TABLE IF NOT EXISTS messages (
  n INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
  conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}', created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, n);
CREATE TABLE IF NOT EXISTS attachments (
  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  message_id TEXT, filename TEXT NOT NULL, mime TEXT NOT NULL, size INTEGER NOT NULL, path TEXT NOT NULL, created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_att_conv ON attachments(conversation_id);
"""

_ALL = object()
_UNSET = object()


class ChatError(Exception):
    """Erro de regra (mensagem pronta para mostrar); `status` vira o código HTTP."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _clean(value: Any, limit: int) -> str:
    text = "".join(ch for ch in str(value or "") if ch == "\n" or ch == "\t" or ord(ch) >= 32 and ord(ch) != 127)
    return text.strip()[:limit]


def _one_line(value: Any, limit: int) -> str:
    return " ".join(_clean(value, limit * 2).split())[:limit]


class ConversationStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with closing(self._connect()) as db:
            db.executescript(_SCHEMA)
            garantir_fts(db)        # indice FTS5 das mensagens (gatilhos mantem em dia; banco antigo e reindexado)

    # ── infra ──────────────────────────────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        return db

    def _rows(self, sql: str, args: Iterable[Any] = ()) -> List[Dict[str, Any]]:
        with self._lock, closing(self._connect()) as db:
            return [dict(r) for r in db.execute(sql, tuple(args)).fetchall()]

    def _row(self, sql: str, args: Iterable[Any] = ()) -> Optional[Dict[str, Any]]:
        rows = self._rows(sql, args)
        return rows[0] if rows else None

    def _exec(self, sql: str, args: Iterable[Any] = ()) -> int:
        with self._lock, closing(self._connect()) as db:
            return db.execute(sql, tuple(args)).rowcount

    def _count(self, table: str, where: str = "1=1", args: Iterable[Any] = ()) -> int:
        if table not in ("projects", "conversations", "messages", "attachments"):
            raise ValueError(f"Tabela inválida: {table}")
        row = self._row(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", args)  # nosec B608
        return int(row["n"]) if row else 0

    # ── projetos ───────────────────────────────────────────────────────────────
    def create_project(self, name: str, folder: str = "") -> Dict[str, Any]:
        name = _one_line(name, MAX_NAME_CHARS)
        if not name:
            raise ChatError("Dê um nome ao projeto.")
        if self._count("projects") >= MAX_PROJECTS:
            raise ChatError(f"Limite de {MAX_PROJECTS} projetos atingido.", 409)
        now = time.time()
        pid = _id("p")
        self._exec("INSERT INTO projects(id, name, folder, created, updated) VALUES (?,?,?,?,?)",
                   (pid, name, _one_line(folder, MAX_FOLDER_CHARS), now, now))
        return self.get_project(pid)  # type: ignore[return-value]

    def get_project(self, project_id: str) -> Optional[Dict[str, Any]]:
        return self._row("SELECT p.*, (SELECT COUNT(*) FROM conversations c WHERE c.project_id = p.id) AS conversations "
                         "FROM projects p WHERE p.id = ?", (project_id,))

    def list_projects(self) -> List[Dict[str, Any]]:
        return self._rows("SELECT p.*, (SELECT COUNT(*) FROM conversations c WHERE c.project_id = p.id) AS conversations "
                          "FROM projects p ORDER BY p.name COLLATE NOCASE")

    def update_project(self, project_id: str, *, name: Any = _UNSET, folder: Any = _UNSET) -> Optional[Dict[str, Any]]:
        cur = self.get_project(project_id)
        if cur is None:
            return None
        new_name = cur["name"] if name is _UNSET else _one_line(name, MAX_NAME_CHARS)
        if not new_name:
            raise ChatError("Dê um nome ao projeto.")
        new_folder = cur["folder"] if folder is _UNSET else _one_line(folder, MAX_FOLDER_CHARS)
        self._exec("UPDATE projects SET name=?, folder=?, updated=? WHERE id=?", (new_name, new_folder, time.time(), project_id))
        return self.get_project(project_id)

    def delete_project(self, project_id: str) -> bool:
        return self._exec("DELETE FROM projects WHERE id = ?", (project_id,)) > 0       # conversas ficam sem projeto

    # ── conversas ──────────────────────────────────────────────────────────────
    def create_conversation(self, title: str = "", project_id: Optional[str] = None) -> Dict[str, Any]:
        if project_id is not None and self.get_project(project_id) is None:
            raise ChatError("Projeto não encontrado.", 404)
        if self._count("conversations") >= MAX_CONVERSATIONS:
            raise ChatError(f"Limite de {MAX_CONVERSATIONS} conversas atingido. Apague as antigas.", 409)
        now = time.time()
        cid = _id("c")
        self._exec("INSERT INTO conversations(id, project_id, title, created, updated) VALUES (?,?,?,?,?)",
                   (cid, project_id, _one_line(title, MAX_TITLE_CHARS) or "Nova conversa", now, now))
        return self.get_conversation(cid)  # type: ignore[return-value]

    def get_conversation(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        return self._row("SELECT * FROM conversations WHERE id = ?", (conversation_id,))

    def list_conversations(self, project_id: Any = _ALL) -> List[Dict[str, Any]]:
        base = ("SELECT c.id, c.project_id, c.title, c.created, c.updated, "
                "(SELECT text FROM messages m WHERE m.conversation_id = c.id AND m.role = 'user' ORDER BY m.n DESC LIMIT 1) AS preview "
                "FROM conversations c")
        if project_id is _ALL:
            return self._rows(base + " ORDER BY c.updated DESC")
        if project_id is None:
            return self._rows(base + " WHERE c.project_id IS NULL ORDER BY c.updated DESC")
        return self._rows(base + " WHERE c.project_id = ? ORDER BY c.updated DESC", (project_id,))

    def update_conversation(self, conversation_id: str, *, title: Any = _UNSET, project_id: Any = _UNSET) -> Optional[Dict[str, Any]]:
        cur = self.get_conversation(conversation_id)
        if cur is None:
            return None
        new_title = cur["title"] if title is _UNSET else (_one_line(title, MAX_TITLE_CHARS) or cur["title"])
        new_project = cur["project_id"] if project_id is _UNSET else project_id
        if new_project is not None and self.get_project(new_project) is None:
            raise ChatError("Projeto não encontrado.", 404)
        self._exec("UPDATE conversations SET title=?, project_id=?, updated=? WHERE id=?",
                   (new_title, new_project, time.time(), conversation_id))
        return self.get_conversation(conversation_id)

    def delete_conversation(self, conversation_id: str) -> Optional[List[str]]:
        """Apaga a conversa (mensagens e anexos em cascata) e devolve os caminhos dos arquivos para o chamador remover."""
        if self.get_conversation(conversation_id) is None:
            return None
        paths = [r["path"] for r in self._rows("SELECT path FROM attachments WHERE conversation_id = ?", (conversation_id,))]
        self._exec("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return paths

    def touch(self, conversation_id: str) -> None:
        self._exec("UPDATE conversations SET updated=? WHERE id=?", (time.time(), conversation_id))

    # ── mensagens ──────────────────────────────────────────────────────────────
    @staticmethod
    def _pack(data: Any) -> str:
        raw = json.dumps(data if data is not None else {}, ensure_ascii=False, default=str)
        if len(raw.encode("utf-8")) > MAX_DATA_BYTES:
            raise ChatError("Conteúdo da mensagem grande demais.", 413)
        return raw

    def add_message(self, conversation_id: str, role: str, kind: str, text: str = "", data: Any = None,
                    attachment_ids: Iterable[str] = ()) -> Dict[str, Any]:
        if role not in ROLES or kind not in KINDS:
            raise ChatError("Mensagem inválida.")
        if self.get_conversation(conversation_id) is None:
            raise ChatError("Conversa não encontrada.", 404)
        if self._count("messages", "conversation_id = ?", (conversation_id,)) >= MAX_MESSAGES_PER_CONVERSATION:
            raise ChatError("Esta conversa ficou longa demais. Comece uma nova.", 409)
        mid = _id("m")
        self._exec("INSERT INTO messages(id, conversation_id, role, kind, text, data, created) VALUES (?,?,?,?,?,?,?)",
                   (mid, conversation_id, role, kind, _clean(text, MAX_TEXT_CHARS), self._pack(data), time.time()))
        ids = list(attachment_ids)
        if ids:
            self.attach_to_message(ids, conversation_id, mid)
        self.touch(conversation_id)
        return self.get_message(mid)  # type: ignore[return-value]

    def _hydrate(self, row: Dict[str, Any]) -> Dict[str, Any]:
        try:
            row["data"] = json.loads(row.get("data") or "{}")
        except ValueError:
            row["data"] = {}
        row["attachments"] = [self.public_attachment(a) for a in
                              self._rows("SELECT * FROM attachments WHERE message_id = ? ORDER BY created", (row["id"],))]
        row.pop("n", None)
        return row

    def search_messages(self, consulta: str, limite: int = 10, conversation_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Busca textual (FTS5) nas mensagens de todas as conversas, ou so numa delas."""
        with self._lock, closing(self._connect()) as db:
            return buscar_mensagens(db, consulta, limite, conversation_id)

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        row = self._row("SELECT * FROM messages WHERE id = ?", (message_id,))
        return self._hydrate(row) if row else None

    def list_messages(self, conversation_id: str) -> List[Dict[str, Any]]:
        return [self._hydrate(r) for r in self._rows("SELECT * FROM messages WHERE conversation_id = ? ORDER BY n", (conversation_id,))]

    def update_message(self, message_id: str, *, text: Any = _UNSET, data: Any = _UNSET, merge: bool = False) -> Optional[Dict[str, Any]]:
        cur = self.get_message(message_id)
        if cur is None:
            return None
        new_text = cur["text"] if text is _UNSET else _clean(text, MAX_TEXT_CHARS)
        new_data = cur["data"] if data is _UNSET else ({**cur["data"], **data} if merge else data)
        self._exec("UPDATE messages SET text=?, data=? WHERE id=?", (new_text, self._pack(new_data), message_id))
        self.touch(cur["conversation_id"])
        return self.get_message(message_id)

    # ── anexos ─────────────────────────────────────────────────────────────────
    @staticmethod
    def public_attachment(row: Dict[str, Any]) -> Dict[str, Any]:
        """O que o navegador pode ver: nunca o caminho no disco."""
        return {"id": row["id"], "filename": row["filename"], "mime": row["mime"], "size": row["size"],
                "url": f"/api/attachments/{row['id']}"}

    def add_attachment(self, conversation_id: str, filename: str, mime: str, size: int, path: str) -> Dict[str, Any]:
        if self.get_conversation(conversation_id) is None:
            raise ChatError("Conversa não encontrada.", 404)
        aid = _id("a")
        self._exec("INSERT INTO attachments(id, conversation_id, message_id, filename, mime, size, path, created) VALUES (?,?,?,?,?,?,?,?)",
                   (aid, conversation_id, None, _one_line(filename, 120) or "imagem", mime, int(size), str(path), time.time()))
        return self.get_attachment(aid)  # type: ignore[return-value]

    def get_attachment(self, attachment_id: str) -> Optional[Dict[str, Any]]:
        return self._row("SELECT * FROM attachments WHERE id = ?", (attachment_id,))

    def pending_attachments(self, conversation_id: str, ids: Iterable[str]) -> List[Dict[str, Any]]:
        """Anexos desta conversa que ainda não foram enviados em nenhuma mensagem (na ordem pedida)."""
        out = []
        for aid in dict.fromkeys(ids):
            row = self._row("SELECT * FROM attachments WHERE id = ? AND conversation_id = ? AND message_id IS NULL", (aid, conversation_id))
            if row is None:
                raise ChatError("Imagem não encontrada ou já enviada. Anexe de novo.", 404)
            out.append(row)
        return out

    def attach_to_message(self, ids: Iterable[str], conversation_id: str, message_id: str) -> int:
        n = 0
        for aid in dict.fromkeys(ids):
            n += self._exec("UPDATE attachments SET message_id = ? WHERE id = ? AND conversation_id = ? AND message_id IS NULL",
                            (message_id, aid, conversation_id))
        return n

    def message_attachments(self, message_id: str) -> List[Dict[str, Any]]:
        return self._rows("SELECT * FROM attachments WHERE message_id = ? ORDER BY created", (message_id,))

    def delete_attachment(self, attachment_id: str) -> Optional[str]:
        row = self.get_attachment(attachment_id)
        if row is None:
            return None
        self._exec("DELETE FROM attachments WHERE id = ?", (attachment_id,))
        return row["path"]

    def purge_orphans(self, older_than_seconds: float = 86400.0) -> List[str]:
        """Anexos enviados e nunca usados em mensagem (o usuário desistiu): devolve os caminhos para apagar os arquivos."""
        cutoff = time.time() - older_than_seconds
        rows = self._rows("SELECT id, path FROM attachments WHERE message_id IS NULL AND created < ?", (cutoff,))
        for r in rows:
            self._exec("DELETE FROM attachments WHERE id = ?", (r["id"],))
        return [r["path"] for r in rows]
