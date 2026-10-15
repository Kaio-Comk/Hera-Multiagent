"""Provedores de nuvem no gateway: chaves (rodízio autorizado, Retry-After, desativação), adaptador,
fábrica a partir do config, ordem por camada e roteamento por modelo."""
import httpx
import pytest
from fastapi.testclient import TestClient

import app.server as server
from app.gateway.factory import build_gateway_adapters
from app.gateway.key_manager import (KeyManager, NoKeyAvailableError, load_keys_from_env, parse_retry_after)
from app.gateway.providers import CloudOpenAIGatewayAdapter, GatewayProviderError, OllamaGatewayAdapter
from app.gateway.schemas import ChatCompletionRequest, ChatMessage
from app.gateway.tier_router import TierRouter
from helpers import AUTH_HEADERS, TEST_BASE_URL

OK_JSON = {"id": "c1", "object": "chat.completion", "created": 1, "model": "m",
           "choices": [{"index": 0, "message": {"role": "assistant", "content": "ola"}, "finish_reason": "stop"}],
           "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
OLLAMA_JSON = {"message": {"content": "do ollama"}, "done": True, "prompt_eval_count": 1, "eval_count": 1}


def req(model="default", text="oi"):
    return ChatCompletionRequest(model=model, messages=[ChatMessage(role="user", content=text)])


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ── KeyManager ─────────────────────────────────────────────────────────────────
def test_rodizio_entre_chaves_e_deduplicacao():
    km = KeyManager(["a" * 20, "b" * 20, "a" * 20, "  ", ""], label="X")
    assert len(km) == 2
    assert [km.acquire().key_id for _ in range(4)] == ["X#1", "X#2", "X#1", "X#2"]


def test_429_poe_a_chave_em_espera_pelo_retry_after_e_ela_nao_e_usada_ate_la():
    clk = Clock()
    km = KeyManager(["a" * 20, "b" * 20], label="X", clock=clk)
    lease = km.acquire()
    km.report_rate_limited(lease.key_id, 120)
    ids = {km.acquire().key_id for _ in range(5)}
    assert ids == {"X#2"}                                    # a chave em espera NAO volta ao rodizio
    clk.t += 121
    assert {km.acquire().key_id for _ in range(4)} == {"X#1", "X#2"}


def test_todas_em_espera_levanta_com_o_menor_tempo_restante():
    clk = Clock()
    km = KeyManager(["a" * 20, "b" * 20], clock=clk)
    km.report_rate_limited(km.acquire().key_id, 30)
    km.report_rate_limited(km.acquire().key_id, 90)
    with pytest.raises(NoKeyAvailableError) as exc:
        km.acquire()
    assert 29 <= exc.value.retry_after <= 30


def test_cooldown_padrao_e_teto():
    clk = Clock()
    km = KeyManager(["a" * 20], clock=clk)
    km.report_rate_limited("key#1", None)                    # sem Retry-After: 60 s
    with pytest.raises(NoKeyAvailableError) as exc:
        km.acquire()
    assert 59 <= exc.value.retry_after <= 60
    km.report_rate_limited("key#1", 10 ** 9)                 # absurdo: teto de 1 h
    with pytest.raises(NoKeyAvailableError) as exc:
        km.acquire()
    assert exc.value.retry_after <= 3600


def test_chave_recusada_e_desativada_e_sem_chaves_o_retry_after_e_none():
    km = KeyManager(["a" * 20], label="X")
    km.report_auth_failure(km.acquire().key_id)
    with pytest.raises(NoKeyAvailableError) as exc:
        km.acquire()
    assert exc.value.retry_after is None
    assert km.status()[0]["state"] == "disabled"


def test_limite_diario_por_chave_e_zera_no_dia_seguinte():
    clk = Clock(86400.0 * 10 + 100)
    km = KeyManager(["a" * 20], daily_limit=2, clock=clk)
    km.acquire(); km.acquire()
    with pytest.raises(NoKeyAvailableError):
        km.acquire()
    assert km.status()[0]["state"] == "daily_limit"
    clk.t += 86400
    assert km.acquire().key_id == "key#1"


def test_status_nunca_contem_o_segredo():
    secret = "sk-segredo-super-secreto-1234567890"
    km = KeyManager([secret], label="X")
    km.acquire()
    assert secret not in repr(km.status()) and secret not in str(km.status())


def test_retry_after_em_segundos_data_http_e_lixo():
    assert parse_retry_after("30") == 30.0 and parse_retry_after("0.5") == 0.5
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", now=1792567680 - 60) is not None
    assert parse_retry_after("nao-sei") is None and parse_retry_after(None) is None


def test_chaves_do_ambiente_separadas_por_virgula():
    env = {"A": "k1, k2 ,,k1", "B": " k3 ", "C": ""}
    assert load_keys_from_env(["A", "B", "C", "NAO_EXISTE"], env) == ["k1", "k2", "k3"]


# ── adaptador de nuvem ─────────────────────────────────────────────────────────
def cloud(km, **kw):
    return CloudOpenAIGatewayAdapter(name="deep", base_url="https://api.deepseek.com/v1", key_manager=km,
                                     default_model="deepseek-chat", tier="cloud_cheap", **kw)


@pytest.mark.asyncio
async def test_envia_a_chave_no_bearer_e_responde():
    seen = []

    async def handler(r):
        seen.append(r.headers["authorization"])
        return httpx.Response(200, json=OK_JSON)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        resp = await cloud(KeyManager(["sk-aaaaaaaaaaaaaaaaaaaa"])).chat_complete(req(), http)
    assert resp.choices[0].message.content == "ola" and seen == ["Bearer sk-aaaaaaaaaaaaaaaaaaaa"]


@pytest.mark.asyncio
async def test_429_troca_para_outra_chave_autorizada_e_a_primeira_descansa():
    calls = []

    async def handler(r):
        key = r.headers["authorization"][-1]
        calls.append(key)
        if key == "1":
            return httpx.Response(429, headers={"retry-after": "100"}, json={})
        return httpx.Response(200, json=OK_JSON)
    km = KeyManager(["sk-aaaaaaaaaaaaaaaaaaa1", "sk-aaaaaaaaaaaaaaaaaaa2"], label="X")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        ad = cloud(km)
        await ad.chat_complete(req(), http)
        await ad.chat_complete(req(), http)
    assert calls == ["1", "2", "2"]                          # 2a requisicao nem tenta a chave que descansa
    assert [k["state"] for k in km.status()] == ["cooldown", "ok"]


@pytest.mark.asyncio
async def test_todas_as_chaves_com_429_vira_erro_429_retryable_para_o_roteador_cair_de_provedor():
    async def handler(r):
        return httpx.Response(429, headers={"retry-after": "30"}, json={})
    km = KeyManager(["sk-aaaaaaaaaaaaaaaaaaa1", "sk-aaaaaaaaaaaaaaaaaaa2"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(GatewayProviderError) as exc:
            await cloud(km).chat_complete(req(), http)
        assert exc.value.status_code == 429 and exc.value.retryable
        with pytest.raises(GatewayProviderError) as exc2:                  # agora nem chama a rede
            await cloud(km).chat_complete(req(), http)
        assert exc2.value.status_code == 429 and "tente em" in str(exc2.value)


@pytest.mark.asyncio
async def test_401_desativa_a_chave_e_tenta_a_proxima():
    async def handler(r):
        return httpx.Response(401 if r.headers["authorization"].endswith("1") else 200,
                              json=OK_JSON if r.headers["authorization"].endswith("2") else {})
    km = KeyManager(["sk-aaaaaaaaaaaaaaaaaaa1", "sk-aaaaaaaaaaaaaaaaaaa2"])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        resp = await cloud(km).chat_complete(req(), http)
    assert resp.choices[0].message.content == "ola"
    assert [k["state"] for k in km.status()] == ["disabled", "ok"]


@pytest.mark.asyncio
async def test_erros_nao_vazam_a_chave_e_5xx_nao_esfria_a_chave():
    secret = "sk-secretissima-0123456789abc"

    async def handler(r):
        return httpx.Response(503, json={"error": "x"})
    km = KeyManager([secret])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(GatewayProviderError) as exc:
            await cloud(km).chat_complete(req(), http)
    assert exc.value.status_code == 503 and exc.value.retryable and secret not in str(exc.value)
    assert km.status()[0]["state"] == "ok"                                   # 5xx nao e culpa da chave


@pytest.mark.asyncio
async def test_400_nao_e_retryable():
    async def handler(r):
        return httpx.Response(400, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(GatewayProviderError) as exc:
            await cloud(KeyManager(["sk-aaaaaaaaaaaaaaaaaaaa"])).chat_complete(req(), http)
    assert not exc.value.retryable


def test_adaptador_sem_chave_ou_com_url_insegura_nao_nasce():
    with pytest.raises(ValueError):
        CloudOpenAIGatewayAdapter(name="x", base_url="https://api.deepseek.com/v1", default_model="m")
    with pytest.raises(Exception):
        cloud_url = "http://api.deepseek.com/v1"                               # sem HTTPS
        CloudOpenAIGatewayAdapter(name="x", base_url=cloud_url, api_key="k" * 20, default_model="m")


# ── fábrica a partir do config ─────────────────────────────────────────────────
BASE_CFG = {"engines": {"ollama": {"base_url": "http://localhost:11434", "default_model": "qwen"}}}


def provider(**kw):
    return {"enabled": True, "tier": "cloud_cheap", "base_url": "https://api.deepseek.com/v1",
            "api_key_env": "DEEPSEEK_API_KEY", "default_model": "deepseek-chat", **kw}


def test_fabrica_liga_so_o_que_tem_chave_e_url_valida():
    cfg = {**BASE_CFG, "providers": {
        "deepseek": provider(models=["deepseek-reasoner"]),
        "sem_chave": provider(api_key_env="NAO_DEFINIDA"),
        "desligado": provider(enabled=False),
        "url_ruim": provider(base_url="https://10.0.0.5/v1"),
        "tier_ruim": provider(tier="gratis-total"),
    }}
    ads = build_gateway_adapters(cfg, environ={"DEEPSEEK_API_KEY": "sk-aaaaaaaaaaaaaaaaaaa1,sk-aaaaaaaaaaaaaaaaaaa2"})
    assert [a.name for a in ads] == ["ollama", "deepseek"]
    assert len(ads[1].key_manager) == 2 and ads[1].served_models == {"deepseek-chat", "deepseek-reasoner"}


def test_fabrica_sem_providers_so_tem_o_ollama():
    assert [a.name for a in build_gateway_adapters(BASE_CFG, environ={})] == ["ollama"]


def test_a_chave_nunca_e_lida_do_config():
    cfg = {**BASE_CFG, "providers": {"deepseek": provider(api_key="sk-no-arquivo-0123456789")}}   # campo ignorado
    assert [a.name for a in build_gateway_adapters(cfg, environ={})] == ["ollama"]


# ── roteador: camadas e modelo ─────────────────────────────────────────────────
def two_adapters():
    deep = cloud(KeyManager(["sk-aaaaaaaaaaaaaaaaaaaa"]))
    return deep, OllamaGatewayAdapter()


def transport(log, cloud_status=200):
    async def handler(r):
        log.append("cloud" if "deepseek" in str(r.url) else "ollama")
        if "deepseek" in str(r.url):
            return httpx.Response(cloud_status, json=OK_JSON if cloud_status == 200 else {})
        return httpx.Response(200, json=OLLAMA_JSON)
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_ordem_de_camadas_nuvem_antes_do_local_mesmo_com_o_local_primeiro_na_lista():
    log = []
    deep, ollama = two_adapters()
    async with httpx.AsyncClient(transport=transport(log)) as http:
        await TierRouter(adapters=[ollama, deep]).route_and_execute(req("default"), http)
    assert log == ["cloud"]


@pytest.mark.asyncio
async def test_ordem_configuravel_local_primeiro():
    log = []
    deep, ollama = two_adapters()
    async with httpx.AsyncClient(transport=transport(log)) as http:
        router = TierRouter(adapters=[deep, ollama], tier_order=["local", "cloud_cheap"])
        await router.route_and_execute(req("default"), http)
    assert log == ["ollama"]


@pytest.mark.asyncio
async def test_nuvem_limitada_cai_para_o_local():
    log = []
    deep, ollama = two_adapters()
    async with httpx.AsyncClient(transport=transport(log, cloud_status=429)) as http:
        resp = await TierRouter(adapters=[deep, ollama]).route_and_execute(req("default"), http)
    assert log == ["cloud", "ollama"] and resp.choices[0].message.content == "do ollama"


@pytest.mark.asyncio
async def test_modelo_especifico_vai_so_ao_provedor_que_o_atende():
    log = []
    deep, ollama = two_adapters()
    async with httpx.AsyncClient(transport=transport(log)) as http:
        router = TierRouter(adapters=[deep, ollama])
        await router.route_and_execute(req("deepseek-chat"), http)     # nao pode ir ao Ollama
        await router.route_and_execute(req("qwen2.5-coder:1.5b"), http)  # nao pode ir a nuvem
    assert log == ["cloud", "ollama"]


@pytest.mark.asyncio
async def test_prompt_com_segredo_so_vai_ao_local_mesmo_com_nuvem_configurada():
    log = []
    deep, ollama = two_adapters()
    segredo = "minha chave: sk-abcdef1234567890abcdef1234567890"
    async with httpx.AsyncClient(transport=transport(log)) as http:
        resp = await TierRouter(adapters=[deep, ollama]).route_and_execute(req("default", segredo), http)
    assert resp.choices[0].message.content == "do ollama" and log == ["ollama"]


@pytest.mark.asyncio
async def test_segredo_com_modelo_de_nuvem_falha_fechado_sem_chamar_a_nuvem():
    log = []
    deep, ollama = two_adapters()
    async with httpx.AsyncClient(transport=transport(log)) as http:
        with pytest.raises(GatewayProviderError) as exc:
            await TierRouter(adapters=[deep, ollama]).route_and_execute(
                req("deepseek-chat", "token sk-abcdef1234567890abcdef1234567890"), http)
    assert log == [] and exc.value.status_code == 404           # a nuvem foi barrada pelo DLP; o Ollama nao serve esse modelo


@pytest.mark.asyncio
async def test_modelo_desconhecido_para_todos_os_provedores_da_nuvem_e_404():
    deep = cloud(KeyManager(["sk-aaaaaaaaaaaaaaaaaaaa"]))
    async with httpx.AsyncClient(transport=transport([])) as http:
        with pytest.raises(GatewayProviderError) as exc:
            await TierRouter(adapters=[deep]).route_and_execute(req("gpt-9000"), http)
    assert exc.value.status_code == 404


# ── API ────────────────────────────────────────────────────────────────────────
def test_endpoint_de_status_dos_provedores_exige_token_e_nao_vaza_segredo(monkeypatch):
    secret = "sk-api-secreta-0123456789abcdef"
    deep = cloud(KeyManager([secret], label="DEEPSEEK_API_KEY"))
    monkeypatch.setattr(server.gateway_tier_router, "adapters", [OllamaGatewayAdapter(), deep])
    anon = TestClient(server.app, base_url=TEST_BASE_URL)
    assert anon.get("/api/gateway/providers").status_code == 401
    r = TestClient(server.app, base_url=TEST_BASE_URL, headers=AUTH_HEADERS).get("/api/gateway/providers")
    assert r.status_code == 200 and secret not in r.text
    body = r.json()
    assert body["tier_order"][-1] == "local"
    assert {p["name"] for p in body["providers"]} == {"ollama", "deep"}
    assert next(p for p in body["providers"] if p["name"] == "deep")["keys"][0]["id"] == "DEEPSEEK_API_KEY#1"


def test_v1_models_inclui_modelos_dos_provedores_de_nuvem(monkeypatch):
    deep = cloud(KeyManager(["sk-aaaaaaaaaaaaaaaaaaaa"]), models=["deepseek-reasoner"])
    monkeypatch.setattr(server.gateway_tier_router, "adapters", [OllamaGatewayAdapter(), deep])
    data = TestClient(server.app, base_url=TEST_BASE_URL, headers=AUTH_HEADERS).get("/v1/models").json()["data"]
    ids = {m["id"]: m["owned_by"] for m in data}
    assert ids["deepseek-chat"] == "hera-deep" and ids["deepseek-reasoner"] == "hera-deep"


def test_localhost_vira_ipv4_so_para_loopback():
    """No Windows `localhost` tenta o IPv6 antes do IPv4 e dobra o tempo para dizer que o Ollama esta parado."""
    from app.gateway.provider_policy import prefer_ipv4_loopback as f
    assert f("http://localhost:11434") == "http://127.0.0.1:11434"
    assert f("http://LOCALHOST/api") == "http://127.0.0.1/api"
    assert f("http://127.0.0.1:11434") == "http://127.0.0.1:11434"
    assert f("https://api.groq.com/v1") == "https://api.groq.com/v1"
    assert f("http://localhost.evil.com:11434") == "http://localhost.evil.com:11434"     # nome parecido nao e loopback
    from adapters.ollama import OllamaAdapter
    assert OllamaAdapter(base_url="http://localhost:11434").base_url == "http://127.0.0.1:11434"


@pytest.mark.asyncio
async def test_ollama_parado_responde_apos_uma_tentativa_so():
    """Daemon parado: a checagem de saude para no primeiro erro de conexao (nao tenta `/api/tags` de novo)."""
    import httpx
    from adapters.ollama import OllamaAdapter
    calls = []

    async def refuse(request):
        calls.append(request.url.path)
        raise httpx.ConnectError("recusado", request=request)

    real = httpx.AsyncClient
    import adapters.ollama as mod
    mod.httpx = type("H", (), {k: getattr(httpx, k) for k in dir(httpx) if not k.startswith("__")})
    mod.httpx.AsyncClient = lambda **kw: real(transport=httpx.MockTransport(refuse), **kw)
    try:
        st = await OllamaAdapter().health_check()
    finally:
        mod.httpx = httpx
    assert st.healthy is False and calls == ["/api/version"]
