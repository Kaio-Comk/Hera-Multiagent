import pytest
import httpx
from models.task import AgentTask, TaskStatus
from adapters.base import AgentAdapter
from adapters.nine_router import NineRouterAdapter

def test_nine_router_implements_protocol():
    adapter = NineRouterAdapter()
    assert isinstance(adapter, AgentAdapter)
    assert adapter.name == "9router"

@pytest.mark.asyncio
async def test_nine_router_capabilities():
    adapter = NineRouterAdapter()
    caps = await adapter.capabilities()
    assert caps.is_local_offline is False
    assert caps.supports_streaming is True
    assert caps.supports_vision is True

@pytest.mark.asyncio
async def test_nine_router_health_check_mocked(monkeypatch):
    adapter = NineRouterAdapter(base_url="http://mock-9router:20128/v1")

    async def mock_get(client, url, *args, **kwargs):
        return httpx.Response(200, json={
            "data": [
                {"id": "alicode-intl/qwen3.5-plus"},
                {"id": "alicode-intl/glm-5"}
            ]
        })

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    health = await adapter.health_check()
    assert health.healthy is True
    assert "alicode-intl/qwen3.5-plus" in health.available_models
    assert len(health.available_models) == 2

@pytest.mark.asyncio
async def test_nine_router_health_check_failure(monkeypatch):
    adapter = NineRouterAdapter(base_url="http://mock-9router:20128/v1")

    async def mock_get(client, url, *args, **kwargs):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    health = await adapter.health_check()
    assert health.healthy is False
    assert "Falha de conexão" in health.error_message

@pytest.mark.asyncio
async def test_nine_router_submit_and_collect_mocked(monkeypatch):
    adapter = NineRouterAdapter(base_url="http://mock-9router:20128/v1")

    async def mock_post(client, url, *args, **kwargs):
        return httpx.Response(200, json={
            "choices": [
                {"message": {"content": "Análise técnica concluída com sucesso."}}
            ],
            "usage": {
                "total_tokens": 128
            }
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    task = AgentTask(
        id="task-9r-01",
        title="Análise de Código",
        instruction="Analise a viabilidade técnica da refatoração"
    )

    handle = await adapter.submit(task)
    result = await adapter.collect(handle)

    assert result.status == TaskStatus.SUCCEEDED
    assert "Análise técnica" in result.output_text
    assert "128 tokens" in result.summary

@pytest.mark.asyncio
async def test_nine_router_live_health():
    """Teste de saúde ao vivo conectando ao 9Router rodando na porta 20128."""
    adapter = NineRouterAdapter(base_url="http://127.0.0.1:20128/v1")
    health = await adapter.health_check()
    if not health.healthy:
        pytest.skip(f"9Router local não está acessível: {health.error_message}")

    assert health.healthy is True
    assert len(health.available_models) > 0
