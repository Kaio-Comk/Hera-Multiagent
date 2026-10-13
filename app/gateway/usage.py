"""Contador de uso do Roteador (SQLite local): quantas chamadas, tokens e erros por provedor, modelo e dia.

Guarda SÓ metadados (provedor, modelo, resultado, tokens, latência). Nunca o texto do prompt nem da resposta.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

RETENTION_DAYS = 90


class UsageLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS usage (
                ts REAL NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, ok INTEGER NOT NULL,
                status INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, latency_ms INTEGER, stream INTEGER)""")
            db.execute("CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts)")
        self._since_prune = 0

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    def record(self, provider: str, model: str, ok: bool, status: Optional[int] = None,
               prompt_tokens: Optional[int] = None, completion_tokens: Optional[int] = None,
               latency_ms: Optional[int] = None, stream: bool = False) -> None:
        """Nunca levanta: um erro no contador não pode derrubar uma requisição."""
        try:
            with self._lock, self._connect() as db:
                db.execute("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?)",
                           (time.time(), str(provider)[:60], str(model)[:200], 1 if ok else 0, status,
                            prompt_tokens, completion_tokens, latency_ms, 1 if stream else 0))
                self._since_prune += 1
                if self._since_prune >= 500:
                    self._since_prune = 0
                    db.execute("DELETE FROM usage WHERE ts < ?", (time.time() - RETENTION_DAYS * 86400,))
        except sqlite3.Error:
            pass

    def summary(self, days: int = 7) -> Dict[str, Any]:
        days = max(1, min(int(days), RETENTION_DAYS))
        since = time.time() - days * 86400
        with self._lock, self._connect() as db:
            db.row_factory = sqlite3.Row
            # Consultas LITERAIS (sem montar SQL por texto): nada aqui depende de entrada do usuario.
            totals = dict(db.execute(
                "SELECT COUNT(*) AS requests, SUM(ok) AS ok, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
                "COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
                "CAST(AVG(latency_ms) AS INTEGER) AS avg_latency_ms FROM usage WHERE ts >= ?", (since,)).fetchone())
            by_provider = [dict(r) for r in db.execute(
                "SELECT provider, model, COUNT(*) AS requests, SUM(ok) AS ok, "
                "COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
                "CAST(AVG(latency_ms) AS INTEGER) AS avg_latency_ms FROM usage WHERE ts >= ? "
                "GROUP BY provider, model ORDER BY requests DESC LIMIT 50", (since,))]
            by_day = [dict(r) for r in db.execute(
                "SELECT date(ts, 'unixepoch', 'localtime') AS day, COUNT(*) AS requests, SUM(ok) AS ok, "
                "COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
                "CAST(AVG(latency_ms) AS INTEGER) AS avg_latency_ms FROM usage WHERE ts >= ? "
                "GROUP BY day ORDER BY day", (since,))]
            recent = [dict(r) for r in db.execute(
                "SELECT ts, provider, model, ok, status, latency_ms, stream FROM usage ORDER BY ts DESC LIMIT 15")]
        totals["ok"] = totals["ok"] or 0
        totals["errors"] = (totals["requests"] or 0) - totals["ok"]
        return {"days": days, "totals": totals, "by_provider": by_provider, "by_day": by_day, "recent": recent}
