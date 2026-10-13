"""Roteador em camadas (TierRouter) do Hera Smart Gateway.

Gerencia o ciclo de fallback inteligente entre provedores permitidos pela política
de privacidade, aplicando concorrência controlada e limites estritos de segurança.
"""

import asyncio
import logging
import time
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
from app.gateway.sse import format_openai_sse_chunk, format_sse_done, format_sse_error, sse_stream_generator
from app.gateway.streaming import StreamRecord, StreamTelemetry

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
        keepalive_seconds: float = 15.0,
        stream_idle_timeout_seconds: float = 60.0,
        stream_total_timeout_seconds: float = 900.0,
    ):
        self.keepalive_seconds = keepalive_seconds
        self.stream_idle_timeout_seconds = stream_idle_timeout_seconds
        self.stream_total_timeout_seconds = stream_total_timeout_seconds
        self.telemetry = StreamTelemetry()
        self.total_timeout_seconds = total_timeout_seconds
        # Ordem de tentativa (config `gateway.default_tier_order`): antes era a ordem da lista de adaptadores.
        self.tier_order = list(tier_order or ["cloud_premium", "cloud_cheap", "cloud_free", "local"])
        self.adapters = adapters
        # Combos: nome -> passos [{"provider": ..., "model": ...}] tentados em ordem (modelo `nome` no /v1).
        self.combos: Dict[str, List[Dict[str, str]]] = {}
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

    async def _try_combo(self, request: ChatCompletionRequest, steps: List[Dict[str, str]], attempt):
        """Tenta cada passo do combo, em ordem, ate um funcionar. Bloqueio de privacidade (403) vale para
        todos os passos e para na hora; qualquer outra falha passa ao proximo passo."""
        last: Optional[GatewayProviderError] = None
        for step in steps:
            step_request = request.model_copy(deep=True, update={"model": step["model"]})
            try:
                return await attempt(step_request, step["provider"])
            except GatewayProviderError as exc:
                if exc.status_code == 403:
                    raise
                last = exc
        raise last or GatewayProviderError("O combo nao tem passos.", status_code=503, retryable=False)

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

    def _select_adapters(self, request: ChatCompletionRequest, force_offline: Optional[bool],
                         enable_rtk: bool, only_provider: Optional[str] = None) -> List[BaseGatewayAdapter]:
        """Privacidade (DLP/offline) -> modelo -> ordem de camadas. Usado pelo caminho normal e pelo stream:
        a política de privacidade é SEMPRE a primeira barreira, antes de qualquer socket."""
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
        if only_provider:                 # passo de combo: o provedor ja foi escolhido; a privacidade valeu acima
            eligible_adapters = [a for a in eligible_adapters if a.name == only_provider]
            if not eligible_adapters:
                raise GatewayProviderError(
                    f"Provedor '{only_provider}' indisponivel para esta requisicao (desligado ou barrado pela privacidade).",
                    status_code=503, retryable=True)
            return eligible_adapters
        eligible_adapters = self._order_by_tier(self._adapters_for_model(eligible_adapters, request.model))
        if not eligible_adapters:
            raise GatewayProviderError(
                f"Nenhum provedor habilitado atende o modelo '{request.model}'.", status_code=404, retryable=False)
        return eligible_adapters

    async def _route_and_execute(
        self,
        request: ChatCompletionRequest,
        client: httpx.AsyncClient,
        force_offline: Optional[bool] = None,
        enable_rtk: bool = False,
        only_provider: Optional[str] = None,
    ) -> ChatCompletionResponse:
        """Inspeciona o payload, filtra provedores elegíveis e executa com fallback pré-resposta."""
        combo = None if only_provider else self.combos.get(request.model or "")
        if combo:
            return await self._try_combo(request, combo, lambda req, prov: self._route_and_execute(
                req, client, force_offline, enable_rtk, prov))
        eligible_adapters = self._select_adapters(request, force_offline, enable_rtk, only_provider)

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
        enable_rtk: bool = False,
        only_provider: Optional[str] = None,
    ) -> AsyncIterator[str]:
        """Streaming de verdade. PRÉ-VOO: abre o stream no primeiro provedor elegível e lê até o primeiro
        dado útil; falha aqui faz fallback (ou vira erro HTTP). Depois do primeiro byte NÃO há fallback."""
        combo = None if only_provider else self.combos.get(request.model or "")
        if combo:
            return await self._try_combo(request, combo, lambda req, prov: self.route_and_stream(
                req, client, force_offline, enable_rtk, prov))
        eligible = self._select_adapters(request, force_offline, enable_rtk, only_provider)
        await self.semaphore.acquire()
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self.semaphore.release()

        rec = StreamRecord(request_id=f"req-{uuid.uuid4().hex[:12]}")
        try:
            handle = None
            last_error: Optional[Exception] = None
            for i, adapter in enumerate(eligible):
                rec.attempts = i + 1
                try:
                    handle = await asyncio.wait_for(adapter.open_stream(request, client),
                                                    timeout=self.total_timeout_seconds)
                    break
                except asyncio.TimeoutError:
                    last_error = GatewayProviderError(
                        f"Provedor {adapter.name} não abriu o stream em {self.total_timeout_seconds:.0f}s",
                        status_code=504, retryable=True)
                except GatewayProviderError as e:
                    last_error = e
                    if not e.retryable:
                        raise
                except Exception as e:                        # noqa: BLE001 - qualquer falha pré-resposta
                    last_error = GatewayProviderError(
                        f"Falha inesperada no provedor {adapter.name}: {type(e).__name__}", status_code=500)
            if handle is None:
                raise last_error or GatewayProviderError("Nenhum provedor abriu o stream.", status_code=503)
        except BaseException:
            release()
            raise

        rec.provider, rec.model, rec.native = handle.provider, handle.model, handle.native
        return self._stream_events(handle, rec, release)

    async def _stream_events(self, handle, rec: StreamRecord, release) -> AsyncIterator[str]:
        """Gerador SSE. Cancelamento (cliente desconectou) fecha a conexão com o provedor."""
        pending = None
        try:
            yield format_openai_sse_chunk(request_id=handle.request_id, model=handle.model, role="assistant")
            queue = [handle.first] if handle.first is not None else []
            finish = None
            idle = 0.0
            started = time.monotonic()
            while True:
                if queue:
                    delta = queue.pop(0)
                else:
                    if pending is None:
                        pending = asyncio.ensure_future(handle.rest.__anext__())
                    ready, _ = await asyncio.wait({pending}, timeout=self.keepalive_seconds)
                    if not ready:
                        idle += self.keepalive_seconds
                        if idle >= self.stream_idle_timeout_seconds:
                            raise GatewayProviderError(
                                f"Provedor {handle.provider} sem enviar dados por {idle:.0f}s", status_code=504)
                        if time.monotonic() - started > self.stream_total_timeout_seconds:
                            raise GatewayProviderError("Stream excedeu o prazo máximo", status_code=504)
                        yield ": keep-alive\n\n"
                        continue
                    idle = 0.0
                    try:
                        delta = pending.result()
                    except StopAsyncIteration:
                        pending = None
                        break
                    pending = None
                if time.monotonic() - started > self.stream_total_timeout_seconds:
                    raise GatewayProviderError("Stream excedeu o prazo máximo", status_code=504)
                if delta.usage_input_tokens is not None:
                    rec.prompt_tokens = delta.usage_input_tokens
                if delta.usage_output_tokens is not None:
                    rec.completion_tokens = delta.usage_output_tokens
                if delta.usage_cached_tokens is not None:
                    rec.cached_tokens = delta.usage_cached_tokens
                if delta.content:
                    if rec.ttfb_ms is None:
                        rec.ttfb_ms = int((time.monotonic() - rec.started) * 1000)
                    rec.chunks += 1
                    rec.chars += len(delta.content)
                    yield format_openai_sse_chunk(request_id=handle.request_id, model=handle.model, content=delta.content)
                elif delta.tool_call_index is not None:
                    if rec.ttfb_ms is None:
                        rec.ttfb_ms = int((time.monotonic() - rec.started) * 1000)
                    rec.chunks += 1
                    fn = {k: v for k, v in (("name", delta.tool_name_fragment),
                                            ("arguments", delta.tool_arguments_fragment)) if v is not None}
                    call = {"index": delta.tool_call_index, "type": "function", "function": fn}
                    if delta.tool_call_id:
                        call["id"] = delta.tool_call_id
                    yield format_openai_sse_chunk(request_id=handle.request_id, model=handle.model, tool_calls=[call])
                if delta.finish_reason:
                    finish = delta.finish_reason
            rec.finish_reason = finish or "stop"
            yield format_openai_sse_chunk(request_id=handle.request_id, model=handle.model, finish_reason=rec.finish_reason)
            yield format_sse_done()
            rec.outcome = "completed"
        except asyncio.CancelledError:
            rec.outcome = "cancelled"
            raise
        except GeneratorExit:
            rec.outcome = "cancelled"
            raise
        except Exception as e:                                # noqa: BLE001
            # Regra de ouro v3: falha DEPOIS do primeiro byte não faz fallback nem costura outro modelo.
            rec.outcome, rec.error = "error", f"{type(e).__name__}: {e}"[:200]
            code = e.status_code if isinstance(e, GatewayProviderError) else 502
            yield format_sse_error(f"Interrupção na geração upstream: {type(e).__name__}", code=code)
            yield format_sse_done()
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
            try:
                await handle.aclose()
            except Exception:                                 # noqa: BLE001 - fechar nunca derruba o servidor
                pass
            rec.total_ms = int((time.monotonic() - rec.started) * 1000)
            self.telemetry.add(rec)
            logging.getLogger("hera.gateway").info(
                "[stream] %s provider=%s model=%s outcome=%s ttfb_ms=%s total_ms=%s chunks=%s fallbacks=%s native=%s",
                rec.request_id, rec.provider, rec.model, rec.outcome, rec.ttfb_ms, rec.total_ms, rec.chunks,
                rec.attempts - 1, rec.native)
            release()
