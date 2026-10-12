"""Adaptadores de provedores para o Hera Smart Gateway.

Normaliza requisições e respostas entre Ollama local e provedores cloud compatíveis com OpenAI.
"""

import time
import uuid
from typing import Any, Dict, List, Optional
import httpx

from app.gateway.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatChoice,
    ChatMessage,
    UsageStats,
)
from app.gateway.provider_policy import validate_provider_url
from app.gateway.key_manager import KeyManager, NoKeyAvailableError, parse_retry_after

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

    async def chat_complete(
        self,
        request: ChatCompletionRequest,
        client: httpx.AsyncClient
    ) -> ChatCompletionResponse:
        raise NotImplementedError

class OllamaGatewayAdapter(BaseGatewayAdapter):
    """Adaptador para o daemon Ollama local (GPU/CPU)."""
    name = "ollama"
    tier = "local"
    is_local = True

    def __init__(self, base_url: str = "http://127.0.0.1:11434", default_model: str = "qwen2.5-coder:1.5b", timeout: float = 120.0):
        self.base_url = validate_provider_url(base_url, is_local_profile=True)
        self.default_model = default_model
        self.timeout = timeout

    async def chat_complete(
        self,
        request: ChatCompletionRequest,
        client: httpx.AsyncClient
    ) -> ChatCompletionResponse:
        model = request.model if request.model and request.model != "default" else self.default_model
        
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
    ):
        self.name = name
        self.base_url = validate_provider_url(base_url, is_local_profile=False)
        if key_manager is None:
            if not api_key:
                raise ValueError(f"Provedor {name!r} sem chave de API")
            key_manager = KeyManager([api_key], label=name)
        self.key_manager = key_manager
        self.default_model = default_model
        self.tier = tier
        self.timeout = timeout
        self.served_models = frozenset([default_model, *(models or [])])

    async def chat_complete(
        self,
        request: ChatCompletionRequest,
        client: httpx.AsyncClient
    ) -> ChatCompletionResponse:
        model = request.model if request.model and request.model != "default" else self.default_model

        payload: Dict[str, Any] = {
            "model": model,
            "messages": [m.model_dump(exclude_none=True) for m in request.messages],
            "stream": False,
            "temperature": request.temperature,
            "top_p": request.top_p,
        }
        if request.max_tokens:
            payload["max_tokens"] = request.max_tokens
        if request.stop:
            payload["stop"] = request.stop

        url = f"{self.base_url}/chat/completions"
        last_error: Optional[GatewayProviderError] = None
        for _ in range(max(1, len(self.key_manager))):
            try:
                lease = self.key_manager.acquire()
            except NoKeyAvailableError as exc:
                if last_error is not None:
                    raise last_error
                wait = f" (tente em {int(exc.retry_after)}s)" if exc.retry_after else ""
                raise GatewayProviderError(
                    f"Todas as chaves de {self.name} estão em espera ou desativadas{wait}",
                    status_code=429 if exc.retry_after else 503, retryable=True)

            headers = {"Authorization": f"Bearer {lease.secret}", "Content-Type": "application/json"}
            try:
                resp = await client.post(url, json=payload, headers=headers, timeout=self.timeout)
            except httpx.TimeoutException as e:
                raise GatewayProviderError(f"Timeout ao comunicar com {self.name}: {type(e).__name__}",
                                           status_code=504, retryable=True)
            except httpx.RequestError as e:
                raise GatewayProviderError(f"Erro de rede com {self.name}: {type(e).__name__}",
                                           status_code=502, retryable=True)

            if resp.status_code == 200:
                return ChatCompletionResponse.model_validate(resp.json())

            if resp.status_code == 429:
                # Respeita o Retry-After: esta chave descansa; só tenta outra chave que o usuário autorizou.
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
            raise GatewayProviderError(
                f"Provedor {self.name} retornou HTTP {resp.status_code}",
                status_code=resp.status_code,
                retryable=resp.status_code in (500, 502, 503, 504))

        raise last_error or GatewayProviderError(f"Falha desconhecida com {self.name}", status_code=502, retryable=True)
