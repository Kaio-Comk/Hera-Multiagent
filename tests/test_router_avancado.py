"""Roteador avancado: estrategias de combo, disjuntor e limites, apelidos, custo, registro, chaves de acesso de
cliente, ajustes, exportar/importar."""
import json
import random
import re
import sqlite3
import time

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from app.config_manager import ConfigManager
from app.gateway.client_keys import ClientKey, ClientKeyStore
from app.gateway.presets import PROVIDER_PRESETS
from app.gateway.provider_policy import validate_provider_url
from app.gateway.providers import GatewayProviderError
from app.gateway.resilience import ProviderGuard
from app.gateway.schemas import ChatCompletionRequest, ChatMessage
from app.gateway.secret_store import SecretStore
from app.gateway.tier_router import CLIENT_NAME, TierRouter
from app.gateway.usage import UsageLedger
from helpers import AUTH_HEADERS, TEST_BASE_URL
from test_router_embutido import Fake


def req(model, text="oi", stream=False):
    return ChatCompletionRequest(model=model, stream=stream, messages=[ChatMessage(role="user", content=text)])


CLIENT = httpx.AsyncClient()


def trio(**errors):
    return [Fake("a", models=("ma",), error=errors.get("a")), Fake("b", models=("mb",), error=errors.get("b")),
            Fake("c", models=("mc",), error=errors.get("c"))]


def combo_router(strategy, adapters=None, weights=None):
    r = TierRouter(adapters=adapters or trio())
    r.combos = {"mix": [{"provider": n, "model": f"m{n}", **({"weight": weights[n]} if weights else {})} for n in "abc"]}
    r.combo_strategies = {"mix": strategy}
    return r


# --------------------------------------------------------------------------- estrategias de combo
@pytest.mark.asyncio
async def test_prioridade_e_sempre_o_primeiro():
    r = combo_router("priority")
    for _ in range(3):
        assert (await r.route_and_execute(req("mix"), CLIENT)).choices[0].message.content == "de a"


@pytest.mark.asyncio
async def test_round_robin_reveza_o_primeiro_passo():
    r = combo_router("round_robin")
    got = [(await r.route_and_execute(req("mix"), CLIENT)).choices[0].message.content for _ in range(4)]
    assert got == ["de a", "de b", "de c", "de a"]


@pytest.mark.asyncio
async def test_round_robin_ainda_tem_fallback():
    r = combo_router("round_robin", trio(a=GatewayProviderError("x", status_code=500, retryable=True)))
    r._rr["mix"] = 0
    assert (await r.route_and_execute(req("mix"), CLIENT)).choices[0].message.content == "de b"   # a falhou -> b


@pytest.mark.asyncio
async def test_mais_rapido_escolhe_pela_latencia_do_historico(tmp_path):
    ledger = UsageLedger(tmp_path / "u.db")
    for provider, ms in (("a", 900), ("b", 120), ("c", 400)):
        for _ in range(3):
            ledger.record(provider, "m", True, 200, 1, 1, ms)
    r = combo_router("fastest")
    r.usage = ledger
    assert (await r.route_and_execute(req("mix"), CLIENT)).choices[0].message.content == "de b"


@pytest.mark.asyncio
async def test_menos_usado_hoje(tmp_path):
    ledger = UsageLedger(tmp_path / "u.db")
    for provider, n in (("a", 5), ("b", 2), ("c", 0)):
        for _ in range(n):
            ledger.record(provider, "m", True, 200, 1, 1, 10)
    r = combo_router("least_used")
    r.usage = ledger
    assert (await r.route_and_execute(req("mix"), CLIENT)).choices[0].message.content == "de c"


@pytest.mark.asyncio
async def test_ponderado_respeita_o_peso(monkeypatch):
    r = combo_router("weighted", weights={"a": 1, "b": 5, "c": 1})
    seen = {}

    def fake_choices(population, weights):
        seen["weights"] = list(weights)
        return [1]                                                     # sorteou o indice 1 (b)
    monkeypatch.setattr(random, "choices", fake_choices)
    assert (await r.route_and_execute(req("mix"), CLIENT)).choices[0].message.content == "de b"
    assert seen["weights"] == [1, 5, 1]


