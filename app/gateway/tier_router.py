"""Roteador em camadas (TierRouter) do Hera Smart Gateway.

Gerencia o ciclo de fallback inteligente entre provedores permitidos pela política
de privacidade, aplicando concorrência controlada e limites estritos de segurança.
"""

import asyncio
from typing import List, Dict, Any, Optional
import httpx

from app.gateway.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from app.gateway.privacy import GatewayPrivacyManager, DataClassification
from app.gateway.providers import (
    BaseGatewayAdapter,
    GatewayProviderError,
)

class TierRouter:
    """Roteador que seleciona e executa provedores em camadas com fallback seguro."""

    def __init__(
        self,
        adapters: List[BaseGatewayAdapter],
        privacy_manager: Optional[GatewayPrivacyManager] = None,
        max_concurrent_requests: int = 10
    ):
        self.adapters = adapters
        self.privacy_manager = privacy_manager or GatewayPrivacyManager()
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)

    async def route_and_execute(
        self,
        request: ChatCompletionRequest,
        client: httpx.AsyncClient,
        force_offline: Optional[bool] = None
    ) -> ChatCompletionResponse:
        """Inspeciona o payload, filtra provedores elegíveis e executa com fallback pré-resposta."""
        # 1. Inspeção centralizada de privacidade antes de qualquer socket
        privacy_result = self.privacy_manager.inspect_request(request, force_offline=force_offline)

        # 2. Filtrar provedores autorizados pela política de privacidade
        candidates_meta = [
            {"adapter": a, "tier": a.tier, "is_local": a.is_local, "name": a.name}
            for a in self.adapters
        ]
        eligible_meta = self.privacy_manager.filter_eligible_candidates(candidates_meta, privacy_result)
        eligible_adapters = [m["adapter"] for m in eligible_meta]

        if not eligible_adapters:
            if privacy_result.classification == DataClassification.LOCAL_ONLY:
                raise GatewayProviderError(
                    f"Execução bloqueada por DLP: payload classificado como LOCAL_ONLY ({privacy_result.rejection_reason}), "
                    "mas nenhum provedor local seguro está configurado ou ativo.",
                    status_code=403,
                    retryable=False
                )
            raise GatewayProviderError("Nenhum provedor disponível para atender a requisição.", status_code=503)

        # 3. Execução com controle de semáforo e fallback seguro
        last_error: Optional[Exception] = None

        async with self.semaphore:
            for i, adapter in enumerate(eligible_adapters):
                try:
                    # Tentar execução no candidato atual
                    response = await adapter.chat_complete(request, client)
                    return response
                except GatewayProviderError as e:
                    last_error = e
                    # Se o erro NÃO for retryable (ex: 400 Bad Request, 401 Credencial inválida definitiva), abortar
                    if not e.retryable:
                        raise e
                    # Se for o último candidato, não há mais para onde fazer fallback
                    if i == len(eligible_adapters) - 1:
                        raise e
                    # Caso contrário, continua o loop e tenta o próximo candidato permitido
                    continue
                except Exception as e:
                    last_error = e
                    if i == len(eligible_adapters) - 1:
                        raise GatewayProviderError(f"Falha inesperada no provedor {adapter.name}: {str(e)}", status_code=500)
                    continue

        if last_error:
            raise last_error
        raise GatewayProviderError("Falha desconhecida no roteamento de provedores.", status_code=500)
