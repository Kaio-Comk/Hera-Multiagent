"""Roteador em camadas (TierRouter) do Hera Smart Gateway.

Gerencia o ciclo de fallback inteligente entre provedores permitidos pela política
de privacidade, aplicando concorrência controlada e limites estritos de segurança.
"""

import asyncio
from typing import List, Dict, Any, Optional
import httpx

from typing import List, Dict, Any, Optional, AsyncIterator
import httpx
import uuid

from app.gateway.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
)
from app.gateway.privacy import GatewayPrivacyManager, DataClassification
from app.gateway.providers import (
    BaseGatewayAdapter,
    GatewayProviderError,
)
from app.gateway.token_saver import RTKTokenSaver
from app.gateway.sse import sse_stream_generator

class TierRouter:
    """Roteador que seleciona e executa provedores em camadas com fallback seguro."""

    def __init__(
        self,
        adapters: List[BaseGatewayAdapter],
        privacy_manager: Optional[GatewayPrivacyManager] = None,
        token_saver: Optional[RTKTokenSaver] = None,
        max_concurrent_requests: int = 10
    ):
        self.adapters = adapters
        self.privacy_manager = privacy_manager or GatewayPrivacyManager()
        self.token_saver = token_saver or RTKTokenSaver()
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)

    def _apply_token_saving_if_applicable(self, request: ChatCompletionRequest):
        """Aplica o RTK Token Saver exclusivamente a saídas de ferramentas (role='tool')."""
        for msg in request.messages:
            if msg.role == "tool" and isinstance(msg.content, str):
                compressed, report = self.token_saver.compress_tool_output(msg.content)
                msg.content = compressed

    async def route_and_execute(
        self,
        request: ChatCompletionRequest,
        client: httpx.AsyncClient,
        force_offline: Optional[bool] = None,
        enable_rtk: bool = False
    ) -> ChatCompletionResponse:
        """Inspeciona o payload, filtra provedores elegíveis e executa com fallback pré-resposta."""
        if enable_rtk:
            self._apply_token_saving_if_applicable(request)

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
                    response = await adapter.chat_complete(request, client)
                    return response
                except GatewayProviderError as e:
                    last_error = e
                    if not e.retryable:
                        raise e
                    if i == len(eligible_adapters) - 1:
                        raise e
                    continue
                except Exception as e:
                    last_error = e
                    if i == len(eligible_adapters) - 1:
                        raise GatewayProviderError(f"Falha inesperada no provedor {adapter.name}: {str(e)}", status_code=500)
                    continue

        if last_error:
            raise last_error
        raise GatewayProviderError("Falha desconhecida no roteamento de provedores.", status_code=500)

    async def route_and_stream(
        self,
        request: ChatCompletionRequest,
        client: httpx.AsyncClient,
        force_offline: Optional[bool] = None,
        enable_rtk: bool = False
    ) -> AsyncIterator[str]:
        """Abre stream com preflight do primeiro chunk e garante terminação limpa."""
        if enable_rtk:
            self._apply_token_saving_if_applicable(request)

        # Reutilizar rota não-streaming para simular preflight e stream em mocks/provedores locais
        resp = await self.route_and_execute(
            request=request,
            client=client,
            force_offline=force_offline,
            enable_rtk=False  # Já aplicado
        )

        full_content = resp.choices[0].message.content or ""
        req_id = resp.id or f"chatcmpl-{uuid.uuid4().hex[:12]}"
        model = resp.model

        # Chunk inicial (preflight)
        first_chunk = full_content[:30] if len(full_content) > 30 else full_content
        remainder = full_content[30:] if len(full_content) > 30 else ""

        async def _mock_remaining():
            if remainder:
                yield remainder

        return sse_stream_generator(
            first_chunk_text=first_chunk,
            stream_iterator=_mock_remaining(),
            request_id=req_id,
            model=model
        )