# --------------------------------------------------------------------------- disjuntor e limites
def test_disjuntor_abre_apos_falhas_seguidas_e_dobra_a_pausa():
    clock = [1000.0]
    g = ProviderGuard(threshold=3, clock=lambda: clock[0])
    for _ in range(2):
        g.report_failure("a", "500")
    assert not g.is_paused("a")
    g.report_failure("a", "500")
    assert g.is_paused("a") and g.resume_in("a") == 30
    clock[0] += 31
    assert not g.is_paused("a")
    for _ in range(3):
        g.report_failure("a", "500")
    assert g.resume_in("a") == 60                                          # dobrou
    g.report_success("a")
    assert not g.is_paused("a") and g.status("a")["consecutive_failures"] == 0


def test_erro_do_pedido_nao_conta_para_o_disjuntor():
    g = ProviderGuard(threshold=2)
    for _ in range(5):
        g.report_failure("a", "400 pedido ruim", counts=False)
    assert not g.is_paused("a")


@pytest.mark.asyncio
async def test_provedor_em_pausa_vai_para_o_fim_da_fila():
    a = Fake("a", models=("m",), error=GatewayProviderError("fora", status_code=500, retryable=True))
    b = Fake("b", models=("m",))
    r = TierRouter(adapters=[a, b])
    r.guard.threshold = 2
    for _ in range(2):
        await r.route_and_execute(req("m"), CLIENT)                     # a falha, b responde
    assert r.guard.is_paused("a")
    a.seen.clear()
    await r.route_and_execute(req("m"), CLIENT)
    assert a.seen == []                                                  # a nem foi tentado: b respondeu antes
    b.error = GatewayProviderError("tambem fora", status_code=500, retryable=True)
    with pytest.raises(GatewayProviderError):
        await r.route_and_execute(req("m"), CLIENT)
    assert a.seen == ["m"]                                               # sem opcao, tenta o pausado (ultimo recurso)


@pytest.mark.asyncio
async def test_limite_por_minuto_pula_para_o_proximo():
    a, b = Fake("a", models=("m",)), Fake("b", models=("m",))
    r = TierRouter(adapters=[a, b])
    r.guard.set_limits({"a": {"rpm": 2, "daily_requests": None, "daily_tokens": None}})
    got = [(await r.route_and_execute(req("m"), CLIENT)).choices[0].message.content for _ in range(4)]
    assert got == ["de a", "de a", "de b", "de b"]


@pytest.mark.asyncio
async def test_limite_diario_de_tokens_usa_o_historico(tmp_path):
    ledger = UsageLedger(tmp_path / "u.db")
    ledger.record("a", "m", True, 200, 600, 500, 10)                    # ja gastou 1100 tokens hoje
    a, b = Fake("a", models=("m",)), Fake("b", models=("m",))
    r = TierRouter(adapters=[a, b])
    r.usage = ledger
    r.guard.set_limits({"a": {"rpm": None, "daily_requests": None, "daily_tokens": 1000}})
    assert (await r.route_and_execute(req("m"), CLIENT)).choices[0].message.content == "de b"


@pytest.mark.asyncio
async def test_limite_estourado_num_passo_de_combo_cai_para_o_proximo():
    r = combo_router("priority")
    r.guard.set_limits({"a": {"rpm": 1, "daily_requests": None, "daily_tokens": None}})
    got = [(await r.route_and_execute(req("mix"), CLIENT)).choices[0].message.content for _ in range(2)]
    assert got == ["de a", "de b"]


@pytest.mark.asyncio
async def test_prioridade_dentro_da_camada():
    a, b = Fake("a", models=("m",)), Fake("b", models=("m",))
    r = TierRouter(adapters=[a, b])
    r.priorities = {"b": 1, "a": 50}
    assert (await r.route_and_execute(req("m"), CLIENT)).choices[0].message.content == "de b"


# --------------------------------------------------------------------------- apelidos e custo
@pytest.mark.asyncio
async def test_apelido_aponta_para_combo_ou_provedor_barra_modelo():
    r = combo_router("priority")
    r.aliases = {"rapido": "mix", "direto": "b/mb"}
    assert (await r.route_and_execute(req("rapido"), CLIENT)).choices[0].message.content == "de a"
    assert (await r.route_and_execute(req("direto"), CLIENT)).choices[0].message.content == "de b"


