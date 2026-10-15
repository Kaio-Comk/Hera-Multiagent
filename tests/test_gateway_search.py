"""Busca na web pelo Roteador: Brave, Tavily e Exa com saída normalizada (`POST /v1/search`)."""
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from app.config_manager import ConfigManager
from app.gateway import modalities_api
from app.gateway.key_manager import KeyManager
from app.gateway.providers import GatewayProviderError
from app.gateway.search import (
    BraveSearch, ExaSearch, SearchRouter, TavilySearch, build_search_providers,
)
from app.gateway.secret_store import SecretStore
from app.gateway.usage import UsageLedger
from helpers import AUTH_HEADERS, TEST_BASE_URL

KEY = "k" * 24


def _km(label="x"):
    return KeyManager([KEY], label=label)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── provedores ──

@pytest.mark.asyncio
async def test_brave_monta_a_requisicao_e_normaliza():
    seen = {}

    async def handler(request):
        seen.update(method=request.method, url=str(request.url), token=request.headers["x-subscription-token"])
        return httpx.Response(200, json={"web": {"results": [
            {"title": "A", "url": "https://a.test", "description": "da A", "age": "2 days ago"}, {"title": "sem url"}]}})

    async with _client(handler) as c:
        res = await BraveSearch(_km()).search("hera gateway", 3, c)
    assert seen["method"] == "GET" and "q=hera+gateway" in seen["url"] and "count=3" in seen["url"] and seen["token"] == KEY
    assert [(r.title, r.url, r.snippet, r.published) for r in res] == [("A", "https://a.test", "da A", "2 days ago")]


@pytest.mark.asyncio
async def test_tavily_e_exa_usam_post_json_e_o_cabecalho_certo():
    calls = []

    async def handler(request):
        calls.append((str(request.url), dict(request.headers), json.loads(request.content)))
        if "tavily" in str(request.url):
            return httpx.Response(200, json={"results": [{"title": "T", "url": "https://t.test", "content": "ct", "score": 0.9}]})
        return httpx.Response(200, json={"results": [{"title": "E", "url": "https://e.test", "text": "tx", "publishedDate": "2025-01-02"}]})

    async with _client(handler) as c:
        t = await TavilySearch(_km()).search("q", 5, c)
        e = await ExaSearch(_km()).search("q", 5, c)
    assert calls[0][1]["authorization"] == f"Bearer {KEY}" and calls[0][2] == {"query": "q", "max_results": 5}
    assert calls[1][1]["x-api-key"] == KEY and calls[1][2]["numResults"] == 5
    assert t[0].public() == {"title": "T", "url": "https://t.test", "snippet": "ct", "score": 0.9}
    assert e[0].public() == {"title": "E", "url": "https://e.test", "snippet": "tx", "published": "2025-01-02"}


@pytest.mark.asyncio
async def test_respeita_o_limite_de_resultados_e_trata_resposta_ruim():
    many = {"web": {"results": [{"title": str(i), "url": f"https://x.test/{i}"} for i in range(10)]}}
    async with _client(lambda r: httpx.Response(200, json=many)) as c:
        assert len(await BraveSearch(_km()).search("q", 4, c)) == 4
    async with _client(lambda r: httpx.Response(200, text="<html>nao e json</html>")) as c:
        with pytest.raises(GatewayProviderError) as exc:
            await BraveSearch(_km()).search("q", 4, c)
    assert exc.value.status_code == 502 and exc.value.retryable


@pytest.mark.asyncio
async def test_429_poe_a_chave_em_espera_e_401_desativa():
    async def limited(request):
        return httpx.Response(429, headers={"retry-after": "30"})

    p = BraveSearch(_km())
    async with _client(limited) as c:
        with pytest.raises(GatewayProviderError) as exc:
            await p.search("q", 3, c)
    assert exc.value.status_code == 429 and p.key_manager.status()[0]["state"] == "cooldown"

    p2 = TavilySearch(_km())
    async with _client(lambda r: httpx.Response(401)) as c:
        with pytest.raises(GatewayProviderError) as exc:
            await p2.search("q", 3, c)
    assert exc.value.status_code == 401 and p2.key_manager.status()[0]["state"] == "disabled"
    assert KEY not in str(exc.value)                                          # o segredo nunca aparece no erro


@pytest.mark.asyncio
async def test_rodizio_entre_duas_chaves_do_mesmo_provedor():
    used = []

    async def handler(request):
        used.append(request.headers["x-subscription-token"])
        return httpx.Response(429 if len(used) == 1 else 200, json={"web": {"results": []}})

    p = BraveSearch(KeyManager(["a" * 24, "b" * 24], label="brave"))
    async with _client(handler) as c:
        assert await p.search("q", 3, c) == []
    assert used == ["a" * 24, "b" * 24]                                        # 429 na primeira -> tenta a segunda


# ── roteador ──

