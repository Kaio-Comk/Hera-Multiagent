"""Gemini 3 exige a `thought_signature` de volta ao reenviar uma chamada de ferramenta. Ela viaja em
`tool_calls[].extra_content.google.thought_signature`; o gateway nao pode descarta-la em nenhum ponto."""
import json

import pytest

from app.gateway.schemas import ChatCompletionResponse, ChatMessage, NormalizedDelta
from app.gateway.streaming import openai_sse_deltas

SIG = {"google": {"thought_signature": "ASSINATURA-OPACA"}}


def test_mensagem_de_assistente_preserva_extra_content_ao_ir_para_o_provedor():
    msg = ChatMessage.model_validate({"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}, "extra_content": SIG}]})
    assert msg.model_dump(exclude_none=True)["tool_calls"][0]["extra_content"] == SIG


def test_sem_extra_content_nao_aparece_null_para_outros_provedores():
    msg = ChatMessage.model_validate({"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]})
    assert "extra_content" not in msg.model_dump(exclude_none=True)["tool_calls"][0]


def test_resposta_nao_streaming_preserva_extra_content():
    resp = ChatCompletionResponse.model_validate({"id": "x", "created": 1, "model": "m", "choices": [{"index": 0, "finish_reason": "tool_calls",
        "message": {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{}"}, "extra_content": SIG}]}}]})
    assert resp.model_dump(exclude_none=True)["choices"][0]["message"]["tool_calls"][0]["extra_content"] == SIG


async def _linhas(*chunks):
    for c in chunks:
        yield "data: " + json.dumps(c)
    yield "data: [DONE]"


@pytest.mark.asyncio
async def test_delta_de_streaming_carrega_extra_content():
    chunk = {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
             "function": {"name": "f", "arguments": "{}"}, "extra_content": SIG}]}}]}
    deltas = [d async for d in openai_sse_deltas(_linhas(chunk), "gemini", "m", "req-1")]
    assert deltas[0].tool_extra_content == SIG


def test_normalized_delta_sem_extra_continua_valido():
    assert NormalizedDelta(request_id="r", provider="p", model="m").tool_extra_content is None
