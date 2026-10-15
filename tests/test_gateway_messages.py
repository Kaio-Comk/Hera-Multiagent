"""Rota `/v1/messages` (formato Anthropic) sobre o roteador do Hera."""
import json

import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from app.config_manager import ConfigManager
from app.gateway.anthropic_api import chat_response_to_claude, claude_body_to_chat, error_body
from app.gateway.secret_store import SecretStore
from app.gateway.usage import UsageLedger
from helpers import AUTH_HEADERS, TEST_BASE_URL
from test_router_embutido import Fake


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


BODY = {"model": "llama", "max_tokens": 50, "system": "seja breve", "messages": [{"role": "user", "content": "oi"}]}


@pytest.mark.asyncio
async def test_messages_nao_streaming_devolve_mensagem_do_claude(api):
    async with master() as c:
        r = await c.post("/v1/messages", json=BODY)
    assert r.status_code == 200
    d = r.json()
    assert d["type"] == "message" and d["role"] == "assistant" and d["model"] == "llama"
    assert d["content"] == [{"type": "text", "text": "de groq"}]
    assert d["stop_reason"] == "end_turn" and set(d["usage"]) >= {"input_tokens", "output_tokens"}


@pytest.mark.asyncio
async def test_messages_streaming_emite_eventos_do_claude(api):
    async with master() as c:
        r = await c.post("/v1/messages", json={**BODY, "stream": True})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = [json.loads(b.split("data: ", 1)[1]) for b in r.text.split("\n\n") if "data: " in b]
    assert [e["type"] for e in events][0] == "message_start" and events[-1]["type"] == "message_stop"
    assert "".join(e["delta"]["text"] for e in events if e.get("delta", {}).get("type") == "text_delta") == "de groq"
    assert "event: message_start" in r.text and "event: content_block_delta" in r.text
    assert any(e["type"] == "message_delta" and e["delta"]["stop_reason"] == "end_turn" for e in events)


@pytest.mark.asyncio
async def test_messages_corpo_invalido_vira_erro_no_formato_anthropic(api):
    async with master() as c:
        for bad in ({"model": "llama", "messages": [{"role": "user", "content": "x"}]},        # sem max_tokens
                    {"model": "llama", "max_tokens": 5, "messages": []},
                    {"max_tokens": 5, "messages": [{"role": "user", "content": "x"}]}):
            r = await c.post("/v1/messages", json=bad)
            assert r.status_code == 400 and r.json()["type"] == "error"
            assert r.json()["error"]["type"] == "invalid_request_error"
        r = await c.post("/v1/messages", content=b"isso nao e json", headers={"content-type": "application/json"})
        assert r.status_code == 400 and r.json()["type"] == "error"


@pytest.mark.asyncio
async def test_messages_sem_token_e_negado(api):
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL) as c:
        assert (await c.post("/v1/messages", json=BODY)).status_code == 401


@pytest.mark.asyncio
async def test_messages_com_chave_de_cliente_respeita_modelos(api):
    async with master() as c:
        made = (await c.post("/api/router/clients", json={"name": "claude-code", "models": ["llama"],
                                                          "human_code": server.HUMAN_CODE.current()})).json()
        api.append(made["id"])
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL,
                           headers={"Authorization": f"Bearer {made['key']}"}) as k:
        assert (await k.post("/v1/messages", json=BODY)).status_code == 200
        denied = await k.post("/v1/messages", json={**BODY, "model": "outro"})
        assert denied.status_code == 403 and denied.json()["type"] == "error"
        assert denied.json()["error"]["type"] == "permission_error"
        assert (await k.get("/api/config")).status_code == 401           # a chave segue sem acesso à gestão


def test_conversao_do_corpo_e_da_resposta():
    chat = claude_body_to_chat({**BODY, "top_p": 0.5, "stop_sequences": ["FIM"], "temperature": 3,
                                "tools": [{"name": "f", "description": "d", "input_schema": {"type": "object"}},
                                          {"type": "web_search_20250305", "name": "web_search"}]})
    assert chat["messages"][0] == {"role": "system", "content": "seja breve"}
    assert chat["top_p"] == 0.5 and chat["stop"] == ["FIM"] and chat["temperature"] == 2.0
    assert [t["function"]["name"] for t in chat["tools"]] == ["f"]        # ferramenta nativa não tem equivalente
    msg = chat_response_to_claude({"id": "chatcmpl-1", "model": "m", "usage": {
        "prompt_tokens": 10, "completion_tokens": 3, "prompt_tokens_details": {"cached_tokens": 4}},
        "choices": [{"finish_reason": "tool_calls", "message": {
            "content": "vou", "reasoning_content": "hm", "tool_calls": [
                {"id": "t", "function": {"name": "f", "arguments": '{"a": 1}'}}]}}]})
    assert [b["type"] for b in msg["content"]] == ["thinking", "text", "tool_use"]
    assert msg["content"][2]["input"] == {"a": 1} and msg["stop_reason"] == "tool_use"
    assert msg["usage"] == {"input_tokens": 6, "output_tokens": 3, "cache_read_input_tokens": 4}
    assert error_body(429, "x")["error"]["type"] == "rate_limit_error"


@pytest.mark.asyncio
async def test_messages_e_responses_aceitam_contexto_grande(api):
    """O limite global de corpo e 1 MiB; estas rotas precisam do mesmo teto folgado do chat (contexto inteiro)."""
    big = "x" * (2 * 1024 * 1024)
    async with master() as c:
        m = await c.post("/v1/messages", json={**BODY, "messages": [{"role": "user", "content": big}]})
        assert m.status_code == 200, m.text[:200]
        r = await c.post("/v1/responses", json={"model": "llama", "input": big})
        assert r.status_code == 200, r.text[:200]
        too = await c.post("/v1/messages", json={**BODY, "messages": [{"role": "user", "content": "y" * (9 * 1024 * 1024)}]})
        assert too.status_code == 413
