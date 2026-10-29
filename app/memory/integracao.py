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

_DESLIGADOS = {'0', 'false', 'nao', 'off'}


def _opt_in(environ: Mapping[str, str] | None) -> bool:
    env = os.environ if environ is None else environ
    valor = env.get('HERA_MEMORY_SNAPSHOT', '')
    return valor.strip().lower() not in _DESLIGADOS


def contexto_da_memoria(
    instrucao: str,
    *,
    environ: Mapping[str, str] | None = None,
    pasta: Path | None = None,
) -> str:
    '''Devolve o snapshot da memoria sanitizado, salvo se HERA_MEMORY_SNAPSHOT=0.'''
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


def injetar_memoria_nos_passos(
    passos: list,
    *,
    environ: Mapping[str, str] | None = None,
    pasta: Path | None = None,
    max_total: int = 4000,
) -> int:
    '''Antepoe o snapshot da memoria a instrucao de cada passo de um plano (servidor/DAG). Devolve quantos passos receberam.

    Ligado por padrao; HERA_MEMORY_SNAPSHOT=0 desliga. A tarefa original fica INTEIRA no fim; se nao sobrar espaco ate `max_total`, a memoria
    e cortada, e abaixo de 200 caracteres uteis o passo nao e alterado. Nunca levanta: memoria e conveniencia.
    '''
    injetados = 0
    try:
        for passo in passos:
            if not isinstance(passo, dict):
                continue
            tarefa = passo.get('instruction')
            if not isinstance(tarefa, str) or not tarefa.strip():
                continue
            contexto = contexto_da_memoria(tarefa, environ=environ, pasta=pasta)
            if not contexto:
                continue
            sobra = max_total - len(tarefa) - 1
            if sobra < 200:
                continue
            if len(contexto) > sobra:
                contexto = contexto[:sobra - 12].rstrip() + '\n[cortado]'
            passo['instruction'] = contexto + '\n' + tarefa
            injetados += 1
    except Exception as exc:  # noqa: BLE001
        logger.warning('falha ao injetar memoria nos passos: %s', exc)
    return injetados
