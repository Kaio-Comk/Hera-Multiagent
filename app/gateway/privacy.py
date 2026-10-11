"""Classificação de privacidade e Pre-flight Data Loss Prevention (DLP) para o Gateway.

Centraliza a inspeção do payload completo antes de qualquer conexão upstream.
Garante que tarefas com segredos ou marcadas como offline sejam restritas
exclusivamente à execução local, bloqueando 100% das saídas para a nuvem.
"""

from enum import Enum
from typing import List, Dict, Any, Optional, Set
from dataclasses import dataclass

from app.privacy_guard import PrivacyGuard
from app.gateway.schemas import ChatCompletionRequest

class DataClassification(str, Enum):
    PUBLIC = "public"
    INTERNAL = "internal"
    LOCAL_ONLY = "local_only"

PrivacyClass = DataClassification

@dataclass(frozen=True)
class PrivacyInspectionResult:
    classification: DataClassification
    has_secrets: bool
    secret_types: List[str]
    allowed_tiers: Set[str]  # e.g. {"local"} ou {"cloud_premium", "cloud_cheap", "cloud_free", "local"}
    rejection_reason: Optional[str] = None

class GatewayPrivacyManager:
    """Controlador central de privacidade para roteamento de requisições do Gateway."""

    def __init__(self, default_offline: bool = False):
        self.default_offline = default_offline

    def inspect_request(
        self,
        request: ChatCompletionRequest,
        force_offline: Optional[bool] = None
    ) -> PrivacyInspectionResult:
        """Inspeciona o payload completo da requisição antes de qualquer chamada externa.
        
        Args:
            request: A requisição normalizada contendo todas as mensagens e ferramentas.
            force_offline: Flag opcional que força modo 100% offline para esta requisição.
            
        Returns:
            PrivacyInspectionResult com a classificação e os tiers autorizados.
        """
        is_offline = self.default_offline if force_offline is None else force_offline

        full_text = request.get_full_text_for_dlp()
        has_secrets, secret_types = PrivacyGuard.scan_content(full_text)

        # Regra de Ouro: Segredo detectado ou modo offline -> LOCAL_ONLY absoluto
        if has_secrets:
            return PrivacyInspectionResult(
                classification=DataClassification.LOCAL_ONLY,
                has_secrets=True,
                secret_types=secret_types,
                allowed_tiers={"local"},
                rejection_reason=f"Segredos/credenciais detectados no payload: {', '.join(secret_types)}"
            )

        if is_offline:
            return PrivacyInspectionResult(
                classification=DataClassification.LOCAL_ONLY,
                has_secrets=False,
                secret_types=[],
                allowed_tiers={"local"},
                rejection_reason="Modo 100% offline ativado pelo usuário ou configuração"
            )

        # Sem segredos e modo online permitido
        return PrivacyInspectionResult(
            classification=DataClassification.PUBLIC,
            has_secrets=False,
            secret_types=[],
            allowed_tiers={"cloud_premium", "cloud_cheap", "cloud_free", "local"}
        )

    def filter_eligible_candidates(
        self,
        candidates: List[Dict[str, Any]],
        privacy_result: PrivacyInspectionResult
    ) -> List[Dict[str, Any]]:
        """Filtra a lista de candidatos com base rigorosa na classificação de privacidade.
        
        Se a classificação for LOCAL_ONLY, remove sumariamente qualquer candidato
        que não seja estritamente local (ex: Ollama).
        """
        if privacy_result.classification == DataClassification.LOCAL_ONLY:
            # Retém estritamente candidatos locais
            eligible = [c for c in candidates if c.get("is_local", False) is True]
            return eligible

        # Se for público, todos os candidatos que pertencem aos tiers permitidos são elegíveis
        return [
            c for c in candidates
            if c.get("tier", "local") in privacy_result.allowed_tiers
        ]

    def classify_prompt(
        self,
        text: str,
        force_offline: Optional[bool] = None
    ) -> PrivacyInspectionResult:
        """Classifica o texto isolado de um prompt com relação a credenciais e privacidade."""
        is_offline = self.default_offline if force_offline is None else force_offline
        has_secrets, secret_types = PrivacyGuard.scan_content(text)

        if has_secrets:
            return PrivacyInspectionResult(
                classification=DataClassification.LOCAL_ONLY,
                has_secrets=True,
                secret_types=secret_types,
                allowed_tiers={"local"},
                rejection_reason=f"Segredos/credenciais detectados no prompt: {', '.join(secret_types)}"
            )

        if is_offline:
            return PrivacyInspectionResult(
                classification=DataClassification.LOCAL_ONLY,
                has_secrets=False,
                secret_types=[],
                allowed_tiers={"local"},
                rejection_reason="Modo 100% offline ativado"
            )

        return PrivacyInspectionResult(
            classification=DataClassification.PUBLIC,
            has_secrets=False,
            secret_types=[],
            allowed_tiers={"cloud_premium", "cloud_cheap", "cloud_free", "local"}
        )
