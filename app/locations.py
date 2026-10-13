r"""Locais padrao PORTATEIS: nenhum caminho de usuario fixo no codigo (fase 4, 12/10/2026).

Antes, `C:\Users\<nome>\...` estava escrito em 7 arquivos (config, Obsidian, memoria do vault,
CLI e adaptador do Antigravity): quebrava em qualquer outra maquina e expunha o usuario no
repositorio publico. Ordem de resolucao: valor do config.yaml > variavel de ambiente > padrao
derivado do HOME / LOCALAPPDATA do usuario atual.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Mapping, Optional

VAULT_ENV = "HERA_VAULT_DIR"
AGY_ENV = "HERA_AGY_PATH"
VAULT_PROJECT_NAME = "Orquestrador Multiagente"


def default_vault_dir(environ: Optional[Mapping[str, str]] = None) -> str:
    """Pasta do projeto no Obsidian: $HERA_VAULT_DIR ou ~/Documents/Obsidian Vault/<projeto>."""
    env = os.environ if environ is None else environ
    fixed = (env.get(VAULT_ENV) or "").strip()
    if fixed:
        return fixed
    return str(Path.home() / "Documents" / "Obsidian Vault" / VAULT_PROJECT_NAME)


def resolve_vault_dir(configured: Optional[str] = None, environ: Optional[Mapping[str, str]] = None) -> str:
    """`configured` (config.yaml / argumento de CLI) tem prioridade; vazio cai no padrao."""
    value = (configured or "").strip()
    return value if value else default_vault_dir(environ)


def find_vault_root(vault_dir: str, configured: Optional[str] = None) -> Path:
    """Raiz do cofre do Obsidian: `general.vault_root` > a pasta acima de `vault_dir` que tem `.obsidian` > `vault_dir`."""
    value = (configured or "").strip()
    if value:
        return Path(value)
    start = Path(vault_dir)
    for candidate in (start, *start.parents):
        if (candidate / ".obsidian").is_dir():
            return candidate
    return start


def find_agy_binary(environ: Optional[Mapping[str, str]] = None) -> str:
    r"""Executavel do Antigravity: $HERA_AGY_PATH > PATH > %LOCALAPPDATA%\agy\bin > nome simples."""
    env = os.environ if environ is None else environ
    fixed = (env.get(AGY_ENV) or "").strip()
    if fixed and os.path.exists(fixed):
        return fixed
    found = shutil.which("agy")
    if found:
        return found
    local = (env.get("LOCALAPPDATA") or "").strip()
    if local:
        candidate = os.path.join(local, "agy", "bin", "agy.EXE")
        if os.path.exists(candidate):
            return candidate
    return "agy"
