"""Roteador em camadas (TierRouter) do Hera Smart Gateway.

Gerencia o ciclo de fallback inteligente entre provedores permitidos pela política
de privacidade, aplicando concorrência controlada e limites estritos de segurança.
"""

import asyncio
import contextvars
import logging
import random
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
from app.gateway.resilience import ProviderGuard
from app.privacy_guard import PrivacyGuard

# Quem esta chamando (chave de acesso de cliente) e o id da requisicao: preenchidos pelo endpoint /v1.
CLIENT_NAME: contextvars.ContextVar = contextvars.ContextVar("hera_client", default=None)
REQUEST_ID: contextvars.ContextVar = contextvars.ContextVar("hera_request_id", default=None)
STRATEGIES = ("priority", "round_robin", "fastest", "least_used", "weighted")

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
        self.combo_strategies: Dict[str, str] = {}       # nome do combo -> priority | round_robin | fastest | least_used | weighted
        self.aliases: Dict[str, str] = {}                # apelido -> combo, provedor/modelo ou modelo
        self.priorities: Dict[str, int] = {}             # provedor -> prioridade dentro da camada (menor primeiro)
        self.pricing: Dict[str, Dict[str, float]] = {}   # "provedor" ou "provedor/modelo" -> {"in": US$/1M, "out": US$/1M}
        self.guard = ProviderGuard()                     # disjuntor + limites por provedor
        self._rr: Dict[str, int] = {}
        self.rtk_stats = {"calls": 0, "saved_chars": 0}
        self.usage = None            # UsageLedger opcional: contagem de chamadas/tokens (sem conteudo)
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
        ordered = sorted(adapters, key=lambda a: (rank.get(a.tier, len(rank)), self.priorities.get(a.name, 100)))
        # provedor em pausa (disjuntor aberto) fica por ULTIMO: so e tentado se os outros falharem
        return [a for a in ordered if not self.guard.is_paused(a.name)] + [a for a in ordered if self.guard.is_paused(a.name)]

    def _today(self, provider: str) -> Dict[str, int]:
        return self.usage.today(provider=provider) if self.usage is not None else {"requests": 0, "tokens": 0}

    def _blocked(self, adapter_name: str) -> Optional[GatewayProviderError]:
        """Estourou o limite configurado do provedor? Devolve o erro (429, retentavel) ou None."""
        reason = self.guard.over_limit(adapter_name, self._today)
        if reason:
            return GatewayProviderError(f"Provedor {adapter_name} em {reason}.", status_code=429, retryable=True)
        return None

    def _apply_alias(self, request: ChatCompletionRequest) -> ChatCompletionRequest:
        target = self.aliases.get(request.model or "")
        return request.model_copy(update={"model": target}) if target else request

    def price(self, adapter_name: str, model: str, prompt_tokens: Optional[int], completion_tokens: Optional[int]) -> Optional[float]:
        """Custo estimado em US$ (preco que VOCE cadastrou). Local e camada gratis custam 0; sem preco: None."""
        entry = self.pricing.get(f"{adapter_name}/{model}") or self.pricing.get(adapter_name)
        if entry:
            return round(((prompt_tokens or 0) * float(entry.get("in", 0)) + (completion_tokens or 0) * float(entry.get("out", 0))) / 1_000_000, 6)
        adapter = next((a for a in self.adapters if a.name == adapter_name), None)
        if adapter is not None and (adapter.is_local or adapter.tier == "cloud_free"):
            return 0.0
        return None

    def _order_combo(self, name: str, steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Ordem em que os passos do combo serao tentados, conforme a estrategia."""
        strategy = self.combo_strategies.get(name, "priority")
        ordered = list(steps)
        if strategy == "round_robin" and ordered:
            start = self._rr.get(name, 0) % len(ordered)
            self._rr[name] = start + 1
            ordered = ordered[start:] + ordered[:start]
        elif strategy == "fastest" and self.usage is not None:
            stats = self.usage.latency_stats()
            ordered.sort(key=lambda st: stats.get(st["provider"], {}).get("avg_ms", 10 ** 9))
        elif strategy == "least_used" and self.usage is not None:
            ordered.sort(key=lambda st: self._today(st["provider"])["requests"])
        elif strategy == "weighted" and len(ordered) > 1:
            weights = [max(1, int(st.get("weight") or 1)) for st in ordered]
            first = random.choices(range(len(ordered)), weights=weights)[0]  # nosec B311 - balanceamento, nao e segredo
            ordered = [ordered[first]] + ordered[:first] + ordered[first + 1:]
        paused = [st for st in ordered if self.guard.is_paused(st["provider"])]
        return [st for st in ordered if st not in paused] + paused

    def _resolve_address(self, request: ChatCompletionRequest):
        """`provedor/modelo` (ex.: `groq/llama-3.3-70b-versatile`) fala direto com aquele provedor. So vale se o nome
        inteiro nao for um combo nem um modelo declarado (OpenRouter tem modelos com `/` no proprio nome)."""
        model = request.model or ""
        if "/" not in model or model in self.combos or model in self._claimed_models():
            return None
        prefix, rest = model.split("/", 1)
        if rest and any(a.name == prefix for a in self.adapters):
            return prefix, rest
        return None

    def _record(self, adapter_name: str, model: str, ok: bool, status: Optional[int], usage, started: float,
                stream: bool = False, error: Optional[str] = None, requested: Optional[str] = None,
                latency_ms: Optional[int] = None, prompt_tokens: Optional[int] = None,
                completion_tokens: Optional[int] = None) -> None:
        """Registra o uso (so metadados) e alimenta o disjuntor do provedor."""
        pt = prompt_tokens if prompt_tokens is not None else getattr(usage, "prompt_tokens", None)
        ct = completion_tokens if completion_tokens is not None else getattr(usage, "completion_tokens", None)
        if ok:
            self.guard.report_success(adapter_name)
        else:
            self.guard.report_failure(adapter_name, error or "", counts=(status is None or status >= 500 or status == 408))
        if self.usage is not None:
            latency = latency_ms if latency_ms is not None else int((time.monotonic() - started) * 1000)
            safe_error = PrivacyGuard.sanitize_content(error)[:240] if error else None
            self.usage.record(adapter_name, model, ok, status, pt, ct, latency, stream, client=CLIENT_NAME.get(),
                              requested_model=requested or model, request_id=REQUEST_ID.get(), error=safe_error,
                              cost=self.price(adapter_name, model, pt, ct) if ok else None)

    async def _try_combo(self, request: ChatCompletionRequest, steps: List[Dict[str, str]], attempt):
        """Tenta cada passo do combo, em ordem, ate um funcionar. Bloqueio de privacidade (403) vale para
        todos os passos e para na hora; qualquer outra falha passa ao proximo passo."""
        last: Optional[GatewayProviderError] = None
        for step in self._order_combo(request.model or "", steps):
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
                if len(compressed) < len(msg.content):
                    self.rtk_stats["calls"] += 1
                    self.rtk_stats["saved_chars"] += len(msg.content) - len(compressed)
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
        token = REQUEST_ID.set(REQUEST_ID.get() or f"req-{uuid.uuid4().hex[:10]}")
        try:
            return await asyncio.wait_for(
                self._route_and_execute(request, client, force_offline, enable_rtk),
                timeout=self.total_timeout_seconds,
            )
        except asyncio.TimeoutError:
            raise GatewayProviderError(
                f"Requisição excedeu o prazo total de {self.total_timeout_seconds:.0f}s.",
                status_code=504, retryable=False)
        finally:
            REQUEST_ID.reset(token)

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
        if not only_provider:
            request = self._apply_alias(request)
        combo = None if only_provider else self.combos.get(request.model or "")
        if combo:
            return await self._try_combo(request, combo, lambda req, prov: self._route_and_execute(
                req, client, force_offline, enable_rtk, prov))
        address = None if only_provider else self._resolve_address(request)
        if address:
            return await self._route_and_execute(request.model_copy(update={"model": address[1]}), client,
                                                 force_offline, enable_rtk, address[0])
        eligible_adapters = self._select_adapters(request, force_offline, enable_rtk, only_provider)

        # 3. Execução com controle de semáforo e fallback seguro
        last_error: Optional[Exception] = None

        async with self.semaphore:
            for i, adapter in enumerate(eligible_adapters):
                blocked = self._blocked(adapter.name)
                if blocked is not None:                      # limite do provedor estourado: pula (sem gastar chamada)
                    last_error = blocked
                    if i == len(eligible_adapters) - 1:
                        raise blocked
                    continue
                started = time.monotonic()
                try:
                    response = await adapter.chat_complete(request, client)
                    self._record(adapter.name, response.model or request.model, True, 200, response.usage, started,
                                 requested=request.model)
                    return response
                except GatewayProviderError as e:
                    self._record(adapter.name, request.model, False, e.status_code, None, started, error=str(e))
                    last_error = e
                    if not e.retryable:
                        raise e
                    if i == len(eligible_adapters) - 1:
                        raise e
                    continue
                except Exception as e:
                    self._record(adapter.name, request.model, False, 500, None, started, error=f"{type(e).__name__}")
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
        if not only_provider:
            request = self._apply_alias(request)
        combo = None if only_provider else self.combos.get(request.model or "")
        if combo:
            return await self._try_combo(request, combo, lambda req, prov: self.route_and_stream(
                req, client, force_offline, enable_rtk, prov))
        address = None if only_provider else self._resolve_address(request)
        if address:
            return await self.route_and_stream(request.model_copy(update={"model": address[1]}), client,
                                               force_offline, enable_rtk, address[0])
        eligible = self._select_adapters(request, force_offline, enable_rtk, only_provider)
        await self.semaphore.acquire()
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self.semaphore.release()

        rec = StreamRecord(request_id=REQUEST_ID.get() or f"req-{uuid.uuid4().hex[:12]}")
        ctx = {"client": CLIENT_NAME.get(), "rid": rec.request_id, "requested": request.model}
        try:
            handle = None
            last_error: Optional[Exception] = None
            for i, adapter in enumerate(eligible):
                rec.attempts = i + 1
                blocked = self._blocked(adapter.name)
                if blocked is not None:
                    last_error = blocked
                    continue
                opened = time.monotonic()
                try:
                    handle = await asyncio.wait_for(adapter.open_stream(request, client),
                                                    timeout=self.total_timeout_seconds)
                    break
                except asyncio.TimeoutError:
                    last_error = GatewayProviderError(
                        f"Provedor {adapter.name} não abriu o stream em {self.total_timeout_seconds:.0f}s",
                        status_code=504, retryable=True)
                    self._record(adapter.name, request.model, False, 504, None, opened, stream=True, error=str(last_error))
                except GatewayProviderError as e:
                    last_error = e
                    self._record(adapter.name, request.model, False, e.status_code, None, opened, stream=True, error=str(e))
                    if not e.retryable:
                        raise
                except Exception as e:                        # noqa: BLE001 - qualquer falha pré-resposta
                    last_error = GatewayProviderError(
                        f"Falha inesperada no provedor {adapter.name}: {type(e).__name__}", status_code=500)
                    self._record(adapter.name, request.model, False, 500, None, opened, stream=True, error=type(e).__name__)
            if handle is None:
                raise last_error or GatewayProviderError("Nenhum provedor abriu o stream.", status_code=503)
        except BaseException:
            release()
            raise

        rec.provider, rec.model, rec.native = handle.provider, handle.model, handle.native
        return self._stream_events(handle, rec, release, ctx)

    async def _stream_events(self, handle, rec: StreamRecord, release, ctx: Optional[Dict[str, Any]] = None) -> AsyncIterator[str]:
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
            ctx = ctx or {}
            done = rec.outcome in ("completed", "cancelled")
            if done:
                self.guard.report_success(rec.provider)
            else:
                self.guard.report_failure(rec.provider, rec.error or "")
            if self.usage is not None:
                self.usage.record(rec.provider, rec.model, done, 200 if rec.outcome != "error" else 502, rec.prompt_tokens,
                                  rec.completion_tokens, rec.total_ms, True, client=ctx.get("client"),
                                  requested_model=ctx.get("requested") or rec.model, request_id=ctx.get("rid"),
                                  error=(PrivacyGuard.sanitize_content(rec.error)[:240] if rec.error else None),
                                  cost=self.price(rec.provider, rec.model, rec.prompt_tokens, rec.completion_tokens) if done else None)
            logging.getLogger("hera.gateway").info(
                "[stream] %s provider=%s model=%s outcome=%s ttfb_ms=%s total_ms=%s chunks=%s fallbacks=%s native=%s",
                rec.request_id, rec.provider, rec.model, rec.outcome, rec.ttfb_ms, rec.total_ms, rec.chunks,
                rec.attempts - 1, rec.native)
            release()
