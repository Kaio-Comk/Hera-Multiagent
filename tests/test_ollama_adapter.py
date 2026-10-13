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

_REAL_CLIENT = httpx.AsyncClient


def _mock_ollama(monkeypatch, status=200, chunks=None, raw=None):
    """Troca o cliente HTTP do adaptador por um que responde NDJSON (o /api/generate agora e em streaming)."""
    import json as _json
    real = _REAL_CLIENT

    def handler(request):
        body = raw if raw is not None else "\n".join(_json.dumps(c) for c in chunks) + "\n"
        return httpx.Response(status, content=body)

    monkeypatch.setattr("adapters.ollama.httpx.AsyncClient",
                        lambda *a, **k: real(transport=httpx.MockTransport(handler), timeout=k.get("timeout")))


@pytest.mark.asyncio
async def test_ollama_submit_and_collect_mocked(monkeypatch):
    from adapters import progress
    adapter = OllamaAdapter(base_url="http://mock-ollama:11434")
    _mock_ollama(monkeypatch, chunks=[
        {"model": "qwen2.5-coder:1.5b", "response": "def soma(a, b):\n", "done": False},
        {"model": "qwen2.5-coder:1.5b", "response": "    return a + b", "done": False},
        {"model": "qwen2.5-coder:1.5b", "response": "", "done": True, "eval_count": 50,
         "eval_duration": 500_000_000, "prompt_eval_count": 12}])           # 0.5s -> 100 tps
    seen = []
    task = AgentTask(
        id="task-ollama-01",
        title="Criar função soma",
        instruction="Crie uma função python soma(a, b)",
        acceptance_criteria=["Retorna a soma de a e b"]
    )

    token = progress.bind(lambda kind, text="", **meta: seen.append((kind, text)))
    try:
        handle = await adapter.submit(task)
        assert handle.task_id == "task-ollama-01"
        result = await adapter.collect(handle)
    finally:
        progress.unbind(token)
    assert result.status == TaskStatus.SUCCEEDED
    assert "def soma" in result.output_text and result.output_text.endswith("return a + b")
    assert "100.0 tokens/s" in result.summary
    assert result.metadata["output_tokens"] == 50 and result.metadata["input_tokens"] == 12
    assert "".join(t for k, t in seen if k == "text") == "def soma(a, b):\n    return a + b"    # o console ao vivo recebeu o texto


@pytest.mark.asyncio
async def test_ollama_http_error_and_stream_error_fail_cleanly(monkeypatch):
    adapter = OllamaAdapter(base_url="http://mock-ollama:11434")
    task = AgentTask(id="task-ollama-02", title="x", instruction="y")
    _mock_ollama(monkeypatch, status=500, raw="boom")
    result = await adapter.collect(await adapter.submit(task))
    assert result.status == TaskStatus.FAILED and "500" in result.summary and "boom" in result.error_message
    _mock_ollama(monkeypatch, chunks=[{"error": "model requires more system memory"}])
    result = await adapter.collect(await adapter.submit(AgentTask(id="task-ollama-03", title="x", instruction="y")))
    assert result.status == TaskStatus.FAILED and "more system memory" in result.error_message

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
    if result.status != TaskStatus.SUCCEEDED and "memory" in (result.error_message or "").lower():
        pytest.skip(f"Ollama sem memoria para carregar o modelo: {result.error_message}")

    assert result.status == TaskStatus.SUCCEEDED
    assert len(result.output_text) > 0
    assert result.execution_time_seconds > 0
