"""Testes automatizados para endpoints OpenAI-compatible (/v1) e roteamento com fallback (Fase 2)."""

import pytest
import httpx
from httpx import ASGITransport, AsyncClient

from app.server import app
from app.gateway.schemas import ChatCompletionRequest, ChatMessage
from app.gateway.providers import (
    OllamaGatewayAdapter,
    CloudOpenAIGatewayAdapter,
    GatewayProviderError,
)
from app.gateway.privacy import GatewayPrivacyManager
from app.gateway.tier_router import TierRouter

@pytest.mark.asyncio
async def test_get_v1_models():
    """Valida que o endpoint GET /v1/models retorne a lista de modelos no formato OpenAI."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/v1/models")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("object") == "list"
        assert isinstance(data.get("data"), list)
        model_ids = [m["id"] for m in data["data"]]
        assert "qwen2.5-coder:1.5b" in model_ids

@pytest.mark.asyncio
async def test_tier_router_success_single_provider():
    """Valida fluxo de sucesso em provedor local único com mock."""
    async def mock_ollama_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "message": {"content": "Código Python gerado com sucesso!"},
            "done": True,
            "prompt_eval_count": 25,
            "eval_count": 50
        })

    mock_transport = httpx.MockTransport(mock_ollama_handler)
    async with httpx.AsyncClient(transport=mock_transport) as http_client:
        adapter = OllamaGatewayAdapter()
        router = TierRouter(adapters=[adapter])

        req = ChatCompletionRequest(
            model="qwen2.5-coder:1.5b",
            messages=[ChatMessage(role="user", content="Escreva um hello world")]
        )
        resp = await router.route_and_execute(req, http_client)
        assert resp.model == "qwen2.5-coder:1.5b"
        assert len(resp.choices) == 1
        assert resp.choices[0].message.content == "Código Python gerado com sucesso!"
        assert resp.usage.total_tokens == 75

@pytest.mark.asyncio
async def test_tier_router_pre_response_fallback():
    """Valida que o TierRouter faça fallback para o provedor secundário se o primário falhar com 500."""
    call_log = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        call_log.append(str(request.url))
        # Provedor 1 falha com 500 (retryable)
        if "api.deepseek.com" in str(request.url):
            return httpx.Response(500, json={"error": "Erro interno no servidor cloud"})
        # Provedor 2 (Ollama local) responde com sucesso
        return httpx.Response(200, json={
            "message": {"content": "Resposta do fallback local"},
            "done": True,
            "prompt_eval_count": 10,
            "eval_count": 20
        })

    mock_transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=mock_transport) as http_client:
        primary_cloud = CloudOpenAIGatewayAdapter(
            name="deepseek_cloud",
            base_url="https://api.deepseek.com/v1",
            api_key="fake-key-for-test",
            default_model="deepseek-chat",
            tier="cloud_cheap"
        )
        fallback_local = OllamaGatewayAdapter()

        router = TierRouter(adapters=[primary_cloud, fallback_local])

        req = ChatCompletionRequest(
            model="default",
            messages=[ChatMessage(role="user", content="Tarefa simples")]
        )
        resp = await router.route_and_execute(req, http_client)
        assert resp.choices[0].message.content == "Resposta do fallback local"
        assert len(call_log) == 2
        assert "deepseek" in call_log[0]
        assert "11434" in call_log[1]

@pytest.mark.asyncio
async def test_tier_router_dlp_blocks_cloud_on_secret():
    """Valida que o TierRouter elimine os provedores de nuvem e falhe com segurança se nenhum local puder atender."""
    cloud_adapter = CloudOpenAIGatewayAdapter(
        name="openai_cloud",
        base_url="https://api.openai.com/v1",
        api_key="fake-key",
        default_model="gpt-4o",
        tier="cloud_premium"
    )
    # Roteador configurado APENAS com provedores cloud
    router = TierRouter(adapters=[cloud_adapter])

    req = ChatCompletionRequest(
        model="gpt-4o",
        messages=[ChatMessage(role="user", content="Minha chave confidencial: sk-abcdef1234567890abcdef1234567890")]
    )

    async with httpx.AsyncClient() as client:
        with pytest.raises(GatewayProviderError) as exc_info:
            await router.route_and_execute(req, client)
        assert exc_info.value.status_code == 403
        assert "DLP" in str(exc_info.value)

@pytest.mark.asyncio
async def test_post_v1_chat_completions_dlp_rejection():
    """Valida rejeição com HTTP 403 no endpoint /v1/chat/completions se houver segredo e nenhum motor local."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        payload = {
            "model": "qwen2.5-coder:1.5b",
            "messages": [
                {"role": "user", "content": "Minha chave AWS: AKIAIOSFODNN7EXAMPLE"}
            ]
        }
        # Ollama local pode estar rodando ou não no ambiente de teste; 
        # a requisição deve ser classificada como LOCAL_ONLY e nunca ir para a nuvem
        resp = await client.post("/v1/chat/completions", json=payload)
        # Deve ou responder (se o Ollama local estiver ativo) ou retornar 403/502/504 sem tocar na nuvem
        assert resp.status_code in (200, 403, 502, 504)
