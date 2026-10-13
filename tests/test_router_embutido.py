"""Roteador embutido: combos com fallback, API de provedores (codigo humano, validacao), motor hera_router."""
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from adapters.hera_router import HeraRouterAdapter
from app.config_manager import ConfigManager
from app.gateway.providers import BaseGatewayAdapter, GatewayProviderError
from app.gateway.schemas import ChatCompletionRequest, ChatCompletionResponse, ChatMessage
from app.gateway.tier_router import TierRouter
from app.router import TaskRouter
from models.task import AgentTask, TaskStatus
from helpers import AUTH_HEADERS, TEST_BASE_URL


def reply(model, text="ok"):
    return ChatCompletionResponse.model_validate({
        "id": "c1", "object": "chat.completion", "created": 1, "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})


class Fake(BaseGatewayAdapter):
    def __init__(self, name, tier="cloud_cheap", local=False, models=("m",), error=None):
        self.name, self.tier, self.is_local = name, tier, local
        self.served_models = frozenset(models)
        self.default_model = models[0]
        self.error = error
        self.seen = []

    async def chat_complete(self, request, client):
        self.seen.append(request.model)
        if self.error:
            raise self.error
        return reply(request.model, f"de {self.name}")


def req(model="meu-combo", text="oi", stream=False):
    return ChatCompletionRequest(model=model, stream=stream, messages=[ChatMessage(role="user", content=text)])


def make_router(*adapters):
    r = TierRouter(adapters=list(adapters))
    r.combos = {"meu-combo": [{"provider": "a", "model": "ma"}, {"provider": "b", "model": "mb"}]}
    return r


CLIENT = httpx.AsyncClient()


@pytest.mark.asyncio
async def test_combo_cai_para_o_proximo_passo_com_o_modelo_do_passo():
    a = Fake("a", models=("ma",), error=GatewayProviderError("429", status_code=429, retryable=True))
    b = Fake("b", models=("mb",))
    resp = await make_router(a, b).route_and_execute(req(), CLIENT)
    assert resp.choices[0].message.content == "de b" and b.seen == ["mb"] and a.seen == ["ma"]


@pytest.mark.asyncio
async def test_combo_passo_forcado_nao_vaza_para_outro_provedor():
    a = Fake("a", models=("ma",), error=GatewayProviderError("fora do ar", status_code=500, retryable=True))
    c = Fake("c", models=("mc",))                       # nao esta no combo: nunca pode ser usado
    r = TierRouter(adapters=[a, c])
    r.combos = {"meu-combo": [{"provider": "a", "model": "ma"}]}
    with pytest.raises(GatewayProviderError):
        await r.route_and_execute(req(), CLIENT)
    assert c.seen == []


@pytest.mark.asyncio
async def test_combo_com_segredo_pula_a_nuvem_e_termina_no_local():
    secret = "sk-" + "abcdef0123456789" * 2
    cloud = Fake("a", models=("ma",))
    local = Fake("ollama", tier="local", local=True, models=("q",))
    r = TierRouter(adapters=[cloud, local])
    r.combos = {"meu-combo": [{"provider": "a", "model": "ma"}, {"provider": "ollama", "model": "q"}]}
    resp = await r.route_and_execute(req(text=f"minha chave {secret}"), CLIENT)
    assert resp.choices[0].message.content == "de ollama" and cloud.seen == []


@pytest.mark.asyncio
async def test_combo_so_de_nuvem_com_segredo_e_bloqueado():
    secret = "sk-" + "abcdef0123456789" * 2
    r = make_router(Fake("a", models=("ma",)), Fake("b", models=("mb",)))
    with pytest.raises(GatewayProviderError):
        await r.route_and_execute(req(text=f"chave {secret}"), CLIENT)


@pytest.mark.asyncio
async def test_combo_no_stream_tambem_tem_fallback():
    a = Fake("a", models=("ma",), error=GatewayProviderError("429", status_code=429, retryable=True))
    b = Fake("b", models=("mb",))
    gen = await make_router(a, b).route_and_stream(req(stream=True), CLIENT)
    body = "".join([chunk async for chunk in gen])
    assert "de b" in body and "[DONE]" in body


# --------------------------------------------------------------------------- motor hera_router
@pytest.mark.asyncio
async def test_motor_hera_router_sucesso_e_erro_sanitizado():
    async def ok(request):
        assert request.model == "meu-combo"
        return reply("mb", "resposta")

    async def boom(request):
        raise GatewayProviderError("falhou com sk-" + "abcdef0123456789" * 2, status_code=502)

    task = AgentTask(id="t1", title="x", instruction="faz", assigned_model="meu-combo")
    good = HeraRouterAdapter(call=ok, models=lambda: ["meu-combo"], has_cloud=lambda: True)
    res = await good.collect(await good.submit(task))
    assert res.status == TaskStatus.SUCCEEDED and res.output_text == "resposta"
    assert "meu-combo" in (await good.health_check()).available_models

    bad = HeraRouterAdapter(call=boom, models=lambda: [], has_cloud=lambda: True)
    res = await bad.collect(await bad.submit(task))
    assert res.status == TaskStatus.FAILED and "abcdef0123456789" not in (res.error_message or "")


@pytest.mark.asyncio
async def test_roteador_de_tarefas_prefere_hera_router_so_com_nuvem():
    async def call(request):
        return reply("m")

    class Local:
        name = "ollama"
        default_model = "q"

    for has_cloud, expected in ((True, "hera_router"), (False, "ollama")):
        hera = HeraRouterAdapter(call=call, models=lambda: [], has_cloud=lambda hc=has_cloud: hc)
        router = TaskRouter(adapters={"ollama": Local(), "hera_router": hera})
        adapter, _, _ = await router.route(AgentTask(id="t", title="x", instruction="resuma", task_type="research"))
        assert adapter.name == expected


# --------------------------------------------------------------------------- API
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "config_mgr", ConfigManager(config_path=str(tmp_path / "config.yaml")))
    monkeypatch.setattr(server.gateway_tier_router, "adapters", list(server.gateway_tier_router.adapters))
    monkeypatch.setattr(server.gateway_tier_router, "combos", {})
    monkeypatch.setattr(server.router, "default_offline", False)
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "x" * 30)
    return server


