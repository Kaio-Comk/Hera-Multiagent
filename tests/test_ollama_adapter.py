import pytest
import httpx
from models.task import AgentTask, TaskStatus
from adapters.base import AgentAdapter
from adapters.ollama import OllamaAdapter

def test_ollama_adapter_implements_protocol():
    adapter = OllamaAdapter()
    assert isinstance(adapter, AgentAdapter)
    assert adapter.name == "ollama"

@pytest.mark.asyncio
async def test_ollama_capabilities():
    adapter = OllamaAdapter()
    caps = await adapter.capabilities()
    assert caps.is_local_offline is True
    assert caps.supports_streaming is True
    assert caps.supports_terminal is False

@pytest.mark.asyncio
async def test_ollama_health_check_success(monkeypatch):
    adapter = OllamaAdapter(base_url="http://mock-ollama:11434")

    async def mock_get(client, url, *args, **kwargs):
        if "version" in str(url):
            return httpx.Response(200, json={"version": "0.40.2"})
        if "tags" in str(url):
            return httpx.Response(200, json={
                "models": [
                    {"name": "qwen2.5-coder:1.5b"},
                    {"name": "qwen3.5:4b"}
                ]
            })
        return httpx.Response(404)

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    health = await adapter.health_check()
    assert health.healthy is True
    assert health.version == "0.40.2"
    assert "qwen2.5-coder:1.5b" in health.available_models
    assert "qwen3.5:4b" in health.available_models

@pytest.mark.asyncio
async def test_ollama_health_check_failure(monkeypatch):
    adapter = OllamaAdapter(base_url="http://mock-ollama:11434")

    async def mock_get(client, url, *args, **kwargs):
        raise httpx.ConnectError("Connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    health = await adapter.health_check()
    assert health.healthy is False
    assert "Falha de conexão" in health.error_message

@pytest.mark.asyncio
async def test_ollama_submit_and_collect_mocked(monkeypatch):
    adapter = OllamaAdapter(base_url="http://mock-ollama:11434")

    async def mock_post(client, url, *args, **kwargs):
        return httpx.Response(200, json={
            "model": "qwen2.5-coder:1.5b",
            "response": "def soma(a, b):\n    return a + b",
            "done": True,
            "eval_count": 50,
            "eval_duration": 500_000_000  # 0.5s -> 100 tps
        })

    monkeypatch.setattr(httpx.AsyncClient, "post", mock_post)

    task = AgentTask(
        id="task-ollama-01",
        title="Criar função soma",
        instruction="Crie uma função python soma(a, b)",
        acceptance_criteria=["Retorna a soma de a e b"]
    )

    handle = await adapter.submit(task)
    assert handle.task_id == "task-ollama-01"

    result = await adapter.collect(handle)
    assert result.status == TaskStatus.SUCCEEDED
    assert "def soma" in result.output_text
    assert "100.0 tokens/s" in result.summary

@pytest.mark.asyncio
async def test_ollama_live_e2e():
    """Teste de integração real se o serviço Ollama local estiver rodando."""
    adapter = OllamaAdapter(base_url="http://localhost:11434", default_model="qwen2.5-coder:1.5b")
    health = await adapter.health_check()
    if not health.healthy:
        pytest.skip(f"Ollama local não está acessível: {health.error_message}")

    task = AgentTask(
        id="task-live-ollama",
        title="Teste Live Ollama",
        instruction="Responda apenas: 'ONLINE_OK'",
        timeout_seconds=30
    )

    handle = await adapter.submit(task)
    result = await adapter.collect(handle)

    assert result.status == TaskStatus.SUCCEEDED
    assert len(result.output_text) > 0
    assert result.execution_time_seconds > 0
