"""Console ao vivo do Hera: tudo o que acontece durante um plano ou tarefa, em ordem, para a tela e para o arquivo.

  * `emit` guarda o evento (memoria, ate 5000), manda para a tela (WebSocket) e acrescenta a um arquivo por execucao
    (`data/runs/<id>.jsonl`), para reabrir depois mesmo reiniciando o Hera;
  * os textos passam pelo filtro de segredos ANTES de sair do processo (chaves e senhas viram `[REDACTED]`);
  * limites: 8000 caracteres por evento, 8 MiB por execucao, 40 execucoes guardadas.
O arquivo de log (`hera.log`) continua sem conteudo de conversa: aqui e o transcrito da execucao, so no seu PC.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

from app.privacy_guard import PrivacyGuard

MAX_EVENTS = 5000
MAX_TEXT = 8000
MAX_RUN_BYTES = 8 * 1024 * 1024
KEEP_RUNS = 40
_RUN_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class ConsoleBus:
    def __init__(self, runs_dir: Path, broadcast: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None):
        self.runs_dir = Path(runs_dir)
        self._broadcast = broadcast
        self._events: Deque[Dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._seq = 0
        self._lock = threading.Lock()
        self._sizes: Dict[str, int] = {}
        self._last: Dict[tuple, float] = {}
        self.current_run: Optional[str] = None

    # ------------------------------------------------------------------ escrita
    def emit(self, kind: str, text: str = "", *, run: Optional[str] = None, step: Optional[str] = None,
             **meta: Any) -> Dict[str, Any]:
        clean = PrivacyGuard.sanitize_content(str(text).replace(chr(0), "")) if text else ""   # PowerShell devolve UTF-16 (NULs)
        if kind == "text" and len(clean) > MAX_TEXT:
            # Resposta longa de um passo: vira varios eventos em ordem, em vez de ser cortada em silencio
            # (o plano.py junta os eventos do passo; antes ele entregava arquivos e codigo pela metade).
            evento: Dict[str, Any] = {}
            for i in range(0, len(clean), MAX_TEXT):
                evento = self._emitir(kind, clean[i:i + MAX_TEXT], run, step, meta)
            return evento
        return self._emitir(kind, clean[:MAX_TEXT], run, step, meta)

    def _emitir(self, kind: str, clean: str, run: Optional[str], step: Optional[str], meta: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "ts": time.time(), "kind": kind, "run": run, "step": step, "text": clean, **meta}
            self._events.append(event)
            self._last[(run, step)] = event["ts"]
        if run and _RUN_ID.match(run):
            self._persist(run, event)
        self._send(event)
        return event

    def _send(self, event: Dict[str, Any]) -> None:
        if self._broadcast is None:
            return
        try:
            asyncio.get_running_loop().create_task(self._broadcast({"type": "console", **event}))
        except RuntimeError:
            pass                                     # sem loop ativo (testes sincronos): so fica guardado

    def _persist(self, run: str, event: Dict[str, Any]) -> None:
        try:
            self.runs_dir.mkdir(parents=True, exist_ok=True)
            path = self.runs_dir / f"{run}.jsonl"
            line = json.dumps(event, ensure_ascii=False) + "\n"
            with self._lock:
                size = self._sizes.get(run)
                if size is None:
                    size = path.stat().st_size if path.exists() else 0
                if size + len(line) > MAX_RUN_BYTES:
                    return
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write(line)
                self._sizes[run] = size + len(line)
            if event["kind"] == "plan_start":
                self._prune()
        except OSError:
            pass                                      # disco cheio/sem permissao: o ao vivo continua funcionando

    def _prune(self) -> None:
        files = sorted(self.runs_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old in files[KEEP_RUNS:]:
            try:
                old.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------ leitura
    def events(self, after: int = 0, limit: int = 500, run: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            items = [e for e in self._events if e["seq"] > after and (run is None or e["run"] == run)]
        return items[-max(1, min(limit, 2000)):]

    def idle_seconds(self, run: str, step: str) -> float:
        """Ha quanto tempo o passo nao diz nada (para o aviso 'ainda trabalhando')."""
        return time.time() - self._last.get((run, step), time.time())

    def sink_for(self, run: str, step: str, **ctx: Any) -> Callable[..., None]:
        """Funcao `sink(kind, text, **meta)` ligada a um passo (para adapters.progress.bind)."""
        def sink(kind: str, text: str = "", **meta: Any) -> None:
            self.emit(kind, text, run=run, step=step, **ctx, **meta)
        return sink

    def list_runs(self) -> List[Dict[str, Any]]:
        out = []
        if not self.runs_dir.is_dir():
            return out
        for path in sorted(self.runs_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:KEEP_RUNS]:
            info: Dict[str, Any] = {"id": path.stem, "updated": path.stat().st_mtime, "title": "", "steps": 0, "status": ""}
            try:
                with open(path, encoding="utf-8") as fh:
                    for line in fh:
                        ev = json.loads(line)
                        if ev["kind"] == "plan_start":
                            info["title"], info["steps"], info["started"] = ev.get("title", ""), ev.get("total", 0), ev["ts"]
                        elif ev["kind"] == "plan_end":
                            info["status"] = ev.get("status", "")
            except (OSError, ValueError):
                pass
            out.append(info)
        return out

    def read_run(self, run: str) -> List[Dict[str, Any]]:
        if not _RUN_ID.match(run):
            return []
        path = self.runs_dir / f"{run}.jsonl"
        events: List[Dict[str, Any]] = []
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        events.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return []
        return events[:5000]
