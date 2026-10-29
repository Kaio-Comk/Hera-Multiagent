"""Adaptadores de provedores para o Hera Smart Gateway.

Normaliza requisições e respostas entre Ollama local e provedores cloud compatíveis com OpenAI.
"""

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional
import httpx

from app.gateway.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatChoice,
    ChatMessage,
    UsageStats,
)
from app.gateway.provider_policy import prefer_ipv4_loopback, validate_provider_url
from app.gateway.key_manager import KeyManager, NoKeyAvailableError, parse_retry_after

RETRY_5XX_DELAY = 2.0          # segundos antes de repetir um 502/503/504 (uma vez por chamada)

_DURATION = __import__("re").compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


def parse_reset_seconds(value: Optional[str]) -> Optional[float]:
    """Tempo até a cota voltar, em segundos: aceita `42`, `1.5`, `6m0s`, `1h2m`, `120ms`. None se ilegível."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    parts = _DURATION.findall(value)
    if not parts or "".join(n + u for n, u in parts) != value:
        return None
    unit = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    return sum(float(n) * unit[u] for n, u in parts)


def parse_rate_limit_headers(headers: Any, previous: Optional[Dict[str, Any]] = None,
                             now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Lê `x-ratelimit-{limit,remaining,reset}-{requests,tokens}` (padrão OpenAI/Groq/etc.). Sem nenhum, mantém o anterior."""
    def num(name: str) -> Optional[int]:
        try:
            return int(float(headers.get(name)))
        except (TypeError, ValueError):
            return None
    found = {
        "limit_requests": num("x-ratelimit-limit-requests"), "remaining_requests": num("x-ratelimit-remaining-requests"),
        "reset_requests_s": parse_reset_seconds(headers.get("x-ratelimit-reset-requests")),
        "limit_tokens": num("x-ratelimit-limit-tokens"), "remaining_tokens": num("x-ratelimit-remaining-tokens"),
        "reset_tokens_s": parse_reset_seconds(headers.get("x-ratelimit-reset-tokens")),
    }
    if all(v is None for v in found.values()):
        return previous
    found["updated_at"] = int(now if now is not None else time.time())
    return found


@dataclass(frozen=True)
class ModalityResponse:
    """Resposta crua de uma modalidade que não é chat (embeddings, imagem, fala, transcrição)."""
    content: bytes
    media_type: str = "application/json"
    status: int = 200
    provider: str = ""          # quem atendeu (preenchido pelo roteador): amarra tarefas assíncronas (vídeo) ao provedor


MODALITY_PATHS = {"embeddings": "/embeddings", "images": "/images/generations", "speech": "/audio/speech",
                  "transcription": "/audio/transcriptions", "video": "/videos"}


