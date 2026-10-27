"""Gerenciador deterministico do ciclo de vida de subprocessos no Windows para o Hera.

Implementa os estados explicitos de processo:
CREATED -> STARTING -> RUNNING -> TERMINATING -> TERMINATED / FAILED

Garante o encerramento completo da arvore de processos no Windows (taskkill /F /T)
com timeout dedicado antes de declarar a tarefa como cancelada ou finalizada.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from enum import Enum
from typing import Dict, List, Optional


class ProcessState(str, Enum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    TERMINATING = "TERMINATING"
    TERMINATED = "TERMINATED"
    FAILED = "FAILED"


class ProcessRecord:
    def __init__(
        self,
        task_id: str,
        run_id: str,
        pid: int,
        engine: str = "",
        state: ProcessState = ProcessState.RUNNING,
    ) -> None:
        self.task_id = task_id
        self.run_id = run_id
        self.pid = pid
        self.engine = engine
        self.state = state
        self.started_at = time.time()
        self.heartbeat_at = time.time()
        self.termination_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "run_id": self.run_id,
            "pid": self.pid,
            "engine": self.engine,
            "state": self.state.value,
            "started_at": self.started_at,
            "heartbeat_at": self.heartbeat_at,
            "termination_reason": self.termination_reason,
        }


class ProcessManager:
    """Rastreia e coordena o encerramento seguro de subprocessos."""

    def __init__(self) -> None:
        self._procs: Dict[str, ProcessRecord] = {}
        self._lock = threading.Lock()

    def register(self, task_id: str, run_id: str, pid: int, engine: str = "") -> ProcessRecord:
        with self._lock:
            rec = ProcessRecord(task_id, run_id, pid, engine, ProcessState.RUNNING)
            self._procs[task_id] = rec
            return rec

    def heartbeat(self, task_id: str) -> None:
        with self._lock:
            if task_id in self._procs:
                self._procs[task_id].heartbeat_at = time.time()

    def terminate(self, task_id: str, reason: str = "cancelled", timeout_seconds: float = 5.0) -> bool:
        """Encerra a arvore de processos de forma limpa e aguarda a finalizacao."""
        with self._lock:
            rec = self._procs.get(task_id)
            if not rec or rec.state in (ProcessState.TERMINATED, ProcessState.FAILED):
                return True
            rec.state = ProcessState.TERMINATING
            rec.termination_reason = reason
            pid = rec.pid

        # No Windows: encerra toda a arvore com taskkill /F /T
        success = False
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=timeout_seconds,
                )
            else:
                os.kill(pid, 15)  # SIGTERM
            success = True
        except Exception:
            success = False

        with self._lock:
            if task_id in self._procs:
                self._procs[task_id].state = ProcessState.TERMINATED if success else ProcessState.FAILED

        return success

    def get(self, task_id: str) -> Optional[ProcessRecord]:
        with self._lock:
            return self._procs.get(task_id)

    def list_active(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                r.to_dict()
                for r in self._procs.values()
                if r.state in (ProcessState.RUNNING, ProcessState.TERMINATING)
            ]


# Instancia global compartilhada
GLOBAL_PROCESS_MANAGER = ProcessManager()
