"""Roteador completo: cofre de chaves (DPAPI), enderecamento provedor/modelo, contador de uso, descoberta de modelos."""
import json
import sqlite3
import sys

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from app.config_manager import ConfigManager
from app.gateway.factory import build_gateway_adapters
from app.gateway.providers import BaseGatewayAdapter, GatewayProviderError
from app.gateway.schemas import ChatCompletionRequest, ChatMessage
from app.gateway.secret_store import SecretStore, SecretStoreError
from app.gateway.tier_router import TierRouter
from app.gateway.usage import UsageLedger
from helpers import AUTH_HEADERS, TEST_BASE_URL
from test_router_embutido import Fake, reply

KEY = "gsk_" + "Z9y8X7w6V5u4T3s2R1q0" * 2


# --------------------------------------------------------------------------- cofre
@pytest.mark.parametrize("dpapi", [False, True] if sys.platform == "win32" else [False])
def test_cofre_guarda_e_le_sem_deixar_a_chave_em_texto_puro(tmp_path, dpapi):
    store = SecretStore(tmp_path / "k.json", use_dpapi=dpapi)
    assert store.set("groq", [KEY, KEY, "outra-chave-12345"]) == 2           # dedup
    assert store.get("groq") == [KEY, "outra-chave-12345"] and store.count("groq") == 2
    on_disk = (tmp_path / "k.json").read_text(encoding="utf-8")
    if dpapi:
        assert KEY not in on_disk and "Z9y8X7w6V5u4T3s2R1q0" not in on_disk
    assert store.names() == ["groq"]
    store.delete("groq")
    assert store.get("groq") == []


@pytest.mark.parametrize("bad", ["", "curta", "tem espaco no meio 1234", "x" * 600, "quebra\nlinha1234"])
def test_cofre_recusa_chave_invalida(tmp_path, bad):
    with pytest.raises(SecretStoreError):
        SecretStore(tmp_path / "k.json", use_dpapi=False).set("groq", [bad])


def test_cofre_ilegivel_vira_sem_chave_e_nao_derruba(tmp_path):
    path = tmp_path / "k.json"
    path.write_text("{isso nao e json", encoding="utf-8")
    store = SecretStore(path, use_dpapi=False)
    assert store.get("groq") == [] and store.names() == []
    path.write_text(json.dumps({"groq": "!!nao-e-base64!!"}), encoding="utf-8")
    assert store.get("groq") == []


def test_fabrica_usa_chave_do_cofre_sem_variavel_de_ambiente(tmp_path):
    store = SecretStore(tmp_path / "k.json", use_dpapi=False)
    cfg = {"engines": {}, "providers": {"groq": {"enabled": True, "tier": "cloud_free", "base_url": "https://api.groq.com/openai/v1",
                                                  "default_model": "llama", "api_key_env": ""}}}
    assert [a.name for a in build_gateway_adapters(cfg, environ={}, secrets=store)] == ["ollama"]
    store.set("groq", [KEY])
    assert [a.name for a in build_gateway_adapters(cfg, environ={}, secrets=store)] == ["ollama", "groq"]


# --------------------------------------------------------------------------- enderecamento provedor/modelo
def req(model, stream=False):
    return ChatCompletionRequest(model=model, stream=stream, messages=[ChatMessage(role="user", content="oi")])


@pytest.mark.asyncio
async def test_enderecamento_provedor_barra_modelo():
    a, b = Fake("groq", models=("llama",)), Fake("deepseek", models=("deepseek-chat",))
    r = TierRouter(adapters=[a, b])
    resp = await r.route_and_execute(req("deepseek/deepseek-chat"), httpx.AsyncClient())
    assert resp.choices[0].message.content == "de deepseek" and b.seen == ["deepseek-chat"] and a.seen == []


@pytest.mark.asyncio
async def test_modelo_com_barra_declarado_nao_e_confundido_com_enderecamento():
    nv = Fake("nvidia", models=("meta/llama-3.3-70b-instruct",))
    meta = Fake("meta", models=("outro",))                        # um provedor com nome igual ao prefixo
    r = TierRouter(adapters=[nv, meta])
    resp = await r.route_and_execute(req("meta/llama-3.3-70b-instruct"), httpx.AsyncClient())
    assert resp.choices[0].message.content == "de nvidia"


@pytest.mark.asyncio
async def test_enderecamento_com_provedor_que_falha_nao_vaza_para_outro():
    a = Fake("groq", models=("llama",), error=GatewayProviderError("fora", status_code=500, retryable=True))
    b = Fake("deepseek", models=("llama",))
    with pytest.raises(GatewayProviderError):
        await TierRouter(adapters=[a, b]).route_and_execute(req("groq/llama"), httpx.AsyncClient())
    assert b.seen == []