def test_custo_estimado(tmp_path):
    local = Fake("ollama", tier="local", local=True, models=("q",))
    free = Fake("gratis", tier="cloud_free", models=("f",))
    paid = Fake("pago", tier="cloud_premium", models=("p",))
    r = TierRouter(adapters=[local, free, paid])
    r.pricing = {"pago": {"in": 3.0, "out": 15.0}, "pago/caro": {"in": 10.0, "out": 30.0}}
    assert r.price("ollama", "q", 1000, 1000) == 0.0 and r.price("gratis", "f", 1000, 1000) == 0.0
    assert r.price("pago", "p", 1_000_000, 1_000_000) == 18.0
    assert r.price("pago", "caro", 1_000_000, 0) == 10.0                  # preco do modelo vence o do provedor
    assert TierRouter(adapters=[paid]).price("pago", "p", 10, 10) is None   # sem preco cadastrado: desconhecido


@pytest.mark.asyncio
async def test_registro_guarda_cliente_pedido_cadeia_e_custo_sem_segredo(tmp_path):
    ledger = UsageLedger(tmp_path / "u.db")
    secret = "sk-" + "abcdef0123456789" * 2
    a = Fake("a", models=("ma",), error=GatewayProviderError(f"401 chave {secret} recusada", status_code=502, retryable=True))
    b = Fake("b", tier="cloud_free", models=("mb",))
    r = TierRouter(adapters=[a, b])
    r.combos = {"mix": [{"provider": "a", "model": "ma"}, {"provider": "b", "model": "mb"}]}
    r.usage = ledger
    token = CLIENT_NAME.set("cursor")
    try:
        await r.route_and_execute(req("mix"), CLIENT)
    finally:
        CLIENT_NAME.reset(token)
    rows = ledger.logs(10)
    assert len(rows) == 2 and {x["client"] for x in rows} == {"cursor"} and len({x["request_id"] for x in rows}) == 1
    ok_row = next(x for x in rows if x["ok"]) and next(x for x in rows if x["ok"])
    err_row = next(x for x in rows if not x["ok"])
    assert ok_row["provider"] == "b" and ok_row["cost"] == 0.0 and err_row["status"] == 502
    assert secret not in json.dumps(rows) and "abcdef0123456789" not in json.dumps(rows)
    assert ledger.today(client="cursor")["requests"] == 2
    summary = ledger.summary(7)
    assert summary["totals"]["success_rate"] == 50.0 and {c["client"] for c in summary["by_client"]} == {"cursor"}


def test_banco_antigo_e_migrado(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE usage (ts REAL NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL, ok INTEGER NOT NULL, "
               "status INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER, latency_ms INTEGER, stream INTEGER)")
    db.execute("INSERT INTO usage VALUES (?,?,?,?,?,?,?,?,?)", (time.time(), "a", "m", 1, 200, 1, 2, 30, 0))
    db.commit()
    db.close()
    ledger = UsageLedger(path)
    ledger.record("b", "m", True, 200, 1, 1, 5, client="x")
    assert ledger.summary(7)["totals"]["requests"] == 2


# --------------------------------------------------------------------------- chaves de acesso de cliente
def test_chave_so_e_mostrada_uma_vez_e_o_arquivo_guarda_so_o_hash(tmp_path):
    store = ClientKeyStore(tmp_path / "k.json")
    created = store.create("cursor", ["groq/*", "rapido"], rpm=10)
    key = created["key"]
    assert key.startswith("hk-") and "key" not in store.list()[0].public()
    disk = (tmp_path / "k.json").read_text(encoding="utf-8")
    assert key not in disk and key[3:20] not in disk
    ck = store.authenticate(key)
    assert ck is not None and ck.name == "cursor" and ck.allows("groq/llama") and ck.allows("rapido") and not ck.allows("openai/gpt")
    assert store.authenticate(key + "x") is None and store.authenticate("hk-errada") is None and store.authenticate(None) is None


def test_chave_desligada_e_apagada_nao_autentica(tmp_path):
    store = ClientKeyStore(tmp_path / "k.json")
    created = store.create("x")
    store.update(created["id"], enabled=False)
    assert store.authenticate(created["key"]) is None
    store.update(created["id"], enabled=True)
    assert store.authenticate(created["key"]) is not None
    assert store.delete(created["id"]) and store.authenticate(created["key"]) is None


def test_limite_por_minuto_da_chave(tmp_path):
    store = ClientKeyStore(tmp_path / "k.json")
    ck = ClientKey(id="1", name="n", prefix="hk-", rpm=2)
    assert store.over_rpm(ck) is None and store.over_rpm(ck) is None
    assert store.over_rpm(ck) is not None


# --------------------------------------------------------------------------- API
@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "config_mgr", ConfigManager(config_path=str(tmp_path / "config.yaml")))
    monkeypatch.setattr(server, "SECRET_STORE", SecretStore(tmp_path / "s.json", use_dpapi=False))
    monkeypatch.setattr(server, "USAGE", UsageLedger(tmp_path / "usage.db"))
    monkeypatch.setattr(server.gateway_tier_router, "adapters", [Fake("groq", models=("llama",))])
    monkeypatch.setattr(server.gateway_tier_router, "combos", {})
    monkeypatch.setattr(server.gateway_tier_router, "usage", server.USAGE)
    monkeypatch.setattr(server.gateway_tier_router, "aliases", {})
    monkeypatch.setattr(server.gateway_tier_router, "tier_order", list(server.gateway_tier_router.tier_order))
    monkeypatch.setattr(server.router, "default_offline", False)
    monkeypatch.setitem(server.RUNTIME_FLAGS, "rtk", False)
    created = []
    yield created
    for key_id in created:
        server.CLIENT_KEYS.delete(key_id)


