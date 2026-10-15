"""Responses API da OpenAI (`/v1/responses`) sobre o roteador do Hera."""
import json

import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from app.config_manager import ConfigManager
from app.gateway.openai_responses import error_body, responses_body_to_chat
from app.gateway.secret_store import SecretStore
from app.gateway.translator.responses_api import (
    chat_stream_to_responses, chat_to_responses_response, flush_responses_stream, new_responses_stream_state,
    responses_to_chat_request,
)
from app.gateway.usage import UsageLedger
from helpers import AUTH_HEADERS, TEST_BASE_URL
from test_router_embutido import Fake


def _chat(body):
    return responses_to_chat_request("m", body, False)


# ── requisição ──

def test_input_string_e_instructions():
    c = _chat({"input": "oi", "instructions": "seja breve", "max_output_tokens": 30, "store": False})
    assert c["messages"] == [{"role": "system", "content": "seja breve"},
                             {"role": "user", "content": [{"type": "text", "text": "oi"}]}]
    assert c["max_tokens"] == 30 and "store" not in c and "input" not in c and "max_output_tokens" not in c


def test_input_vazio_vira_placeholder():
    assert _chat({"input": []})["messages"][0]["content"][0]["text"] == "..."
    assert _chat({"input": "  "})["messages"][0]["content"][0]["text"] == "..."


def test_developer_vira_system_e_imagem_vira_image_url():
    c = _chat({"input": [
        {"role": "developer", "content": [{"type": "input_text", "text": "regra"}]},
        {"type": "message", "role": "user", "content": [{"type": "input_image", "image_url": "data:image/png;base64,AA"}]}]})
    assert c["messages"][0]["role"] == "system"
    assert c["messages"][1]["content"][0] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA", "detail": "auto"}}


