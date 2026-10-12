"""Testes unitários e de integração para RTK Token Saver e Streaming SSE (Fase 3)."""

import pytest
import httpx
from httpx import ASGITransport, AsyncClient

from app.server import app
from app.gateway.token_saver import RTKTokenSaver
from app.gateway.sse import format_openai_sse_chunk, format_sse_error, format_sse_done, sse_stream_generator
from helpers import AUTH_HEADERS, TEST_BASE_URL

# ============================================================================
# 1. Testes do RTK Token Saver Conservador
# ============================================================================

def test_rtk_preserves_python_code_indentation():
    """Garante que o código Python e sua indentação sejam preservados byte-exact."""
    code_sample = """def calculate_metrics(items: list) -> dict:
    total = 0
    for x in items:
        if x > 0:
            total += x
    return {"total": total}
"""
    saver = RTKTokenSaver(max_tool_lines=50)
    compressed, report = saver.compress_tool_output(code_sample)
    assert compressed == code_sample
    assert report.is_lossy is False
    assert report.strategy_applied == "verbatim_preserved"

def test_rtk_preserves_valid_json():
    """Garante que JSON válido passe sem truncamento para evitar quebra de sintaxe."""
    json_payload = '{"status": "ok", "records": [1, 2, 3], "nested": {"key": "val"}}'
    saver = RTKTokenSaver()
    compressed, report = saver.compress_tool_output(json_payload)
    assert compressed == json_payload
    assert report.strategy_applied == "json_passthrough"

def test_rtk_structured_head_tail_truncation():
    """Valida truncamento estruturado (Head + Marker + Tail) para logs gigantes."""
    lines = [f"Linha de log número {i}\n" for i in range(200)]
    huge_log = "".join(lines)

    saver = RTKTokenSaver(max_tool_lines=100, head_lines=30, tail_lines=30)
    compressed, report = saver.compress_tool_output(huge_log)

    assert report.is_lossy is True
    assert report.reduction_percentage > 50.0
    assert "linhas omitidas pelo RTK Token Saver" in compressed
    assert "Linha de log número 0" in compressed      # Head preservado
    assert "Linha de log número 199" in compressed    # Tail preservado

def test_rtk_consecutive_lines_deduplication():
    """Valida colapso de linhas idênticas consecutivas em logs de polling."""
    repetitive_log = "Polling worker status: RUNNING\n" * 20
    saver = RTKTokenSaver(max_tool_lines=100, dedup_repeats_threshold=3)
    compressed, report = saver.compress_tool_output(repetitive_log)

    assert report.is_lossy is True
    assert "linha acima repetida 19 vezes" in compressed
    assert len(compressed) < len(repetitive_log)

# ============================================================================
# 2. Testes de Serialização e Streaming SSE
# ============================================================================

def test_format_openai_sse_chunk():
    """Valida formatação padrão de chunk SSE compatível com OpenAI."""
    chunk = format_openai_sse_chunk(
        request_id="test-req-123",
        model="qwen2.5-coder:1.5b",
        content="def hello():"
    )
    assert chunk.startswith("data: {")
    assert chunk.endswith("\n\n")
    assert '"content": "def hello():"' in chunk
    assert '"model": "qwen2.5-coder:1.5b"' in chunk

def test_format_sse_done():
    """Valida evento finalizador."""
    assert format_sse_done() == "data: [DONE]\n\n"

@pytest.mark.asyncio
async def test_sse_stream_generator_normal_flow():
    """Valida fluxo de streaming com preflight e terminação limpa."""
    async def sample_stream():
        yield " mundo"
        yield "!"

    events = []
    async for event in sse_stream_generator(
        first_chunk_text="Olá",
        stream_iterator=sample_stream(),
        request_id="req-abc",
        model="qwen2.5-coder:1.5b"
    ):
        events.append(event)

    assert len(events) == 6
    assert '"role": "assistant"' in events[0]
    assert '"content": "Olá"' in events[1]
    assert '"content": " mundo"' in events[2]
    assert '"content": "!"' in events[3]
    assert '"finish_reason": "stop"' in events[4]
    assert events[5] == "data: [DONE]\n\n"

@pytest.mark.asyncio
async def test_endpoint_streaming_chat_completions():
    """Testa chamada E2E com stream=True no endpoint /v1/chat/completions."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url=TEST_BASE_URL, headers=AUTH_HEADERS) as client:
        payload = {
            "model": "qwen2.5-coder:1.5b",
            "messages": [
                {"role": "user", "content": "Olá em streaming!"}
            ],
            "stream": True
        }
        resp = await client.post("/v1/chat/completions", json=payload)
        # Se o Ollama local estiver ativo ou simulado, retorna stream 200
        assert resp.status_code in (200, 502, 504)
        if resp.status_code == 200:
            assert "text/event-stream" in resp.headers.get("content-type", "")