def master():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS)


def with_key(key):
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers={"Authorization": f"Bearer {key}"})


def code():
    return server.HUMAN_CODE.current()


@pytest.mark.asyncio
async def test_api_chave_de_cliente_so_abre_o_v1(api):
    async with master() as c:
        made = await c.post("/api/router/clients", json={"name": "cursor", "models": ["llama", "groq/*"], "human_code": code()})
        assert made.status_code == 200
        key = made.json()["key"]
        api.append(made.json()["id"])
    async with with_key(key) as k:
        models = (await k.get("/v1/models")).json()["data"]
        assert {m["id"] for m in models} >= {"llama", "groq/llama"}
        ok = await k.post("/v1/chat/completions", json={"model": "llama", "messages": [{"role": "user", "content": "oi"}]})
        assert ok.status_code == 200 and ok.json()["choices"][0]["message"]["content"] == "de groq"
        # rotas de gestao e dados continuam fechadas para a chave de cliente
        for method, url in (("get", "/api/config"), ("get", "/api/router/state"), ("post", "/api/tasks/run"),
                            ("get", "/api/router/clients"), ("get", "/api/obsidian/tree")):
            assert (await getattr(k, method)(url)).status_code == 401, url
    async with with_key("hk-chave-inventada") as bad:
        assert (await bad.get("/v1/models")).status_code == 401


@pytest.mark.asyncio
async def test_api_chave_respeita_modelos_permitidos_e_aparece_no_registro(api):
    async with master() as c:
        made = (await c.post("/api/router/clients", json={"name": "limitada", "models": ["so-este"], "human_code": code()})).json()
        api.append(made["id"])
    async with with_key(made["key"]) as k:
        denied = await k.post("/v1/chat/completions", json={"model": "llama", "messages": [{"role": "user", "content": "oi"}]})
        assert denied.status_code == 403
        assert (await k.get("/v1/models")).json()["data"] == []
    async with master() as c:
        await c.patch(f"/api/router/clients/{made['id']}", json={"models": ["llama"]})
    async with with_key(made["key"]) as k:
        await k.post("/v1/chat/completions", json={"model": "llama", "messages": [{"role": "user", "content": "oi"}]})
    async with master() as c:
        logs = (await c.get("/api/router/logs", params={"client": "limitada"})).json()["logs"]
        assert len(logs) == 1 and logs[0]["provider"] == "groq" and logs[0]["ok"] == 1
        listed = (await c.get("/api/router/clients")).json()["clients"]
        assert listed[0]["today"]["requests"] == 1 and "key" not in listed[0] and "hash" not in listed[0]


