import time
from typing import List, Optional, Literal
from pydantic import BaseModel, Field


class DiffStats(BaseModel):
    """Estatísticas agregadas de um diff de código."""
    files_changed: int = 0
    insertions: int = 0
    deletions: int = 0
    binary_files_count: int = 0


class DiffAlert(BaseModel):
    """Alerta de segurança ou integridade identificado durante análise do diff."""
    level: Literal["warning", "danger", "info"]
    category: Literal["secret", "sensitive_file", "binary", "out_of_scope", "conflict"]
    message: str
    file_path: Optional[str] = None


class DiffProposal(BaseModel):
    """Proposta formal de alteração contendo diff exato e referências Git."""
    id: str
    task_id: str
    repo_path: str
    worktree_id: Optional[str] = None
    worktree_path: Optional[str] = None
    target_branch: str = "master"
    feature_branch: str
    base_commit: str
    head_commit: str
    diff_content: str
    diff_hash: str  # SHA-256 do diff serializado
    stats: DiffStats = Field(default_factory=DiffStats)
    alerts: List[DiffAlert] = Field(default_factory=list)
    status: Literal["pending_review", "approved", "rejected", "changes_requested", "merged"] = "pending_review"
    created_at: float = Field(default_factory=time.time)
    reviewed_at: Optional[float] = None
    reviewer_id: Optional[str] = None


class ApprovalRequest(BaseModel):
    """Payload enviado pelo usuário/operador para autorizar o merge."""
    proposal_id: str
    user_id: str = "operator"
    session_id: str
    nonce: str
    expected_diff_hash: str
    comments: Optional[str] = None


class ApprovalToken(BaseModel):
    """Token criptográfico emitido após verificação dos parâmetros de aprovação."""
    token_id: str
    proposal_id: str
    task_id: str
    worktree_id: Optional[str] = None
    base_commit: str
    head_commit: str
    diff_hash: str
    user_id: str
    nonce: str
    expires_at: float
    signature: str


class RejectRequest(BaseModel):
    """Requisição de descarte da proposta e recursos temporários associados."""
    proposal_id: str
    user_id: str = "operator"
    reason: str
    cleanup_worktree: bool = True


class RequestChangesRequest(BaseModel):
    """Requisição de feedback / correção enviada ao agente sem aplicar merge."""
    proposal_id: str
    user_id: str = "operator"
    feedback: str


class MergeResult(BaseModel):
    """Resultado determinístico da operação de merge no Git."""
    success: bool
    merged_commit: Optional[str] = None
    message: str
    target_branch: str
    error: Optional[str] = None
