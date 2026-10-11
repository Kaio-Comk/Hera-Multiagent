import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from app.hitl.schemas import (
    DiffProposal,
    DiffStats,
    DiffAlert,
    ApprovalRequest,
    ApprovalToken,
    RejectRequest,
    RequestChangesRequest,
    MergeResult,
)
from app.hitl.git_manager import GitManager, GitSecurityError, DirtyWorkingTreeError, MergeConflictError
from app.hitl.diff_analyzer import DiffAnalyzer
from app.hitl.approval_engine import ApprovalEngine, ApprovalSecurityError


class HitlService:
    """Serviço central de coordenação do Human-in-the-Loop e Visual Diff Review."""

    def __init__(
        self,
        git_manager: Optional[GitManager] = None,
        diff_analyzer: Optional[DiffAnalyzer] = None,
        approval_engine: Optional[ApprovalEngine] = None,
        state_store: Optional[any] = None,
        obsidian_writer: Optional[any] = None,
    ):
        self.git_manager = git_manager or GitManager()
        self.diff_analyzer = diff_analyzer or DiffAnalyzer()
        self.approval_engine = approval_engine or ApprovalEngine()
        self.state_store = state_store
        self.obsidian_writer = obsidian_writer
        self._proposals: Dict[str, DiffProposal] = {}

    def create_proposal(
        self,
        task_id: str,
        repo_path: str,
        feature_branch: str,
        target_branch: str = "master",
        worktree_id: Optional[str] = None,
        worktree_path: Optional[str] = None,
        allowed_paths: Optional[List[str]] = None,
    ) -> DiffProposal:
        """Cria e registra uma nova proposta formal de alteração sob inspeção do HITL."""
        base_commit = self.git_manager.get_current_commit(repo_path, ref=target_branch)
        head_commit = self.git_manager.get_current_commit(repo_path, ref=feature_branch)

        diff_content = self.git_manager.get_diff(repo_path, base_ref=target_branch, head_ref=feature_branch)
        diff_hash = self.git_manager.compute_diff_hash(diff_content)

        stats = self.diff_analyzer.calculate_stats(diff_content)
        alerts = self.diff_analyzer.analyze_alerts(diff_content, allowed_paths=allowed_paths)

        proposal_id = f"prop-{uuid.uuid4().hex[:12]}"
        proposal = DiffProposal(
            id=proposal_id,
            task_id=task_id,
            repo_path=str(Path(repo_path).resolve()),
            worktree_id=worktree_id,
            worktree_path=worktree_path,
            target_branch=target_branch,
            feature_branch=feature_branch,
            base_commit=base_commit,
            head_commit=head_commit,
            diff_content=diff_content,
            diff_hash=diff_hash,
            stats=stats,
            alerts=alerts,
            status="pending_review",
            created_at=time.time(),
        )

        self._proposals[proposal_id] = proposal

        # Registrar no StateStore se disponível
        if self.state_store:
            try:
                self.state_store.record_event(
                    task_id=task_id,
                    event_type="HITL_PROPOSAL_CREATED",
                    message=f"Proposta {proposal_id} criada para branch {feature_branch} (diff hash {diff_hash[:8]}).",
                    payload={
                        "proposal_id": proposal_id,
                        "diff_hash": diff_hash,
                        "files_changed": stats.files_changed,
                        "alerts_count": len(alerts),
                    }
                )
            except Exception:
                pass

        return proposal

    def get_proposal(self, proposal_id: str) -> Optional[DiffProposal]:
        return self._proposals.get(proposal_id)

    def list_proposals(self, status: Optional[str] = None) -> List[DiffProposal]:
        props = list(self._proposals.values())
        if status:
            props = [p for p in props if p.status == status]
        return sorted(props, key=lambda x: x.created_at, reverse=True)

    def approve_and_merge(self, request: ApprovalRequest) -> MergeResult:
        """Processa a aprovação com verificação rigorosa de hashes e executa o merge atômico."""
        proposal = self._proposals.get(request.proposal_id)
        if not proposal:
            return MergeResult(
                success=False,
                message=f"Proposta '{request.proposal_id}' não encontrada.",
                target_branch="desconhecida",
                error="PROPOSAL_NOT_FOUND"
            )

        if proposal.status != "pending_review":
            return MergeResult(
                success=False,
                message=f"Proposta está em status '{proposal.status}' e não pode ser aprovada.",
                target_branch=proposal.target_branch,
                error="INVALID_STATUS"
            )

        # 1. Conferir hash esperado pelo cliente
        if request.expected_diff_hash != proposal.diff_hash:
            return MergeResult(
                success=False,
                message="Hash esperado do diff diverge do hash da proposta registrada.",
                target_branch=proposal.target_branch,
                error="DIFF_HASH_MISMATCH"
            )

        # 2. Emitir token de aprovação assinado
        token = self.approval_engine.create_approval_token(
            proposal=proposal,
            user_id=request.user_id,
            nonce=request.nonce,
        )

        # 3. Recomputar head_commit e diff em tempo real do Git
        try:
            current_head = self.git_manager.get_current_commit(proposal.repo_path, ref=proposal.feature_branch)
            current_diff = self.git_manager.get_diff(
                proposal.repo_path,
                base_ref=proposal.target_branch,
                head_ref=proposal.feature_branch
            )
            current_diff_hash = self.git_manager.compute_diff_hash(current_diff)
        except Exception as e:
            return MergeResult(
                success=False,
                message=f"Falha ao inspecionar estado atual do Git: {e}",
                target_branch=proposal.target_branch,
                error="GIT_INSPECTION_FAILED"
            )

        # 4. Validar token e invariantes (replay, mutação de diff, staling)
        try:
            self.approval_engine.validate_and_consume_token(
                token=token,
                current_head_commit=current_head,
                current_diff_hash=current_diff_hash,
                expected_user_id=request.user_id
            )
        except ApprovalSecurityError as e:
            return MergeResult(
                success=False,
                message=f"Validação de segurança da aprovação falhou: {e}",
                target_branch=proposal.target_branch,
                error=type(e).__name__
            )

        # 5. Executar merge
        try:
            merged_commit = self.git_manager.perform_merge(
                repo_path=proposal.repo_path,
                target_branch=proposal.target_branch,
                feature_branch=proposal.feature_branch,
                commit_message=f"merge(hitl): aprovação de {proposal.feature_branch} por {request.user_id} (hash {proposal.diff_hash[:8]})"
            )
        except (DirtyWorkingTreeError, MergeConflictError, RuntimeError) as e:
            return MergeResult(
                success=False,
                message=f"Falha ao executar merge no Git: {e}",
                target_branch=proposal.target_branch,
                error=type(e).__name__
            )

        # 6. Atualizar status da proposta
        proposal.status = "merged"
        proposal.reviewed_at = time.time()
        proposal.reviewer_id = request.user_id

        # 7. Limpar worktree se houver
        if proposal.worktree_id and self.git_manager.registry.is_registered(proposal.worktree_id):
            try:
                self.git_manager.remove_worktree(proposal.repo_path, proposal.worktree_id)
            except Exception:
                pass

        # 8. Registrar eventos no StateStore
        if self.state_store:
            try:
                self.state_store.record_event(
                    task_id=proposal.task_id,
                    event_type="HITL_APPROVED",
                    message=f"Proposta {proposal.id} aprovada pelo operador {request.user_id}.",
                    payload={"diff_hash": proposal.diff_hash, "comments": request.comments}
                )
                self.state_store.record_event(
                    task_id=proposal.task_id,
                    event_type="HITL_MERGED",
                    message=f"Branch {proposal.feature_branch} integrada à {proposal.target_branch} no commit {merged_commit[:8]}.",
                    payload={"merged_commit": merged_commit, "target_branch": proposal.target_branch}
                )
            except Exception:
                pass

        # 9. Notificar Obsidian Vault se configurado
        if self.obsidian_writer:
            try:
                self.obsidian_writer.record_hitl_merge(
                    task_id=proposal.task_id,
                    proposal_id=proposal.id,
                    merged_commit=merged_commit,
                    target_branch=proposal.target_branch,
                    feature_branch=proposal.feature_branch,
                    diff_hash=proposal.diff_hash,
                    reviewer=request.user_id,
                )
            except Exception:
                pass

        return MergeResult(
            success=True,
            merged_commit=merged_commit,
            message="Merge concluído e verificado com sucesso.",
            target_branch=proposal.target_branch,
        )

    def reject_proposal(self, request: RejectRequest) -> bool:
        """Rejeita a proposta e descarta worktrees/branches exclusivas do HERA de forma segura."""
        proposal = self._proposals.get(request.proposal_id)
        if not proposal:
            return False

        proposal.status = "rejected"
        proposal.reviewed_at = time.time()
        proposal.reviewer_id = request.user_id

        if request.cleanup_worktree and proposal.worktree_id:
            if self.git_manager.registry.is_registered(proposal.worktree_id):
                try:
                    self.git_manager.remove_worktree(proposal.repo_path, proposal.worktree_id, force=True)
                except Exception:
                    pass

        if self.state_store:
            try:
                self.state_store.record_event(
                    task_id=proposal.task_id,
                    event_type="HITL_REJECTED",
                    message=f"Proposta {proposal.id} rejeitada pelo operador {request.user_id}. Motivo: {request.reason}",
                    payload={"reason": request.reason}
                )
            except Exception:
                pass

        return True

    def request_changes(self, request: RequestChangesRequest) -> bool:
        """Registra feedback de correção sem modificar o repositório principal."""
        proposal = self._proposals.get(request.proposal_id)
        if not proposal:
            return False

        proposal.status = "changes_requested"
        proposal.reviewed_at = time.time()
        proposal.reviewer_id = request.user_id

        if self.state_store:
            try:
                self.state_store.record_event(
                    task_id=proposal.task_id,
                    event_type="HITL_CHANGES_REQUESTED",
                    message=f"Solicitação de alterações para {proposal.id}: {request.feedback}",
                    payload={"feedback": request.feedback}
                )
            except Exception:
                pass

        return True
