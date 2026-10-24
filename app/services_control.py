"""Liga e desliga, pelo painel, os serviços locais que o Hera usa (hoje só o Ollama).

Só existe esta lista curta de serviços e os comandos são FIXOS (nada vem da requisição): o Hera não executa texto de ninguém.
  * o processo nasce sem janela, em loopback (127.0.0.1) e com ambiente enxuto (sem chaves de API do Hera);
  * "Parar" só encerra o que o PRÓPRIO Hera iniciou (árvore inteira, para não deixar filhos órfãos); um serviço
    aberto por fora (ícone da bandeja, outro terminal) é só reportado como ativo;
  * o log de cada serviço vai para `<data>/logs/`.
"""
from __future__ import annotations

import os
import shutil
import socket
import subprocess  # nosec B404
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
SECRET_ENV_SUFFIXES = ("_API_KEY", "_API_KEYS", "_TOKEN", "_SECRET", "_PASSWORD")

SERVICES: Dict[str, Dict[str, Any]] = {
    "ollama": {
        "label": "Ollama", "port": 11434, "url": "http://127.0.0.1:11434",
        "candidates": ["ollama", str(Path(LOCALAPPDATA) / "Programs" / "Ollama" / "ollama.exe")],
        "args": ["serve"], "env": {"OLLAMA_HOST": "127.0.0.1:11434"},
        "install_hint": "Instale o Ollama em https://ollama.com/download",
    },
}


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def clean_env(extra: Dict[str, str]) -> Dict[str, str]:
    """Ambiente do processo filho: o do usuário, sem nada que pareça segredo ou chave do Hera."""
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("HERA_") and not k.upper().endswith(SECRET_ENV_SUFFIXES)}
    env.update(extra)
    return env


class ServiceManager:
    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)
        self._procs: Dict[str, subprocess.Popen] = {}

    # ------------------------------------------------------------------ consulta
    def resolve_exe(self, name: str) -> Optional[str]:
        for candidate in SERVICES[name]["candidates"]:
            found = shutil.which(candidate)
            if found:
                return found
        return None

    def _log_path(self, name: str) -> Path:
        return self.log_dir / f"{name}.log"

    def _log_tail(self, name: str, limit: int = 400) -> str:
        try:
            data = self._log_path(name).read_bytes()[-4000:].decode("utf-8", "replace")
        except OSError:
            return ""
        lines = [ln.strip() for ln in data.splitlines() if ln.strip()]
        return " | ".join(lines[-4:])[-limit:]

    def status(self, name: str) -> Dict[str, Any]:
        spec = SERVICES[name]
        proc = self._procs.get(name)
        managed = proc is not None and proc.poll() is None
        info: Dict[str, Any] = {
            "name": name, "label": spec["label"], "url": spec["url"], "port": spec["port"],
            "dashboard": spec.get("dashboard"), "installed": self.resolve_exe(name) is not None,
            "running": port_open(spec["port"]), "managed": managed, "pid": proc.pid if managed else None,
            "install_hint": spec["install_hint"],
        }
        if proc is not None and not managed and not info["running"]:
            info["exit_code"] = proc.returncode
            info["last_log"] = self._log_tail(name)
        return info

    def all_status(self) -> List[Dict[str, Any]]:
        return [self.status(n) for n in SERVICES]

    # ------------------------------------------------------------------ ações
    def start(self, name: str) -> Dict[str, Any]:
        spec = SERVICES[name]
        if port_open(spec["port"]):
            return {**self.status(name), "message": f"{spec['label']} já está rodando."}
        exe = self.resolve_exe(name)
        if not exe:
            raise FileNotFoundError(f"{spec['label']} não foi encontrado. {spec['install_hint']}")
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log = open(self._log_path(name), "ab")          # noqa: SIM115 - fica aberto enquanto o filho vive
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            # Comando FIXO (tabela SERVICES), sem shell e sem argumento vindo da requisição.
            self._procs[name] = subprocess.Popen(  # nosec B603
                [exe, *spec["args"]], stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                env=clean_env(spec["env"]), creationflags=flags, close_fds=True)
        finally:
            log.close()
        return {**self.status(name), "message": f"Iniciando {spec['label']}..."}

    def wait_until_up(self, name: str, seconds: float = 25.0) -> bool:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if port_open(SERVICES[name]["port"]):
                return True
            proc = self._procs.get(name)
            if proc is not None and proc.poll() is not None:
                return False
            time.sleep(0.4)
        return False

    def stop(self, name: str) -> Dict[str, Any]:
        spec = SERVICES[name]
        proc = self._procs.get(name)
        if proc is None or proc.poll() is not None:
            if port_open(spec["port"]):
                raise PermissionError(f"{spec['label']} foi aberto fora do Hera; feche-o pelo ícone da bandeja ou pelo terminal onde abriu.")
            return {**self.status(name), "message": f"{spec['label']} já está parado."}
        if sys.platform == "win32":                       # encerra a ÁRVORE (cmd -> node -> filhos)
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],  # nosec B603 B607
                           capture_output=True, timeout=20, check=False)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        self._procs.pop(name, None)
        return {**self.status(name), "message": f"{spec['label']} parado."}
