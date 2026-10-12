import time
import uuid
import hmac
import json
from pathlib import Path
import hashlib
import secrets
from typing import Set, Dict, Optional
from app.hitl.schemas import DiffProposal, ApprovalToken, ApprovalRequest


class ApprovalSecurityError(RuntimeError):
    """Exceção base para violações de segurança no processo de aprovação."""
    pass


class TokenExpiredError(ApprovalSecurityError):
    """Lançada quando um token de aprovação ultrapassa o tempo limite (TTL)."""
    pass


class ReplayAttackError(ApprovalSecurityError):
    """Lançada quando um nonce já consumido é reutilizado."""
    pass


class UnauthorizedError(ApprovalSecurityError):
    """Lançada quando o autor da requisição difere do autorizado no token."""
    pass


class StaleCommitError(ApprovalSecurityError):
    """Lançada quando o commit de HEAD mudou após a aprovação."""
    pass


class DiffMutatedError(ApprovalSecurityError):
    """Lançada quando o diff do código foi alterado após a aprovação."""
    pass


class ApprovalEngine:
    """Motor de emissão, assinatura criptográfica e validação de tokens HITL."""

    MAX_ISSUED_NONCES = 1000   # teto: POST /api/hitl/nonces nao pode encher a memoria

    def __init__(
        self,
        secret_key: Optional[str] = None,
        token_ttl_seconds: float = 300.0,  # 5 minutos por padrão
        state_path: Optional[Path] = None,
    ):
        self.secret_key = (secret_key or secrets.token_hex(32)).encode("utf-8")
        self.token_ttl_seconds = token_ttl_seconds
        self._consumed_nonces: Set[str] = set()
        self._issued_nonces: Dict[str, float] = {}
        # Nonces consumidos sobrevivem a reinicios (antes um restart permitia reusar um nonce
        # ainda valido): nonce -> instante a partir do qual pode ser esquecido.
        self._state_path = Path(state_path) if state_path else None
        self._consumed_until: Dict[str, float] = {}
        self._load_state()

    def _load_state(self) -> None:
        if not self._state_path:
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            now = time.time()
            self._consumed_until = {n: float(t) for n, t in (data.get("consumed") or {}).items() if float(t) > now}
            self._consumed_nonces = set(self._consumed_until)
        except (OSError, ValueError, TypeError, AttributeError):
            self._consumed_until = {}

    def _save_state(self) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps({"consumed": self._consumed_until}), encoding="utf-8")
        except OSError:
            pass

    def _purge_issued(self) -> None:
        now = time.time()
        for n in [n for n, exp in self._issued_nonces.items() if exp < now]:
            del self._issued_nonces[n]

    def generate_nonce(self) -> str:
        """Gera um nonce de uso único criptograficamente seguro e rastreável."""
        self._purge_issued()
        while len(self._issued_nonces) >= self.MAX_ISSUED_NONCES:      # descarta o mais antigo
            del self._issued_nonces[min(self._issued_nonces, key=self._issued_nonces.get)]
        nonce = f"nonce-{uuid.uuid4().hex}"
        self._issued_nonces[nonce] = time.time() + self.token_ttl_seconds
        return nonce

    def _sign(self, payload_str: str) -> str:
        """Assina string canônica com HMAC-SHA256."""
        return hmac.new(self.secret_key, payload_str.encode("utf-8"), hashlib.sha256).hexdigest()

    def _canonical_payload(
        self,
        token_id: str,
        proposal_id: str,
        task_id: str,
        base_commit: str,
        head_commit: str,
        diff_hash: str,
        user_id: str,
        nonce: str,
        expires_at: float
    ) -> str:
        return f"{token_id}|{proposal_id}|{task_id}|{base_commit}|{head_commit}|{diff_hash}|{user_id}|{nonce}|{expires_at:.3f}"

    def create_approval_token(
        self,
        proposal: DiffProposal,
        user_id: str,
        nonce: str,
        custom_ttl: Optional[float] = None
    ) -> ApprovalToken:
        """Cria e assina um token de aprovação imutável para a proposta."""
        if nonce in self._consumed_nonces:
            raise ReplayAttackError(f"Nonce '{nonce}' já foi consumido anteriormente.")
        # O nonce precisa ter sido EMITIDO por este servidor e nao ter vencido (antes qualquer
        # texto servia como nonce).
        issued_exp = self._issued_nonces.get(nonce)
        if issued_exp is None or time.time() > issued_exp:
            raise ApprovalSecurityError("Nonce não emitido por este servidor, ou expirado.")

        now = time.time()
        ttl = custom_ttl if custom_ttl is not None else self.token_ttl_seconds
        expires_at = now + ttl

        token_id = f"tok-{uuid.uuid4().hex[:12]}"
        payload = self._canonical_payload(
            token_id=token_id,
            proposal_id=proposal.id,
            task_id=proposal.task_id,
            base_commit=proposal.base_commit,
            head_commit=proposal.head_commit,
            diff_hash=proposal.diff_hash,
            user_id=user_id,
            nonce=nonce,
            expires_at=expires_at,
        )
        signature = self._sign(payload)

        return ApprovalToken(
            token_id=token_id,
            proposal_id=proposal.id,
            task_id=proposal.task_id,
            worktree_id=proposal.worktree_id,
            base_commit=proposal.base_commit,
            head_commit=proposal.head_commit,
            diff_hash=proposal.diff_hash,
            user_id=user_id,
            nonce=nonce,
            expires_at=expires_at,
            signature=signature,
        )

    def validate_and_consume_token(
        self,
        token: ApprovalToken,
        current_head_commit: str,
        current_diff_hash: str,
        expected_user_id: Optional[str] = None
    ) -> bool:
        """Valida rigorosamente todas as invariantes de segurança antes de permitir o merge.
        
        1. Verifica assinatura criptográfica HMAC.
        2. Verifica prazo de validade (TTL).
        3. Verifica identidade do autor (se especificado).
        4. Verifica proteção contra Replay Attack de Nonce.
        5. Verifica integridade do HEAD commit.
        6. Verifica integridade do SHA-256 do Diff (qualquer mutação invalida).
        """
        # 1. Verificar assinatura
        expected_payload = self._canonical_payload(
            token_id=token.token_id,
            proposal_id=token.proposal_id,
            task_id=token.task_id,
            base_commit=token.base_commit,
            head_commit=token.head_commit,
            diff_hash=token.diff_hash,
            user_id=token.user_id,
            nonce=token.nonce,
            expires_at=token.expires_at,
        )
        expected_sig = self._sign(expected_payload)
        if not hmac.compare_digest(expected_sig, token.signature):
            raise ApprovalSecurityError("Assinatura criptográfica do token de aprovação inválida.")

        # 2. Verificar expiração
        if time.time() > token.expires_at:
            raise TokenExpiredError(
                f"Token de aprovação expirado em {token.expires_at} (atual: {time.time()}). Re-validação necessária."
            )

        # 3. Verificar autor
        if expected_user_id and token.user_id != expected_user_id:
            raise UnauthorizedError(
                f"Autor da requisição '{expected_user_id}' diverge do autorizado no token '{token.user_id}'."
            )

        # 4. Verificar Replay Attack
        if token.nonce in self._consumed_nonces:
            raise ReplayAttackError(
                f"Tentativa de Replay Attack detectada: o nonce '{token.nonce}' já foi consumido."
            )

        # 5. Verificar Head Commit
        if current_head_commit != token.head_commit:
            raise StaleCommitError(
                f"O commit da feature branch divergiu após a aprovação! "
                f"Esperado: {token.head_commit}, Atual: {current_head_commit}. Nova revisão exigida."
            )

        # 6. Verificar Diff Hash
        if current_diff_hash != token.diff_hash:
            raise DiffMutatedError(
                f"O diff de código sofreu mutação após a revisão humana! "
                f"Hash aprovado: {token.diff_hash}, Hash recalculado: {current_diff_hash}."
            )

        # Consome nonce (e persiste: um reinicio nao pode "devolver" o nonce)
        self._consumed_nonces.add(token.nonce)
        self._issued_nonces.pop(token.nonce, None)
        self._consumed_until[token.nonce] = time.time() + 2 * self.token_ttl_seconds
        self._save_state()
        return True
