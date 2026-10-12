"""Política de lançamento de servidores MCP stdio (plano mestre v3, seção 7).

Um servidor MCP stdio é CÓDIGO que roda na sua máquina com os seus direitos. Registrar um é, por definição,
autorizar execução; por isso a política é estrita:

  * o executável é só o NOME de um interpretador da allowlist (o catálogo já exige isso) e é resolvido
    pelo PATH;
  * o 1º argumento é um SCRIPT `.py` que precisa existir e estar dentro de uma raiz permitida: nada de
    `-c`, `-m`, `-` (stdin), nem script de fora do projeto;
  * o ambiente do processo é MÍNIMO (PATH, TEMP, ...): chaves de API e segredos do Hera NÃO são herdados.
    Variáveis declaradas no servidor entram, exceto as que injetam código (PYTHONPATH, PATH, ...).
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional

from app.mcp.schemas import McpServerConfig

PYTHON_FAMILY = {"python", "python3", "py"}
SAFE_ENV_VARS = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "USERPROFILE",
                 "HOME", "LOCALAPPDATA", "APPDATA", "LANG", "LC_ALL")
BLOCKED_ENV_VARS = frozenset({
    "PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "WINDIR", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME",
    "PYTHONINSPECT", "PYTHONBREAKPOINT", "PYTHONEXECUTABLE", "PYTHONUSERBASE", "PYTHONWARNINGS",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "DYLD_INSERT_LIBRARIES", "NODE_OPTIONS", "HTTP_PROXY", "HTTPS_PROXY",
})
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
MAX_ARGS = 20
MAX_ARG_LEN = 500
MAX_ENV_VALUE = 4096


class McpLaunchError(ValueError):
    """Servidor MCP stdio recusado pela política de lançamento."""


def _inside(path: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        try:
            path.resolve().relative_to(Path(root).resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


def validate_stdio_server(cfg: McpServerConfig, allowed_roots: Iterable[Path]) -> None:
    """Levanta `McpLaunchError` se o servidor stdio não puder ser lançado com segurança."""
    if cfg.transport != "stdio":
        return
    name = (cfg.command or "").strip().lower()
    if name.endswith(".exe"):
        name = name[:-4]
    if name not in PYTHON_FAMILY:
        raise McpLaunchError(f"Interpretador '{cfg.command}' não suportado para lançamento (use python).")
    if not cfg.args:
        raise McpLaunchError("Informe o script do servidor MCP como primeiro argumento (um arquivo .py).")
    if len(cfg.args) > MAX_ARGS or any(not isinstance(a, str) or len(a) > MAX_ARG_LEN or "\x00" in a for a in cfg.args):
        raise McpLaunchError(f"No máximo {MAX_ARGS} argumentos de até {MAX_ARG_LEN} caracteres.")
    script = cfg.args[0]
    if script.startswith("-"):
        raise McpLaunchError("O primeiro argumento deve ser o caminho de um script .py, não uma opção (-c/-m/-).")
    if not script.lower().endswith(".py"):
        raise McpLaunchError("O script do servidor MCP precisa ter a extensão .py.")
    path = Path(script)
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.is_file():
        raise McpLaunchError(f"Script não encontrado: {script}")
    if not _inside(path, allowed_roots):
        raise McpLaunchError("O script do servidor MCP precisa estar dentro de uma raiz permitida do projeto.")
    for key, value in cfg.env.items():
        if not _ENV_NAME.match(key) or key in BLOCKED_ENV_VARS:
            raise McpLaunchError(f"Variável de ambiente não permitida para servidor MCP: {key!r}")
        if not isinstance(value, str) or len(value) > MAX_ENV_VALUE or "\x00" in value:
            raise McpLaunchError(f"Valor inválido para a variável {key!r}")


def build_argv(cfg: McpServerConfig) -> List[str]:
    """argv resolvido (executável pelo PATH + script absoluto + demais argumentos). Sem shell."""
    exe = shutil.which(cfg.command or "")
    if not exe:
        raise McpLaunchError(f"Executável não encontrado no PATH: {cfg.command}")
    script = Path(cfg.args[0])
    if not script.is_absolute():
        script = (Path.cwd() / script)
    return [exe, str(script.resolve()), *cfg.args[1:]]


def build_env(cfg: McpServerConfig, environ: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """Ambiente mínimo: nada de chaves de API nem de segredos do Hera."""
    env_src = os.environ if environ is None else environ
    env = {k: env_src[k] for k in SAFE_ENV_VARS if k in env_src}
    env.update({"PYTHONUTF8": "1", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"})
    env.update(cfg.env)
    return env
