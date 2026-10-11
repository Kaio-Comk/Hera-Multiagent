"""Gerenciador de cache incremental por SHA-256 para o Repo-Map.

Evita reindexação desnecessária de arquivos inalterados no workspace.
"""

import hashlib
from pathlib import Path
from typing import Dict, Any, Optional

def compute_file_hash(path: Path) -> str:
    """Calcula o hash SHA-256 do conteúdo de um arquivo."""
    hasher = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            hasher.update(chunk)
    return hasher.hexdigest()

class RepoMapCache:
    """Cache em memória para metadados e símbolos indexados."""

    def __init__(self):
        self._entries: Dict[str, Dict[str, Any]] = {}

    def get(self, file_path: str, current_hash: str) -> Optional[Dict[str, Any]]:
        """Retorna os símbolos cacheados se o hash do arquivo ainda for idêntico."""
        entry = self._entries.get(file_path)
        if entry and entry.get("hash") == current_hash:
            return entry.get("data")
        return None

    def put(self, file_path: str, file_hash: str, data: Dict[str, Any]):
        """Armazena o resultado da indexação associado ao hash do arquivo."""
        self._entries[file_path] = {
            "hash": file_hash,
            "data": data
        }

    def invalidate(self, file_path: str):
        """Remove a entrada do cache para um arquivo modificado ou removido."""
        self._entries.pop(file_path, None)

    def clear(self):
        """Limpa todo o cache."""
        self._entries.clear()