@pytest.mark.asyncio
async def test_api_limites_da_chave_por_minuto_e_por_dia(api):
    async with master() as c:
        made = (await c.post("/api/router/clients", json={"name": "apertada", "rpm": 1, "human_code": code()})).json()
        api.append(made["id"])
    body = {"model": "llama", "messages": [{"role": "user", "content": "oi"}]}
    async with with_key(made["key"]) as k:
        assert (await k.post("/v1/chat/completions", json=body)).status_code == 200
        limited = await k.post("/v1/chat/completions", json=body)
        assert limited.status_code == 429 and limited.headers.get("retry-after")
    async with master() as c:
        made2 = (await c.post("/api/router/clients", json={"name": "diaria", "daily_tokens": 5, "human_code": code()})).json()
        api.append(made2["id"])
    server.USAGE.record("groq", "llama", True, 200, 4, 4, 10, client="diaria")
    async with with_key(made2["key"]) as k:
        assert (await k.post("/v1/chat/completions", json=body)).status_code == 429


@pytest.mark.asyncio
async def test_api_gestao_de_chaves_exige_codigo_e_nome_unico(api):
    async with master() as c:
        assert (await c.post("/api/router/clients", json={"name": "abc"})).status_code == 403
        first = await c.post("/api/router/clients", json={"name": "abc", "human_code": code()})
        api.append(first.json()["id"])
        dup = await c.post("/api/router/clients", json={"name": "ABC", "human_code": code()})
        assert dup.status_code == 409
        assert (await c.post("/api/router/clients", json={"name": "xyz", "models": ["tem espaco"], "human_code": code()})).status_code == 400
        kid = first.json()["id"]
        assert (await c.patch(f"/api/router/clients/{kid}", json={"enabled": False})).status_code == 200      # desligar: sem codigo
        assert (await c.patch(f"/api/router/clients/{kid}", json={"enabled": True})).status_code == 403       # religar: com codigo
        assert (await c.patch(f"/api/router/clients/{kid}", json={"enabled": True, "human_code": code()})).status_code == 200
        assert (await c.delete(f"/api/router/clients/{kid}")).status_code == 200
        api.remove(kid)
        assert (await c.delete(f"/api/router/clients/{kid}")).status_code == 404


@pytest.mark.asyncio
async def test_api_ajustes_camadas_apelidos_precos_rtk(api):
    async with master() as c:
        ok = await c.put("/api/router/settings", json={
            "tier_order": ["cloud_free", "cloud_cheap", "cloud_premium", "local"], "rtk": True, "breaker_threshold": 5,
            "aliases": {"rapido": "groq/llama"}, "pricing": {"groq/llama": {"in": 0.5, "out": 1.5}}})
        assert ok.status_code == 200
        assert server.gateway_tier_router.tier_order[0] == "cloud_free" and server.RUNTIME_FLAGS["rtk"] is True
        assert server.gateway_tier_router.aliases == {"rapido": "groq/llama"} and server.gateway_tier_router.guard.threshold == 5
        assert server.gateway_tier_router.pricing["groq/llama"] == {"in": 0.5, "out": 1.5}
        state = (await c.get("/api/router/state")).json()
        assert state["aliases"] == {"rapido": "groq/llama"} and state["rtk"]["enabled"] is True and state["breaker_threshold"] == 5
        ids = {m["id"] for m in (await c.get("/v1/models")).json()["data"]}
        assert "rapido" in ids
        for bad in ({"tier_order": ["cloud_free", "cloud_free", "local"]}, {"tier_order": ["cloud_free"]},
                    {"aliases": {"default": "x"}}, {"aliases": {"com espaco": "x"}}, {"aliases": {"a": "a"}},
                    {"pricing": {"Chave Ruim": {"in": 1, "out": 1}}}, {"pricing": {"x1": {"in": -1, "out": 1}}},
                    {"breaker_threshold": 0}):
            assert (await c.put("/api/router/settings", json=bad)).status_code in (400, 422), bad


