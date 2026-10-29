from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Mapping

from app.memory.index import MemoryIndex
from app.memory.recall import montar_snapshot
from app.memory.store import MemoryStore
from app.privacy_guard import PrivacyGuard

logger = logging.getLogger(__name__)

_VERDADEIROS = {'1', 'true', 'sim', 'on'}


def _opt_in(environ: Mapping[str, str] | None) -> bool:
    env = os.environ if environ is None else environ
    valor = env.get('HERA_MEMORY_SNAPSHOT', '')
    return valor.strip().lower() in _VERDADEIROS


def contexto_da_memoria(
    instrucao: str,
    *,
    environ: Mapping[str, str] | None = None,
    pasta: Path | None = None,
) -> str:
    '''Devolve o snapshot da memoria sanitizado, se opt-in estiver ligado.'''
    if not instrucao or not instrucao.strip():
        return ''
    if not _opt_in(environ):
        return ''
    try:
        from cli.memory_cmd import pasta_padrao

        base = Path(pasta) if pasta is not None else Path(pasta_padrao(environ))
        if not base.exists() or not base.is_dir():
            return ''
        store = MemoryStore(base)
        with MemoryIndex(store, base / '.indice.sqlite') as index:
            snap = montar_snapshot(index, instrucao)
        return PrivacyGuard.sanitize_content(snap.texto)
    except Exception as exc:  # noqa: BLE001
        logger.warning('falha ao montar contexto da memoria: %s', exc)
        return ''
