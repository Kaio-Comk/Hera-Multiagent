"""Contenção de caminhos: o que o cliente da API informa nunca sai das raízes permitidas.

Fase 3 do plano de correções (12/10/2026). Antes, `/api/hitl/proposals` aceitava qualquer
`repo_path` e rodava git nele; agora o caminho é resolvido (links e `..` inclusos) e precisa
ficar dentro de uma raiz permitida (`ROOT_DIR` do projeto + `general.allowed_repo_roots` do
config.yaml, que NÃO é editável pela API).
"""
from __future__ import annotations

import ntpath
import posixpath
from pathlib import Path
from typing import Iterable, List


class PathNotAllowedError(ValueError):
    """Caminho fora das raízes permitidas, ou relativo inseguro."""


def ensure_inside(path: str, roots: Iterable[Path]) -> Path:
    """Resolve `path` e garante que está dentro de alguma raiz. Devolve o caminho resolvido."""
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise PathNotAllowedError("Caminho vazio ou inválido.")
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathNotAllowedError(f"Caminho inválido: {exc}") from exc
    for root in roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return resolved
        except (ValueError, OSError):
            continue
    raise PathNotAllowedError(f"Caminho fora das raízes permitidas: {path}")


def is_safe_relative(path: str) -> bool:
    """Caminho RELATIVO sem `..`, sem unidade/raiz e sem NUL (usado em `allowed_paths`)."""
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        return False
    for mod in (ntpath, posixpath):
        if mod.isabs(path) or mod.splitdrive(path)[0] or path.startswith(("/", "\\")):
            return False
        if ".." in mod.normpath(path).split(mod.sep):
            return False
    return ".." not in path.replace("\\", "/").split("/")


def ensure_safe_relative_list(paths: List[str]) -> List[str]:
    bad = [p for p in paths if not is_safe_relative(p)]
    if bad:
        raise PathNotAllowedError(f"allowed_paths precisa de caminhos relativos sem '..': {bad[:3]}")
    return paths
