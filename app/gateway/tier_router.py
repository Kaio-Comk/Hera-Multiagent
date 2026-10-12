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
        max_concurrent_requests: int = 10,
        total_timeout_seconds: float = 180.0,
        tier_order: Optional[List[str]] = None,
    ):
        self.total_timeout_seconds = total_timeout_seconds
        # Ordem de tentativa (config `gateway.default_tier_order`): antes era a ordem da lista de adaptadores.
        self.tier_order = list(tier_order or ["cloud_premium", "cloud_cheap", "cloud_free", "local"])
        self.adapters = adapters
        self.privacy_manager = privacy_manager or GatewayPrivacyManager()
        self.token_saver = token_saver or RTKTokenSaver()
        self.semaphore = asyncio.Semaphore(max_concurrent_requests)

    def _claimed_models(self) -> set:
        claimed: set = set()
        for a in self.adapters:
            if a.served_models:
                claimed |= set(a.served_models)
        return claimed

    def _adapters_for_model(self, adapters: List[BaseGatewayAdapter], model: Optional[str]) -> List[BaseGatewayAdapter]:
        """`default`: todos. Modelo específico: provedores de nuvem que o declaram; o local atende o que
        nenhuma nuvem reivindica (assim `qwen2.5-coder` vai ao Ollama e `deepseek-chat` não)."""
        if not model or model == "default":
            return list(adapters)
        claimed = self._claimed_models()
        out = []
        for a in adapters:
            if a.served_models:
                if model in a.served_models:
                    out.append(a)
            elif model not in claimed:
                out.append(a)
        return out

    def _order_by_tier(self, adapters: List[BaseGatewayAdapter]) -> List[BaseGatewayAdapter]:
        rank = {t: i for i, t in enumerate(self.tier_order)}
        return sorted(adapters, key=lambda a: rank.get(a.tier, len(rank)))

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
        """Executa com PRAZO TOTAL: tentativas e fallbacks somados não passam de `total_timeout_seconds`
        (antes cada tentativa tinha o seu timeout e N provedores somavam N vezes)."""
        try:
            return await asyncio.wait_for(
                self._route_and_execute(request, client, force_offline, enable_rtk),
                timeout=self.total_timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise GatewayProviderError(
                f"Requisição excedeu o prazo total de {self.total_timeout_seconds:.0f}s.",
                status_code=504, retryable=False)

    async def _route_and_execute(
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

        # 2b. Só os provedores que atendem o MODELO pedido (a privacidade já foi aplicada acima),
        #     na ordem de camadas configurada.
        eligible_adapters = self._order_by_tier(self._adapters_for_model(eligible_adapters, request.model))
        if not eligible_adapters:
            raise GatewayProviderError(
                f"Nenhum provedor habilitado atende o modelo '{request.model}'.", status_code=404, retryable=False)

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