def test_function_call_e_output_viram_tool_calls_e_tool():
    c = _chat({"input": [
        {"type": "message", "role": "user", "content": "x"},
        {"type": "reasoning", "summary": [{"type": "summary_text", "text": "penso"}]},
        {"type": "function_call", "call_id": "c1", "name": "ler", "arguments": '{"a":1}'},
        {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        {"type": "function_call", "name": "  ", "call_id": "c2", "arguments": "{}"}]})
    roles = [m["role"] for m in c["messages"]]
    assert roles == ["user", "assistant", "tool"]
    assert c["messages"][1]["tool_calls"][0] == {"id": "c1", "type": "function",
                                                 "function": {"name": "ler", "arguments": '{"a":1}'}}
    assert c["messages"][1]["reasoning_content"] == "penso"                  # raciocínio gruda no turno do assistente
    assert c["messages"][2] == {"role": "tool", "tool_call_id": "c1", "content": "ok"}


def test_ferramentas_function_custom_e_hospedada():
    c = _chat({"input": "x", "tools": [
        {"type": "function", "name": "f", "description": "d", "parameters": {"type": "object"}},
        {"type": "custom", "name": "apply_patch", "description": "edita", "format": {"syntax": "lark", "definition": "x"}},
        {"type": "web_search_preview"}]})
    names = [t["function"]["name"] for t in c["tools"]]
    assert names == ["f", "apply_patch"]                                     # ferramenta hospedada (sem nome) é descartada
    assert c["tools"][0]["function"]["parameters"] == {"type": "object", "properties": {}}
    assert c["tools"][1]["function"]["parameters"]["required"] == ["input"]
    assert c["_custom_tool_names"] == ["apply_patch"]


def test_effort_do_reasoning_e_input_invalido():
    assert _chat({"input": "x", "reasoning": {"effort": "high"}})["reasoning_effort"] == "high"
    with pytest.raises(ValueError):
        _chat({"input": 123})
    with pytest.raises(ValueError):
        responses_body_to_chat({"input": "x"})                              # sem model


# ── resposta completa ──

def test_resposta_completa_texto_ferramenta_custom_e_uso():
    r = chat_to_responses_response({"id": "chatcmpl-1", "model": "m", "created": 5, "usage": {
        "prompt_tokens": 10, "completion_tokens": 4, "prompt_tokens_details": {"cached_tokens": 3}},
        "choices": [{"message": {"content": "olá", "reasoning_content": "hm", "tool_calls": [
            {"id": "a", "function": {"name": "f", "arguments": '{"x":1}'}},
            {"id": "b", "function": {"name": "apply_patch", "arguments": '{"input":"PATCH"}'}}]}}]}, ["apply_patch"])
    assert r["id"] == "resp_chatcmpl-1" and r["status"] == "completed" and r["object"] == "response"
    assert [o["type"] for o in r["output"]] == ["reasoning", "message", "function_call", "custom_tool_call"]
    assert r["output"][1]["content"][0]["text"] == "olá"
    assert r["output"][2]["arguments"] == '{"x":1}' and r["output"][3]["input"] == "PATCH"
    assert r["usage"] == {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14,
                          "input_tokens_details": {"cached_tokens": 3}}


# ── stream ──

def _chunk(delta=None, finish=None, usage=None, cid="chatcmpl-x"):
    c = {"id": cid, "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}]}
    if usage:
        c["usage"] = usage
    return c


def _run(chunks, custom=None):
    st, out = new_responses_stream_state(custom), []
    for c in chunks:
        out.extend(chat_stream_to_responses(c, st))
    out.extend(chat_stream_to_responses(None, st))
    return out


def test_stream_texto_ordem_dos_eventos_e_completed_com_output():
    ev = _run([_chunk({"role": "assistant"}), _chunk({"content": "ol"}), _chunk({"content": "á"}),
               _chunk({}, "stop", usage={"prompt_tokens": 5, "completion_tokens": 2})])
    names = [e["event"] for e in ev]
    assert names[:2] == ["response.created", "response.in_progress"]
    assert names.index("response.output_item.added") < names.index("response.output_text.delta") < \
        names.index("response.output_text.done") < names.index("response.completed")
    assert names.count("response.completed") == 1 and names[-1] == "response.completed"
    done = ev[-1]["data"]["response"]
    assert done["output"][0]["content"][0]["text"] == "olá" and done["usage"]["total_tokens"] == 7
    seqs = [e["data"]["sequence_number"] for e in ev]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)               # sequence_number cresce sem repetir


def test_stream_usage_em_trailer_adia_o_completed():
    ev = _run([_chunk({"content": "x"}), _chunk({}, "stop"), {"id": "chatcmpl-x", "choices": [],
                                                              "usage": {"prompt_tokens": 3, "completion_tokens": 1}}])
    completed = [e for e in ev if e["event"] == "response.completed"]
    assert len(completed) == 1 and completed[0]["data"]["response"]["usage"]["total_tokens"] == 4


def test_stream_sem_usage_conclui_no_flush():
    ev = _run([_chunk({"content": "x"}), _chunk({}, "stop")])
    assert [e["event"] for e in ev].count("response.completed") == 1
    assert "usage" not in ev[-1]["data"]["response"]


def test_stream_ferramenta_function_e_custom():
    ev = _run([_chunk({"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "f", "arguments": '{"a"'}}]}),
               _chunk({"tool_calls": [{"index": 0, "function": {"arguments": ":1}"}}]}),
               _chunk({"tool_calls": [{"index": 1, "id": "c2", "function": {"name": "apply_patch", "arguments": '{"input":"P"}'}}]}),
               _chunk({}, "tool_calls", usage={"prompt_tokens": 1, "completion_tokens": 1})], custom=["apply_patch"])
    names = [e["event"] for e in ev]
    assert "".join(e["data"]["delta"] for e in ev if e["event"] == "response.function_call_arguments.delta") == '{"a":1}'
    assert next(e for e in ev if e["event"] == "response.function_call_arguments.done")["data"]["arguments"] == '{"a":1}'
    cust = next(e for e in ev if e["event"] == "response.custom_tool_call_input.done")
    assert cust["data"]["input"] == "P"                                       # embrulho {"input":...} é aberto
    out = ev[-1]["data"]["response"]["output"]
    assert [o["type"] for o in out] == ["function_call", "custom_tool_call"] and names[-1] == "response.completed"


def test_stream_raciocinio_antes_do_texto_e_tags_think():
    ev = _run([_chunk({"reasoning_content": "pen"}), _chunk({"content": "resp"}), _chunk({}, "stop")])
    names = [e["event"] for e in ev]
    assert names.index("response.reasoning_summary_text.done") < names.index("response.output_text.delta")
    ev2 = _run([_chunk({"content": "<think>a"}), _chunk({"content": "b</think>fim"}), _chunk({}, "stop")])
    assert "".join(e["data"]["delta"] for e in ev2 if e["event"] == "response.reasoning_summary_text.delta") == "ab"
    assert "".join(e["data"]["delta"] for e in ev2 if e["event"] == "response.output_text.delta") == "fim"


def test_flush_repetido_nao_duplica():
    st = new_responses_stream_state()
    chat_stream_to_responses(_chunk({"content": "x"}), st)
    assert len(flush_responses_stream(st)) > 0 and flush_responses_stream(st) == []


# ── rota ──

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "config_mgr", ConfigManager(config_path=str(tmp_path / "config.yaml")))
    monkeypatch.setattr(server, "SECRET_STORE", SecretStore(tmp_path / "s.json", use_dpapi=False))
    monkeypatch.setattr(server, "USAGE", UsageLedger(tmp_path / "usage.db"))
    monkeypatch.setattr(server.gateway_tier_router, "adapters", [Fake("groq", models=("llama",))])
    monkeypatch.setattr(server.gateway_tier_router, "usage", server.USAGE)
    monkeypatch.setattr(server.gateway_tier_router, "aliases", {})
    monkeypatch.setattr(server.router, "default_offline", False)
    monkeypatch.setitem(server.RUNTIME_FLAGS, "rtk", False)
    created = []
    yield created
    for key_id in created:
        server.CLIENT_KEYS.delete(key_id)


def master():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS)


@pytest.mark.asyncio
async def test_rota_responses_nao_streaming(api):
    async with master() as c:
        r = await c.post("/v1/responses", json={"model": "llama", "input": "oi", "instructions": "breve"})
    assert r.status_code == 200
    d = r.json()
    assert d["object"] == "response" and d["status"] == "completed" and d["model"] == "llama"
    assert d["output"][0]["content"][0]["text"] == "de groq"


@pytest.mark.asyncio
async def test_rota_responses_streaming(api):
    async with master() as c:
        r = await c.post("/v1/responses", json={"model": "llama", "input": "oi", "stream": True})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(b.split("data: ", 1)[1]) for b in r.text.split("\n\n") if "data: " in b]
    types = [e["type"] for e in events]
    assert types[0] == "response.created" and types[-1] == "response.completed"
    assert "".join(e["delta"] for e in events if e["type"] == "response.output_text.delta") == "de groq"
    assert "event: response.output_text.delta" in r.text