class GatewayProviderError(Exception):
    """Erro levantado durante execução de chamada upstream ao provedor."""
    def __init__(self, message: str, status_code: int = 500, retryable: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable

class BaseGatewayAdapter:
    """Interface base para adaptadores do Gateway."""
    name: str = "base"
    tier: str = "local"
    is_local: bool = True
    # Modelos que ESTE provedor atende (nuvem). None = local: atende o que nenhuma nuvem reivindica.
    served_models: Optional[frozenset] = None

    def supports(self, kind: str) -> bool:
        """Modalidades além do chat (`embeddings`, `images`, `speech`, `transcription`). Padrão: nenhuma."""
        return False

    async def modality(self, kind: str, payload: Dict[str, Any], client: httpx.AsyncClient,
                       files: Optional[Dict[str, Any]] = None) -> "ModalityResponse":
        raise GatewayProviderError(f"Provedor {self.name} não atende '{kind}'.", status_code=404, retryable=True)

    async def chat_complete(
        self,
        request: ChatCompletionRequest,
        client: httpx.AsyncClient
    ) -> ChatCompletionResponse:
        raise NotImplementedError

    async def open_stream(self, request: ChatCompletionRequest, client: httpx.AsyncClient):
        """Pré-voo do stream. Adaptadores SEM streaming nativo executam a resposta completa e a fatiam
        (`native=False` na telemetria). Os nativos (Ollama, nuvem) sobrescrevem este método."""
        from app.gateway.streaming import preflight, simulated_deltas
        resp = await self.chat_complete(request, client)
        text = resp.choices[0].message.content or ""

        async def _noop() -> None:
            return None
        return await preflight(self.name, resp.model, resp.id,
                               simulated_deltas(text, self.name, resp.model, resp.id,
                                                resp.choices[0].finish_reason or "stop"),
                               _noop, native=False)

class OllamaGatewayAdapter(BaseGatewayAdapter):
    """Adaptador para o daemon Ollama local (GPU/CPU)."""
    name = "ollama"
    tier = "local"
    is_local = True
    fit_check: Optional[Callable[[str], Awaitable[Optional[str]]]] = None   # motivo da recusa (modelo grande demais para a GPU) ou None

    def __init__(self, base_url: str = "http://127.0.0.1:11434", default_model: str = "qwen2.5-coder:1.5b", timeout: float = 120.0):
        self.base_url = prefer_ipv4_loopback(validate_provider_url(base_url, is_local_profile=True))
        self.default_model = default_model
        self.timeout = timeout

    async def _refuse_if_too_big(self, model: str) -> None:
        if self.fit_check is None:
            return
        try:
            refusal = await self.fit_check(model)
        except Exception:       # noqa: BLE001 - a trava nunca pode impedir a chamada por defeito proprio
            return
        if refusal:
            raise GatewayProviderError(refusal, status_code=400, retryable=False)

    async def chat_complete(
        self,
        request: ChatCompletionRequest,
        client: httpx.AsyncClient
    ) -> ChatCompletionResponse:
        model = request.model if request.model and request.model != "default" else self.default_model
        await self._refuse_if_too_big(model)
        
        # Formatar mensagens para a API nativa do Ollama (/api/chat)
        ollama_messages = []
        for m in request.messages:
            ollama_messages.append({
                "role": m.role,
                "content": m.content if isinstance(m.content, str) else str(m.content)
            })

        payload = {
            "model": model,
            "messages": ollama_messages,
            "stream": False,
            "options": {
                "temperature": request.temperature,
                "top_p": request.top_p,
            }
        }
        if request.max_tokens:
            payload["options"]["num_predict"] = request.max_tokens

        url = f"{self.base_url}/api/chat"
        try:
            resp = await client.post(url, json=payload, timeout=self.timeout)
            if resp.status_code != 200:
                is_retryable = resp.status_code >= 500
                raise GatewayProviderError(
                    f"Ollama retornou HTTP {resp.status_code}: {resp.text[:200]}",
                    status_code=resp.status_code,
                    retryable=is_retryable
                )
            
            data = resp.json()
            msg_content = data.get("message", {}).get("content", "")
            prompt_tokens = data.get("prompt_eval_count", 0)
            completion_tokens = data.get("eval_count", 0)

            return ChatCompletionResponse(
                id=f"chatcmpl-ollama-{uuid.uuid4().hex[:12]}",
                created=int(time.time()),
                model=model,
                choices=[
                    ChatChoice(
                        index=0,
                        message=ChatMessage(role="assistant", content=msg_content),
                        finish_reason="stop" if data.get("done") else "length"
                    )
                ],
                usage=UsageStats(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens
                )
            )
        except httpx.TimeoutException as e:
            raise GatewayProviderError(f"Timeout ao comunicar com Ollama: {str(e)}", status_code=504, retryable=True)
        except httpx.RequestError as e:
            raise GatewayProviderError(f"Falha de conexão com Ollama: {str(e)}", status_code=502, retryable=True)

    def supports(self, kind: str) -> bool:
        return kind == "embeddings"

    async def modality(self, kind: str, payload: Dict[str, Any], client: httpx.AsyncClient,
                       files: Optional[Dict[str, Any]] = None) -> ModalityResponse:
        """Embeddings pelo `/api/embed` do Ollama, devolvidos no formato OpenAI (`/v1/embeddings`)."""
        if kind != "embeddings":
            return await super().modality(kind, payload, client, files)
        model = payload.get("model") if payload.get("model") and payload.get("model") != "default" else self.default_model
        try:
            resp = await client.post(f"{self.base_url}/api/embed", json={"model": model, "input": payload.get("input")},
                                     timeout=self.timeout)
        except httpx.TimeoutException as e:
            raise GatewayProviderError(f"Timeout ao comunicar com Ollama: {type(e).__name__}", status_code=504, retryable=True)
        except httpx.RequestError as e:
            raise GatewayProviderError(f"Falha de conexão com Ollama: {type(e).__name__}", status_code=502, retryable=True)
        if resp.status_code != 200:
            raise GatewayProviderError(f"Ollama retornou HTTP {resp.status_code} em embeddings",
                                       status_code=resp.status_code, retryable=resp.status_code >= 500)
        data = resp.json()
        vectors = data.get("embeddings") or []
        tokens = int(data.get("prompt_eval_count") or 0)
        body = {"object": "list", "model": model,
                "data": [{"object": "embedding", "index": i, "embedding": v} for i, v in enumerate(vectors)],
                "usage": {"prompt_tokens": tokens, "total_tokens": tokens}}
        return ModalityResponse(json.dumps(body).encode("utf-8"))

    async def open_stream(self, request: ChatCompletionRequest, client: httpx.AsyncClient):
        """Streaming nativo do Ollama (`/api/chat` com stream=true, NDJSON)."""
        from app.gateway.streaming import ndjson_deltas, preflight
        model = request.model if request.model and request.model != "default" else self.default_model
        await self._refuse_if_too_big(model)
        payload = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content if isinstance(m.content, str) else str(m.content)}
                         for m in request.messages],
            "stream": True,
            "options": {"temperature": request.temperature, "top_p": request.top_p},
        }
        if request.max_tokens:
            payload["options"]["num_predict"] = request.max_tokens
        url = f"{self.base_url}/api/chat"
        try:
            resp = await client.send(client.build_request("POST", url, json=payload, timeout=self.timeout), stream=True)
        except httpx.TimeoutException as e:
            raise GatewayProviderError(f"Timeout ao comunicar com Ollama: {type(e).__name__}", status_code=504, retryable=True)
        except httpx.RequestError as e:
            raise GatewayProviderError(f"Falha de conexão com Ollama: {type(e).__name__}", status_code=502, retryable=True)
        if resp.status_code != 200:
            body = (await resp.aread())[:200].decode("utf-8", "replace")
            await resp.aclose()
            raise GatewayProviderError(f"Ollama retornou HTTP {resp.status_code}: {body}",
                                       status_code=resp.status_code, retryable=resp.status_code >= 500)
        rid = f"chatcmpl-ollama-{uuid.uuid4().hex[:12]}"
        return await preflight(self.name, model, rid, ndjson_deltas(resp.aiter_lines(), self.name, model, rid), resp.aclose)


