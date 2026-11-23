"""Pacote principal do Hera.

Os nomes abaixo continuam disponiveis como `from app import StateStore`, mas sao carregados
SOB DEMANDA (PEP 562). Antes este arquivo importava os 9 modulos na hora, entao qualquer
`import app.<qualquer coisa>` (ate `hera --help`) pagava pydantic, adaptadores e DAG: ~1 s.
"""
from importlib import import_module

_LAZY = {
    "StateStore": "app.state_store",
    "Coordinator": "app.coordinator",
    "TaskRouter": "app.router",
    "ObsidianVaultWriter": "app.vault.obsidian",
    "TaskDAG": "app.dag_scheduler",
    "DAGScheduler": "app.dag_scheduler",
    "WorktreeManager": "app.worktree_manager",
    "TaskValidator": "app.validator",
    "ValidationResult": "app.validator",
    "ObsidianMemoryManager": "app.vault.memory",
    "AutoFixEngine": "app.auto_fix",
}

__all__ = list(_LAZY)


def __getattr__(name):
    modulo = _LAZY.get(name)
    if modulo is None:
        raise AttributeError(f"module 'app' has no attribute {name!r}")
    valor = getattr(import_module(modulo), name)
    globals()[name] = valor          # proximas consultas nao passam mais por aqui
    return valor


def __dir__():
    return sorted(set(globals()) | set(_LAZY))
