"""Registro de uso do Roteador (SQLite local): chamadas, tokens, custo estimado, latência e erros por provedor,
modelo, cliente e dia.

Guarda SÓ metadados (provedor, modelo, resultado, tokens, latência, nome do cliente, erro já higienizado e curto).
Nunca o texto do prompt nem da resposta.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

RETENTION_DAYS = 90
_COLUMNS = {"client": "TEXT", "requested_model": "TEXT", "request_id": "TEXT", "error": "TEXT", "cost": "REAL"}


def _day_start() -> float:
    now = time.localtime()
    return time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))


class UsageLedger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS usage (
                ts REAL NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, ok INTEGER NOT NULL,
                status INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, latency_ms INTEGER, stream INTEGER)""")
            have = {row[1] for row in db.execute("PRAGMA table_info(usage)")}
            for name, kind in _COLUMNS.items():                       # migração de bancos antigos
                if name not in have:
                    db.execute(f"ALTER TABLE usage ADD COLUMN {name} {kind}")  # nosec B608
            db.execute("CREATE INDEX IF NOT EXISTS usage_ts ON usage(ts)")
            db.execute("CREATE INDEX IF NOT EXISTS usage_client_ts ON usage(client, ts)")
        self._since_prune = 0
        self._cache: Dict[str, Any] = {}

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=5)

    # ------------------------------------------------------------------ escrita
    def record(self, provider: str, model: str, ok: bool, status: Optional[int] = None,
               prompt_tokens: Optional[int] = None, completion_tokens: Optional[int] = None,
               latency_ms: Optional[int] = None, stream: bool = False, client: Optional[str] = None,
               requested_model: Optional[str] = None, request_id: Optional[str] = None,
               error: Optional[str] = None, cost: Optional[float] = None) -> None:
        """Nunca levanta: um erro no contador não pode derrubar uma requisição."""
        try:
            with self._lock, self._connect() as db:
                db.execute("INSERT INTO usage (ts, provider, model, ok, status, prompt_tokens, completion_tokens, latency_ms,"
                           " stream, client, requested_model, request_id, error, cost) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           (time.time(), str(provider)[:60], str(model)[:200], 1 if ok else 0, status, prompt_tokens,
                            completion_tokens, latency_ms, 1 if stream else 0, (client or "")[:60] or None,
                            (requested_model or "")[:200] or None, (request_id or "")[:40] or None,
                            (error or "")[:240] or None, cost))
                self._cache.clear()
                self._since_prune += 1
                if self._since_prune >= 500:
                    self._since_prune = 0
                    db.execute("DELETE FROM usage WHERE ts < ?", (time.time() - RETENTION_DAYS * 86400,))
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------------ leitura
    def today(self, provider: Optional[str] = None, client: Optional[str] = None) -> Dict[str, int]:
        """Chamadas e tokens de HOJE (para limites diários). Com cache curto: é consultado a cada requisição."""
        key = f"today:{provider}:{client}"
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < 5:
            return hit[1]
        sql, args = ("SELECT COUNT(*), COALESCE(SUM(COALESCE(prompt_tokens,0)+COALESCE(completion_tokens,0)),0) "
                     "FROM usage WHERE ts >= ?"), [_day_start()]
        if provider:
            sql += " AND provider = ?"
            args.append(provider)
        if client:
            sql += " AND client = ?"
            args.append(client)
        try:
            with self._lock, self._connect() as db:
                requests, tokens = db.execute(sql, args).fetchone()
        except sqlite3.Error:
            return {"requests": 0, "tokens": 0}
        out = {"requests": int(requests or 0), "tokens": int(tokens or 0)}
        self._cache[key] = (time.monotonic(), out)
        return out

    def latency_stats(self, window_hours: int = 24) -> Dict[str, Dict[str, Any]]:
        """Latência média e nº de chamadas OK por provedor (para a estratégia 'mais rápido')."""
        key = f"lat:{window_hours}"
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < 15:
            return hit[1]
        out: Dict[str, Dict[str, Any]] = {}
        try:
            with self._lock, self._connect() as db:
                for provider, avg, n in db.execute(
                        "SELECT provider, AVG(latency_ms), COUNT(*) FROM usage WHERE ok = 1 AND latency_ms IS NOT NULL "
                        "AND ts >= ? GROUP BY provider", (time.time() - window_hours * 3600,)):
                    out[provider] = {"avg_ms": int(avg or 0), "calls": int(n)}
        except sqlite3.Error:
            return {}
        self._cache[key] = (time.monotonic(), out)
        return out

    def summary(self, days: int = 7) -> Dict[str, Any]:
        days = max(1, min(int(days), RETENTION_DAYS))
        since = time.time() - days * 86400
        with self._lock, self._connect() as db:
            db.row_factory = sqlite3.Row
            # Consultas LITERAIS (sem montar SQL por texto): nada aqui depende de entrada do usuario.
            totals = dict(db.execute(
                "SELECT COUNT(*) AS requests, SUM(ok) AS ok, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
                "COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
                "CAST(AVG(latency_ms) AS INTEGER) AS avg_latency_ms, COALESCE(SUM(cost),0) AS cost_usd, "
                "SUM(CASE WHEN cost IS NULL AND ok = 1 THEN 1 ELSE 0 END) AS unpriced "
                "FROM usage WHERE ts >= ?", (since,)).fetchone())
            by_provider = [dict(r) for r in db.execute(
                "SELECT provider, model, COUNT(*) AS requests, SUM(ok) AS ok, "
                "COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
                "CAST(AVG(latency_ms) AS INTEGER) AS avg_latency_ms, COALESCE(SUM(cost),0) AS cost_usd FROM usage "
                "WHERE ts >= ? GROUP BY provider, model ORDER BY requests DESC LIMIT 50", (since,))]
            by_day = [dict(r) for r in db.execute(
                "SELECT date(ts, 'unixepoch', 'localtime') AS day, COUNT(*) AS requests, SUM(ok) AS ok, "
                "COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
                "CAST(AVG(latency_ms) AS INTEGER) AS avg_latency_ms, COALESCE(SUM(cost),0) AS cost_usd FROM usage "
                "WHERE ts >= ? GROUP BY day ORDER BY day", (since,))]
            by_client = [dict(r) for r in db.execute(
                "SELECT COALESCE(client, 'painel/token mestre') AS client, COUNT(*) AS requests, SUM(ok) AS ok, "
                "COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
                "COALESCE(SUM(cost),0) AS cost_usd FROM usage WHERE ts >= ? GROUP BY client ORDER BY requests DESC LIMIT 30",
                (since,))]
            lat = sorted(r[0] for r in db.execute(
                "SELECT latency_ms FROM usage WHERE ok = 1 AND latency_ms IS NOT NULL AND ts >= ?", (since,)))
            recent = [dict(r) for r in db.execute(
                "SELECT ts, provider, model, ok, status, latency_ms, stream FROM usage ORDER BY ts DESC LIMIT 15")]
        totals["ok"] = totals["ok"] or 0
        totals["unpriced"] = totals["unpriced"] or 0
        totals["errors"] = (totals["requests"] or 0) - totals["ok"]
        totals["success_rate"] = round(100 * totals["ok"] / totals["requests"], 1) if totals["requests"] else None
        totals["p50_ms"] = lat[len(lat) // 2] if lat else None
        totals["p95_ms"] = lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else None
        return {"days": days, "totals": totals, "by_provider": by_provider, "by_day": by_day, "by_client": by_client,
                "recent": recent}

    def logs(self, limit: int = 100, provider: Optional[str] = None, client: Optional[str] = None,
             only_errors: bool = False) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        sql = ("SELECT ts, provider, model, requested_model, ok, status, latency_ms, prompt_tokens, completion_tokens, "
               "stream, client, request_id, error, cost FROM usage WHERE 1=1")
        args: List[Any] = []
        if provider:
            sql += " AND provider = ?"
            args.append(provider)
        if client:
            sql += " AND client = ?"
            args.append(client)
        if only_errors:
            sql += " AND ok = 0"
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self._lock, self._connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(sql, args)]
