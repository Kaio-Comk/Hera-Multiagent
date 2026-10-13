"""Onde o Hera guarda seu estado e seus logs (corrige dois problemas achados ao analisar uma execucao real).

1. O banco de tarefas era `data/orchestrator.db` RELATIVO a pasta onde o comando foi aberto: com o comando `hera` rodando
   de qualquer pasta, cada pasta criava o seu proprio historico (ex.: C:\\Users\\<voce>\\data\\orchestrator.db). Agora o caminho
   relativo vale a partir da pasta do projeto, e o historico "perdido" numa pasta antiga e importado uma vez.
2. O Hera so mostrava o log na tela e no terminal: fechou o terminal, perdeu. Agora tudo o que a UI mostra tambem vai para
   `data/logs/hera.log` (rotativo), sem conteudo de conversas e com segredos mascarados.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import sqlite3
from pathlib import Path
from typing import Mapping, Optional

DB_ENV = "HERA_DB_PATH"
LOG_NAME = "hera.log"


def resolve_db_path(configured: Optional[str], root: Path, environ: Optional[Mapping[str, str]] = None) -> Path:
    """`HERA_DB_PATH` > `general.db_path` do config. Caminho relativo vale a partir da PASTA DO PROJETO (nao do cwd)."""
    env = os.environ if environ is None else environ
    value = (env.get(DB_ENV) or "").strip() or (configured or "").strip() or "data/orchestrator.db"
    path = Path(value)
    return path if path.is_absolute() else Path(root) / path


def merge_legacy_db(legacy: Path, target: Path) -> int:
    """Importa tarefas e eventos de um banco antigo (criado por engano na pasta de onde o Hera foi aberto).
    Idempotente: so entra o que ainda nao existe no destino. Devolve quantas tarefas entraram."""
    legacy, target = Path(legacy), Path(target)
    if not legacy.is_file() or legacy.resolve() == target.resolve() or not target.is_file():
        return 0
    try:
        with sqlite3.connect(str(target), timeout=10) as db:
            db.execute("ATTACH DATABASE ? AS legacy", (str(legacy),))
            have = {row[1] for row in db.execute("PRAGMA legacy.table_info(tasks)")}
            if not have:
                return 0
            cols = [row[1] for row in db.execute("PRAGMA main.table_info(tasks)") if row[1] in have]
            col_list = ", ".join(cols)
            fresh = [row[0] for row in db.execute(
                "SELECT id FROM legacy.tasks WHERE id NOT IN (SELECT id FROM main.tasks)")]
            if not fresh:
                return 0
            db.execute(f"INSERT OR IGNORE INTO main.tasks ({col_list}) SELECT {col_list} FROM legacy.tasks "  # nosec B608
                       "WHERE id NOT IN (SELECT id FROM main.tasks)")
            for task_id in fresh:
                db.execute("INSERT INTO main.events (task_id, event_type, message, payload, timestamp) "
                           "SELECT task_id, event_type, message, payload, timestamp FROM legacy.events WHERE task_id = ?",
                           (task_id,))
            db.commit()
            return len(fresh)
    except sqlite3.Error:
        return 0


def setup_file_logging(log_dir: Path, sanitize=None) -> Path:
    """Liga o arquivo de log rotativo (2 MiB x 3). Idempotente: chamar de novo nao duplica linhas."""
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / LOG_NAME
    root = logging.getLogger("hera")
    for handler in root.handlers:
        if getattr(handler, "_hera_file", None) == str(path):
            return path
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    handler._hera_file = str(path)                                  # type: ignore[attr-defined]

    class Fmt(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            text = super().format(record)
            return sanitize(text) if sanitize else text

    handler.setFormatter(Fmt("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = True
    return path
