"""HERA Multiagent - Human-in-the-Loop (HITL) & Visual Diff Review System.

Garante que qualquer código gerado por agentes só seja integrado após
revisão humana explícita, verificação criptográfica de hashes de diff e
operações git seguras em worktrees isoladas.
"""

from app.hitl.schemas import (
    DiffProposal,
    DiffStats,
    DiffAlert,
    ApprovalToken,
    ApprovalRequest,
    RejectRequest,
    RequestChangesRequest,
    MergeResult,
)

__all__ = [
    "DiffProposal",
    "DiffStats",
    "DiffAlert",
    "ApprovalToken",
    "ApprovalRequest",
    "RejectRequest",
    "RequestChangesRequest",
    "MergeResult",
]