class CloudOpenAIGatewayAdapter(BaseGatewayAdapter):
    """Adaptador para provedores de nuvem compatíveis com a API OpenAI (DeepSeek, Groq, OpenRouter, etc.).

    As chaves vêm de um `KeyManager` (rodízio só entre chaves autorizadas pelo usuário; 429 põe a chave em
    espera pelo `Retry-After`; 401/403 desativa a chave). O segredo nunca aparece em mensagens de erro."""
    is_local = False

    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: Optional[str] = None,
        default_model: str = "default",
        tier: str = "cloud_cheap",
        timeout: float = 60.0,
        key_manager: Optional[KeyManager] = None,
        models: Optional[List[str]] = None,
        stream_usage: bool = False,
        modalities: Optional[List[str]] = None,
        extra_headers: Optional[Dict[str, str]] = None,
    ):
        self.name = name
        self.base_url = validate_provider_url(base_url, is_local_profile=False)
        # Cabeçalhos extras do provedor (já validados: sem Authorization/Content-Type etc., ver sanitize_extra_headers).
        self.extra_headers: Dict[str, str] = dict(extra_headers or {})
        # Última cota informada pelo provedor (cabeçalhos x-ratelimit-*): None até a primeira resposta que a traga.
        self.quota: Optional[Dict[str, Any]] = None
        if key_manager is None:
            if not api_key:
                raise ValueError(f"Provedor {name!r} sem chave de API")
            key_manager = KeyManager([api_key], label=name)
        self.key_manager = key_manager
        self.default_model = default_model
        self.tier = tier
        self.timeout = timeout
        self.served_models = frozenset([default_model, *(models or [])])
        # Pede `stream_options.include_usage` (nem todo provedor aceita: opt-in no config).
        self.stream_usage = stream_usage
        # Modalidades além do chat que este provedor atende (config `modalities:`); padrão: todas as que têm rota.
        self.modalities = frozenset(modalities if modalities is not None else MODALITY_PATHS)

    def supports(self, kind: str) -> bool:
        return kind in self.modalities and kind in MODALITY_PATHS

    async def video_fetch(self, video_id: str, client: httpx.AsyncClient, content: bool = False):
        """Status (`GET /videos/{id}`) ou arquivo (`GET /videos/{id}/content`, devolvido ABERTO para streaming)."""
        if not self.supports("video"):
            raise GatewayProviderError(f"Provedor {self.name} não atende 'video'.", status_code=404, retryable=False)
        from urllib.parse import quote
        path = f"/videos/{quote(video_id, safe='')}" + ("/content" if content else "")
        return await self._send_with_keys(client, path, method="GET", stream=content)

    async def modality(self, kind: str, payload: Dict[str, Any], client: httpx.AsyncClient,
                       files: Optional[Dict[str, Any]] = None) -> ModalityResponse:
        if not self.supports(kind):
            return await super().modality(kind, payload, client, files)
        if files is not None:
            resp = await self._send_with_keys(client, MODALITY_PATHS[kind], data=payload, files=files)
        else:
            resp = await self._send_with_keys(client, MODALITY_PATHS[kind], payload=payload)
        default_type = "audio/mpeg" if kind == "speech" else "application/json"
        return ModalityResponse(resp.content, resp.headers.get("content-type", default_type).split(";")[0], resp.status_code)

    async def _request_with_keys(self, client: httpx.AsyncClient, payload: Dict[str, Any], stream: bool):
        return await self._send_with_keys(client, "/chat/completions", payload=payload, stream=stream)

    async def _send_with_keys(self, client: httpx.AsyncClient, path: str, *, payload: Optional[Dict[str, Any]] = None,
                              data: Optional[Dict[str, Any]] = None, files: Optional[Dict[str, Any]] = None,
                              stream: bool = False, method: str = "POST"):
        """Envia a requisição com uma chave do KeyManager. Resposta 200 volta aberta (`stream=True`) ou
        completa; 429 põe a chave para descansar (Retry-After) e tenta outra chave AUTORIZADA; 401/403
        desativa a chave. O segredo nunca aparece em mensagens de erro."""
        url = f"{self.base_url}{path}"
        last_error: Optional[GatewayProviderError] = None
        prepare = getattr(self.key_manager, "prepare", None)      # OAuth: renova o token se estiver vencendo
        repetiu_5xx = False
        for _ in range(max(1, len(self.key_manager)) + 1):          # +1: a repeticao de um 5xx passageiro
            if prepare is not None:
                await prepare(client)
            try:
                lease = self.key_manager.acquire()
            except NoKeyAvailableError as exc:
                if last_error is not None:
                    raise last_error
                wait = f" (tente em {int(exc.retry_after)}s)" if exc.retry_after else ""
                raise GatewayProviderError(
                    f"Todas as chaves de {self.name} estão em espera ou desativadas{wait}",
                    status_code=429 if exc.retry_after else 503, retryable=True)

            headers = {**self.extra_headers, "Authorization": f"Bearer {lease.secret}"}
            if files is None and method != "GET":   # multipart define o próprio Content-Type (com o boundary)
                headers["Content-Type"] = "application/json"
            if method == "GET":
                kwargs: Dict[str, Any] = {}
            else:
                kwargs = {"files": files, "data": data} if files is not None else {"json": payload}
            try:
                if stream:
                    resp = await client.send(client.build_request(method, url, headers=headers,
                                                                  timeout=self.timeout, **kwargs), stream=True)
                else:
                    resp = await client.request(method, url, headers=headers, timeout=self.timeout, **kwargs)
            except httpx.TimeoutException as e:
                raise GatewayProviderError(f"Timeout ao comunicar com {self.name}: {type(e).__name__}",
                                           status_code=504, retryable=True)
            except httpx.RequestError as e:
                raise GatewayProviderError(f"Erro de rede com {self.name}: {type(e).__name__}",
                                           status_code=502, retryable=True)

            fresh = parse_rate_limit_headers(resp.headers)       # só o que ESTA resposta trouxe (nada de valor velho)
            if fresh:
                self.quota = fresh
            if resp.status_code == 200:
                # Cota de requisições zerada com data de volta conhecida: descansa ESTA chave até lá em vez de gastar um 429.
                if fresh and fresh.get("remaining_requests") == 0 and fresh.get("reset_requests_s"):
                    self.key_manager.report_rate_limited(lease.key_id, float(fresh["reset_requests_s"]))
                return resp
            if stream:
                await resp.aclose()
            if resp.status_code == 429:
                self.key_manager.report_rate_limited(lease.key_id, parse_retry_after(resp.headers.get("retry-after")))
                last_error = GatewayProviderError(f"Provedor {self.name} retornou HTTP 429 (limite de uso)",
                                                  status_code=429, retryable=True)
                continue
            if resp.status_code in (401, 403):
                self.key_manager.report_auth_failure(lease.key_id)
                last_error = GatewayProviderError(
                    f"Provedor {self.name} recusou a chave {lease.key_id} (HTTP {resp.status_code}); chave desativada",
                    status_code=resp.status_code, retryable=True)   # retryable: o roteador cai para o próximo provedor
                continue
            erro = GatewayProviderError(
                f"Provedor {self.name} retornou HTTP {resp.status_code}",
                status_code=resp.status_code,
                retryable=resp.status_code in (500, 502, 503, 504))
            if resp.status_code in (502, 503, 504) and not repetiu_5xx:
                repetiu_5xx = True                       # sobrecarga passageira (River 503): uma nova tentativa
                last_error = erro
                await asyncio.sleep(RETRY_5XX_DELAY)
                continue
            raise erro
        raise last_error or GatewayProviderError(f"Falha desconhecida com {self.name}", status_code=502, retryable=True)

    def _payload(self, request: ChatCompletionRequest, stream: bool) -> Dict[str, Any]:
        model = request.model if request.model and request.model != "default" else self.default_model
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "stream": stream,
            "temperature": request.temperature,
            "top_p": request.top_p,
        }
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop
        if request.tools:
            payload["tools"] = [t.model_dump(exclude_none=True) for t in request.tools]
            if request.tool_choice is not None:
                payload["tool_choice"] = request.tool_choice
        if stream and self.stream_usage:
            payload["stream_options"] = {"include_usage": True}
        return payload

    async def chat_complete(
        self,
        request: ChatCompletionRequest,
        client: httpx.AsyncClient
    ) -> ChatCompletionResponse:
        resp = await self._request_with_keys(client, self._payload(request, stream=False), stream=False)
        return ChatCompletionResponse.model_validate(resp.json())

    async def open_stream(self, request: ChatCompletionRequest, client: httpx.AsyncClient):
        """Streaming nativo (SSE compatível com OpenAI): pré-voo lê até o primeiro dado útil."""
        from app.gateway.streaming import openai_sse_deltas, preflight
        payload = self._payload(request, stream=True)
        resp = await self._request_with_keys(client, payload, stream=True)
        rid = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        return await preflight(self.name, payload["model"], rid,
                               openai_sse_deltas(resp.aiter_lines(), self.name, payload["model"], rid), resp.aclose)