def client():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS)


def code():
    return server.HUMAN_CODE.current()


@pytest.mark.asyncio
async def test_api_adicionar_provedor_exige_codigo_humano(api):
    async with client() as c:
        r = await c.post("/api/router/providers", json={"preset": "groq"})
        assert r.status_code == 403 and r.json()["detail"]["error"] == "human_code_required"
        r = await c.post("/api/router/providers", json={"preset": "groq", "human_code": code()})
        assert r.status_code == 200
        view = r.json()["provider"]
        assert view["enabled"] and view["active"] and view["keys_found"] == 1
        state = (await c.get("/api/router/state")).json()
    assert "gsk_" not in json.dumps(state)                      # o segredo nunca sai
    assert any(a.name == "groq" for a in server.gateway_tier_router.adapters)


@pytest.mark.asyncio
async def test_api_provedor_sem_chave_fica_inativo(api, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY")
    async with client() as c:
        r = await c.post("/api/router/providers", json={"preset": "groq", "human_code": code()})
    assert r.json()["provider"]["active"] is False and r.json()["provider"]["keys_found"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [
    {"key_env": "HERA_API_KEY"},                   # nao deixa mandar o token do Hera para fora
    {"key_env": "PATH"},
    {"key_env": "AWS_SECRET_ACCESS_KEY"},
    {"base_url": "http://api.groq.com/openai/v1"},  # so https
    {"base_url": "https://127.0.0.1/v1"},
    {"base_url": "https://localhost/v1"},
    {"name": "ollama"},
    {"name": "Nome Ruim!"},
    {"tier": "gratis"},
])
async def test_api_provedor_rejeita_entradas_perigosas(api, patch):
    body = {"preset": "groq", "human_code": code(), **patch}
    async with client() as c:
        r = await c.post("/api/router/providers", json=body)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_router_config"
    assert [a.name for a in server.gateway_tier_router.adapters] == ["ollama"]


@pytest.mark.asyncio
async def test_api_ligar_pede_codigo_e_desligar_nao(api):
    async with client() as c:
        await c.post("/api/router/providers", json={"preset": "groq", "enabled": False, "human_code": code()})
        assert (await c.post("/api/router/providers/groq/enabled", json={"enabled": True})).status_code == 403
        ok = await c.post("/api/router/providers/groq/enabled", json={"enabled": True, "human_code": code()})
        assert ok.status_code == 200 and ok.json()["provider"]["active"] is True
        off = await c.post("/api/router/providers/groq/enabled", json={"enabled": False})
        assert off.status_code == 200 and off.json()["provider"]["active"] is False


@pytest.mark.asyncio
async def test_api_combos_validam_provedores_e_travam_exclusao(api):
    async with client() as c:
        await c.post("/api/router/providers", json={"preset": "groq", "human_code": code()})
        good = {"combos": {"rapido": [{"provider": "groq", "model": "llama-3.3-70b-versatile"},
                                      {"provider": "ollama", "model": "qwen2.5-coder:1.5b"}]}}
        r = await c.put("/api/router/combos", json=good)
        assert r.status_code == 200 and "rapido" in server.gateway_tier_router.combos
        assert (await c.put("/api/router/combos", json={"combos": {"x1": [{"provider": "fantasma", "model": "m"}]}})).status_code == 400
        assert (await c.put("/api/router/combos", json={"combos": {"default": [{"provider": "groq", "model": "m"}]}})).status_code == 400
        assert (await c.put("/api/router/combos", json={"combos": {"groq": [{"provider": "groq", "model": "m"}]}})).status_code == 400
        assert (await c.put("/api/router/combos", json={"combos": {"vazio": []}})).status_code == 400
        assert (await c.delete("/api/router/providers/groq")).status_code == 409      # esta num combo
        await c.put("/api/router/combos", json={"combos": {}})
        assert (await c.delete("/api/router/providers/groq")).status_code == 200


@pytest.mark.asyncio
async def test_api_testar_provedor(api, monkeypatch):
    fake = Fake("groq", models=("llama",))
    monkeypatch.setattr(server.gateway_tier_router, "adapters", [fake])
    async with client() as c:
        ok = (await c.post("/api/router/test", json={"provider": "groq"})).json()
        assert ok["ok"] is True and ok["text"] == "de groq" and fake.seen == ["llama"]
        fake.error = GatewayProviderError("401 chave recusada", status_code=401)
        bad = (await c.post("/api/router/test", json={"provider": "groq"})).json()
        assert bad["ok"] is False and bad["status"] == 401
        assert (await c.post("/api/router/test", json={"provider": "inexistente"})).status_code == 404