@pytest.mark.asyncio
async def test_api_limites_do_provedor_e_reset_do_disjuntor(api):
    async with master() as c:
        await c.post("/api/router/providers", json={"preset": "groq", "human_code": code()})
        r = await c.post("/api/router/providers/groq/limits", json={"priority": 3, "rpm": 30, "daily_tokens": 100000})
        assert r.status_code == 200 and r.json()["provider"]["rpm"] == 30 and r.json()["provider"]["priority"] == 3
        assert server.gateway_tier_router.guard.limits["groq"]["rpm"] == 30 and server.gateway_tier_router.priorities["groq"] == 3
        cleared = await c.post("/api/router/providers/groq/limits", json={"rpm": 0})
        assert cleared.json()["provider"]["rpm"] is None
        assert (await c.post("/api/router/providers/nenhum/limits", json={"rpm": 1})).status_code == 404
        server.gateway_tier_router.guard.report_failure("groq", "x")
        for _ in range(5):
            server.gateway_tier_router.guard.report_failure("groq", "x")
        assert (await c.get("/api/router/state")).json()["providers"][0]["health"]["state"] == "paused"
        assert (await c.post("/api/router/providers/groq/reset")).json()["health"]["state"] == "ok"


@pytest.mark.asyncio
async def test_api_combo_com_estrategia_e_peso(api):
    async with master() as c:
        await c.post("/api/router/providers", json={"preset": "groq", "human_code": code()})
        body = {"combos": {"misto": {"strategy": "weighted", "steps": [{"provider": "groq", "model": "m1", "weight": 3},
                                                                       {"provider": "ollama", "model": "q"}]},
                           "antigo": [{"provider": "ollama", "model": "q"}]}}
        assert (await c.put("/api/router/combos", json=body)).status_code == 200
        state = (await c.get("/api/router/state")).json()["combos"]
        assert state["misto"]["strategy"] == "weighted" and state["misto"]["steps"][0]["weight"] == 3
        assert state["antigo"]["strategy"] == "priority"
        assert server.gateway_tier_router.combo_strategies["misto"] == "weighted"
        bad = {"combos": {"x1": {"strategy": "aleatorio", "steps": [{"provider": "ollama", "model": "q"}]}}}
        assert (await c.put("/api/router/combos", json=bad)).status_code == 422


@pytest.mark.asyncio
async def test_api_exportar_e_importar_chegam_desligados_e_sem_chaves(api):
    async with master() as c:
        await c.post("/api/router/providers", json={"preset": "groq", "human_code": code()})
        await c.post("/api/router/providers/groq/keys", json={"keys": ["gsk_" + "Z9y8X7w6V5u4T3s2R1q0" * 2], "human_code": code()})
        await c.put("/api/router/combos", json={"combos": {"misto": {"strategy": "fastest", "steps": [{"provider": "groq", "model": "m"}]}}})
        exported = (await c.get("/api/router/export")).json()
        assert "Z9y8X7w6V5u4T3s2R1q0" not in json.dumps(exported) and exported["combos"]["misto"]["strategy"] == "fastest"
        await c.put("/api/router/combos", json={"combos": {}})
        await c.delete("/api/router/providers/groq")
        assert (await c.post("/api/router/import", json={"data": exported})).status_code == 403
        done = await c.post("/api/router/import", json={"data": exported, "human_code": code()})
        assert done.status_code == 200 and done.json()["providers"] == 1
        prov = (await c.get("/api/router/state")).json()["providers"][0]
        assert prov["name"] == "groq" and prov["enabled"] is False          # chega DESLIGADO
        evil = json.loads(json.dumps(exported))
        evil["providers"]["groq"]["base_url"] = "http://127.0.0.1:8000/v1"
        assert (await c.post("/api/router/import", json={"data": evil, "human_code": code()})).status_code == 400
        evil = json.loads(json.dumps(exported))
        evil["providers"]["groq"]["api_key_env"] = "HERA_API_KEY"
        assert (await c.post("/api/router/import", json={"data": evil, "human_code": code()})).status_code == 400
        assert (await c.post("/api/router/import", json={"data": {"version": 2}, "human_code": code()})).status_code == 400


def test_catalogo_de_provedores_e_valido():
    ids = [p["id"] for p in PROVIDER_PRESETS]
    assert len(ids) == len(set(ids)) >= 25
    for p in PROVIDER_PRESETS:
        validate_provider_url(p["base_url"])
        assert re.match(r"^[A-Z][A-Z0-9_]{1,63}_API_KEYS?$", p["key_env"]) and p["tier"] in ("cloud_premium", "cloud_cheap", "cloud_free")
        assert p["default_model"] and p["signup"].startswith("https://")