@pytest.mark.asyncio
async def test_roteador_fallback_e_escolha_de_provedor():
    async def handler(request):
        if "brave" in str(request.url):
            return httpx.Response(503)
        return httpx.Response(200, json={"results": [{"title": "T", "url": "https://t.test", "content": "c"}]})

    r = SearchRouter([BraveSearch(_km("b")), TavilySearch(_km("t"))])
    async with _client(handler) as c:
        out = await r.search("q", 3, c)
        assert out["provider"] == "tavily" and out["object"] == "search.results" and out["results"][0]["url"] == "https://t.test"
        assert (await r.search("q", 3, c, provider="tavily"))["provider"] == "tavily"
        with pytest.raises(GatewayProviderError) as exc:
            await r.search("q", 3, c, provider="brave")                        # forçado: sem fallback
        assert exc.value.status_code == 503
        with pytest.raises(GatewayProviderError) as exc:
            await r.search("q", 3, c, provider="exa")
        assert exc.value.status_code == 404
    with pytest.raises(GatewayProviderError) as exc:
        await SearchRouter([]).search("q", 3, CLIENT_DUMMY)
    assert exc.value.status_code == 503


CLIENT_DUMMY = httpx.AsyncClient()


def test_fabrica_le_o_config_ordem_e_ignora_sem_chave():
    cfg = {"search": {"brave": {"enabled": True, "api_key_env": "BR"}, "tavily": {"enabled": True, "api_key_env": "TV"},
                      "exa": {"enabled": False, "api_key_env": "EX"}, "order": ["tavily", "brave"]}}
    got = build_search_providers(cfg, environ={"BR": KEY, "TV": KEY + ",," + "z" * 24, "EX": KEY})
    assert [p.name for p in got] == ["tavily", "brave"] and len(got[0].key_manager) == 2
    assert build_search_providers(cfg, environ={"BR": KEY}) [0].name == "brave"            # tavily sem chave: ignorado
    assert build_search_providers({}, environ={}) == []


def test_validacao_do_corpo():
    assert modalities_api.prepare_search({"query": " oi "}, 20, 2000) == ("oi", 5, "")
    assert modalities_api.prepare_search({"query": "x", "max_results": 20, "provider": "brave"}, 20, 2000) == ("x", 20, "brave")
    for bad in ({}, {"query": " "}, {"query": "x", "max_results": 0}, {"query": "x", "max_results": 21},
                {"query": "x", "max_results": True}, {"query": "x", "provider": "../x"}, {"query": "x" * 2001}, [1]):
        with pytest.raises(ValueError):
            modalities_api.prepare_search(bad, 20, 2000)


# ── rota ──

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "config_mgr", ConfigManager(config_path=str(tmp_path / "config.yaml")))
    monkeypatch.setattr(server, "SECRET_STORE", SecretStore(tmp_path / "s.json", use_dpapi=False))
    monkeypatch.setattr(server, "USAGE", UsageLedger(tmp_path / "usage.db"))
    monkeypatch.setattr(server.router, "default_offline", False)
    seen = []

    async def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json={"web": {"results": [{"title": "A", "url": "https://a.test", "description": "d"}]}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(server, "gateway_http_client", http)
    monkeypatch.setattr(server.search_router, "providers", [BraveSearch(_km("brave"))])
    created = []
    yield seen, created
    for key_id in created:
        server.CLIENT_KEYS.delete(key_id)


def master():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS)


@pytest.mark.asyncio
async def test_rota_busca_devolve_resultados_e_registra_uso(api):
    seen, _ = api
    async with master() as c:
        r = await c.post("/v1/search", json={"query": "hera orquestrador", "max_results": 3})
    assert r.status_code == 200
    d = r.json()
    assert d["provider"] == "brave" and d["results"] == [{"title": "A", "url": "https://a.test", "snippet": "d"}]
    assert "q=hera+orquestrador" in seen[0]
    assert server.USAGE.today(provider="brave")["requests"] == 1


@pytest.mark.asyncio
async def test_rota_dlp_barra_segredo_e_modo_offline(api, monkeypatch):
    seen, _ = api
    async with master() as c:
        leak = await c.post("/v1/search", json={"query": "meu token sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA vazou?"})
        assert leak.status_code == 403 and leak.json()["error"]["type"] == "permission_denied"
        monkeypatch.setattr(server.router, "default_offline", True)
        off = await c.post("/v1/search", json={"query": "noticias de hoje"})
        assert off.status_code == 403
    assert seen == []                                                          # nada saiu da máquina


@pytest.mark.asyncio
async def test_rota_erros_e_sem_provedor(api, monkeypatch):
    async with master() as c:
        assert (await c.post("/v1/search", json={"max_results": 3})).status_code == 400
        assert (await c.post("/v1/search", content=b"nao json", headers={"content-type": "application/json"})).status_code == 400
        assert (await c.post("/v1/search", json={"query": "x", "provider": "exa"})).status_code == 404
        monkeypatch.setattr(server.search_router, "providers", [])
        none = await c.post("/v1/search", json={"query": "x"})
        assert none.status_code == 503 and none.json()["error"]["type"] == "server_error"
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL) as anon:
        assert (await anon.post("/v1/search", json={"query": "x"})).status_code == 401


@pytest.mark.asyncio
async def test_rota_com_chave_de_cliente_limitada_por_provedor(api):
    _, created = api
    async with master() as c:
        made = (await c.post("/api/router/clients", json={"name": "agente", "models": ["search/brave"],
                                                          "human_code": server.HUMAN_CODE.current()})).json()
        created.append(made["id"])
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL,
                           headers={"Authorization": f"Bearer {made['key']}"}) as k:
        assert (await k.post("/v1/search", json={"query": "x", "provider": "brave"})).status_code == 200
        assert (await k.post("/v1/search", json={"query": "x"})).status_code == 403          # "search" não está liberado
        assert (await k.get("/api/config")).status_code == 401
