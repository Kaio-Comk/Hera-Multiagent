"""HERA Multiagent - Multi-Model Arena Module.

Fornece avaliação e telemetria comparativa entre múltiplos modelos locais e na nuvem,
com controle de semáforo de GPU para a GTX 1650 Super (4GB), barreira de privacidade
e descarte automático de rotas cloud para prompts restritos ao ambiente local.
"""

from app.arena.schemas import (
    ArenaCandidate,
    CandidateResult,
    ArenaRunRequest,
    ArenaRunReport,
)
from app.arena.runner import ArenaRunner

__all__ = [
    "ArenaCandidate",
    "CandidateResult",
    "ArenaRunRequest",
    "ArenaRunReport",
    "ArenaRunner",
]