@pytest.mark.asyncio
async def test_rota_responses_erros_no_formato_openai(api):
    async with master() as c:
        bad = await c.post("/v1/responses", json={"model": "llama"})          # sem input
        assert bad.status_code == 400 and bad.json()["error"]["type"] == "invalid_request_error"
        nomodel = await c.post("/v1/responses", json={"input": "x"})
        assert nomodel.status_code == 400
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL) as anon:
        assert (await anon.post("/v1/responses", json={"model": "llama", "input": "x"})).status_code == 401


@pytest.mark.asyncio
async def test_rota_responses_com_chave_de_cliente(api):
    async with master() as c:
        made = (await c.post("/api/router/clients", json={"name": "codex", "models": ["llama"],
                                                          "human_code": server.HUMAN_CODE.current()})).json()
        api.append(made["id"])
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL,
                           headers={"Authorization": f"Bearer {made['key']}"}) as k:
        assert (await k.post("/v1/responses", json={"model": "llama", "input": "oi"})).status_code == 200
        denied = await k.post("/v1/responses", json={"model": "outro", "input": "oi"})
        assert denied.status_code == 403 and denied.json()["error"]["type"] == "permission_denied"
        assert (await k.get("/api/config")).status_code == 401


def test_error_body_formato():
    assert error_body(429, "x")["error"]["type"] == "rate_limit_exceeded"