# --------------------------------------------------------------------------- contador de uso
@pytest.mark.asyncio
async def test_contador_registra_sucesso_erro_e_stream_sem_conteudo(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.db")
    ok = Fake("groq", models=("llama",))
    bad = Fake("deepseek", models=("ds",), error=GatewayProviderError("429", status_code=429, retryable=True))
    r = TierRouter(adapters=[ok, bad])
    r.usage = ledger
    await r.route_and_execute(req("groq/llama"), httpx.AsyncClient())
    with pytest.raises(GatewayProviderError):
        await r.route_and_execute(req("deepseek/ds"), httpx.AsyncClient())
    gen = await r.route_and_stream(req("groq/llama", stream=True), httpx.AsyncClient())
    _ = [c async for c in gen]

    summary = ledger.summary(7)
    assert summary["totals"]["requests"] == 3 and summary["totals"]["errors"] == 1
    providers = {row["provider"]: row for row in summary["by_provider"]}
    assert providers["groq"]["requests"] == 2 and providers["deepseek"]["ok"] == 0
    assert len(summary["by_day"]) == 1 and len(summary["recent"]) == 3
    cols = {row[1] for row in sqlite3.connect(tmp_path / "usage.db").execute("PRAGMA table_info(usage)")}
    assert not cols & {"prompt", "content", "messages", "response", "text"}      # so metadados


def test_contador_nunca_levanta(tmp_path):
    ledger = UsageLedger(tmp_path / "usage.db")
    ledger.path = tmp_path / "pasta" / "nao" / "existe" / "x.db"
    ledger.record("a", "m", True)                                                # sqlite falha: engolido


# --------------------------------------------------------------------------- API
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "config_mgr", ConfigManager(config_path=str(tmp_path / "config.yaml")))
    monkeypatch.setattr(server, "SECRET_STORE", SecretStore(tmp_path / "k.json", use_dpapi=False))
    monkeypatch.setattr(server, "USAGE", UsageLedger(tmp_path / "usage.db"))
    monkeypatch.setattr(server.gateway_tier_router, "adapters", list(server.gateway_tier_router.adapters))
    monkeypatch.setattr(server.gateway_tier_router, "combos", {})
    monkeypatch.setattr(server.gateway_tier_router, "usage", server.USAGE)
    monkeypatch.setattr(server.router, "default_offline", False)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    return server


def client():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS)


def code():
    return server.HUMAN_CODE.current()


@pytest.mark.asyncio
async def test_api_colar_chave_no_painel_ativa_o_provedor(api):
    async with client() as c:
        await c.post("/api/router/providers", json={"preset": "groq", "human_code": code()})
        assert (await c.get("/api/router/state")).json()["providers"][0]["active"] is False
        assert (await c.post("/api/router/providers/groq/keys", json={"keys": [KEY]})).status_code == 403
        assert (await c.post("/api/router/providers/groq/keys", json={"keys": ["curta"], "human_code": code()})).status_code == 400
        assert (await c.post("/api/router/providers/inexistente/keys", json={"keys": [KEY], "human_code": code()})).status_code == 404
        ok = await c.post("/api/router/providers/groq/keys", json={"keys": [KEY], "human_code": code()})
        assert ok.status_code == 200 and ok.json() == {"status": "ok", "keys_stored": 1}
        state = (await c.get("/api/router/state")).json()
        prov = state["providers"][0]
        assert prov["active"] is True and prov["keys_stored"] == 1 and prov["keys_env"] == 0
        assert KEY not in json.dumps(state) and "Z9y8X7w6V5u4T3s2R1q0" not in json.dumps(state)
        assert (await c.delete("/api/router/providers/groq/keys")).status_code == 200
        assert (await c.get("/api/router/state")).json()["providers"][0]["active"] is False


@pytest.mark.asyncio
async def test_api_remover_provedor_apaga_as_chaves(api):
    async with client() as c:
        await c.post("/api/router/providers", json={"preset": "groq", "human_code": code()})
        await c.post("/api/router/providers/groq/keys", json={"keys": [KEY], "human_code": code()})
        assert server.SECRET_STORE.count("groq") == 1
        assert (await c.delete("/api/router/providers/groq")).status_code == 200
    assert server.SECRET_STORE.count("groq") == 0


@pytest.mark.asyncio
async def test_api_provedor_personalizado_sem_variavel_de_ambiente(api):
    body = {"name": "meuprov", "base_url": "https://api.exemplo.com/v1", "default_model": "m1", "key_env": "", "human_code": code()}
    async with client() as c:
        r = await c.post("/api/router/providers", json=body)
        assert r.status_code == 200 and r.json()["provider"]["key_env"] == ""


@pytest.mark.asyncio
async def test_api_descobrir_modelos_grava_a_lista(api, monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "m-a"}, {"id": "models/m-b"}, {"id": "m-a"}, {"id": "x\x00y"}]})

    monkeypatch.setattr(server, "make_safe_async_client", lambda timeout=20.0: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    async with client() as c:
        await c.post("/api/router/providers", json={"preset": "groq", "human_code": code()})
        assert (await c.post("/api/router/providers/groq/discover")).status_code == 404       # ainda sem chave
        await c.post("/api/router/providers/groq/keys", json={"keys": [KEY], "human_code": code()})
        r = (await c.post("/api/router/providers/groq/discover")).json()
        assert r["ok"] and r["models"] == ["m-a", "m-b"]
        assert seen["auth"] == f"Bearer {KEY}" and seen["url"].endswith("/openai/v1/models")
        assert (await c.get("/api/router/state")).json()["providers"][0]["models"] == ["m-a", "m-b"]


@pytest.mark.asyncio
async def test_api_uso_e_modelos_enderecaveis(api, monkeypatch):
    fake = Fake("groq", models=("llama",))
    monkeypatch.setattr(server.gateway_tier_router, "adapters", [fake])
    server.USAGE.record("groq", "llama", True, 200, 5, 7, 120)
    async with client() as c:
        usage = (await c.get("/api/router/usage?days=7")).json()
        assert usage["totals"]["requests"] == 1 and usage["totals"]["completion_tokens"] == 7
        ids = {m["id"] for m in (await c.get("/v1/models")).json()["data"]}
        assert {"llama", "groq/llama"} <= ids
