"""Vídeo assíncrono (`/v1/videos`): criação, status e arquivo, sempre voltando ao provedor que criou."""
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from app.config_manager import ConfigManager
from app.gateway import modalities_api
from app.gateway.client_keys import is_client_path
from app.gateway.key_manager import KeyManager
from app.gateway.providers import CloudOpenAIGatewayAdapter, GatewayProviderError
from app.gateway.secret_store import SecretStore
from app.gateway.usage import UsageLedger
from helpers import AUTH_HEADERS, TEST_BASE_URL

KEY = "k" * 24


def _adapter(name="openai", **kw):
    return CloudOpenAIGatewayAdapter(name=name, base_url="https://api.openai.com/v1", default_model="sora-2",
                                     key_manager=KeyManager([KEY], label=name), **kw)


# ── id com provedor ──

def test_id_de_video_leva_o_provedor_e_rejeita_lixo():
    tagged = modalities_api.tag_video_id("openai", "video_123")
    assert tagged == "openai~video_123" and modalities_api.split_video_id(tagged) == ("openai", "video_123")
    for bad in ("semprovedor", "~x", "openai~", "openai~../../etc", "openai~a/b", "openai~" + "a" * 129, "a b~c", ""):
        with pytest.raises(ValueError):
            modalities_api.split_video_id(bad)
    assert modalities_api.prepare("video", {"model": "m", "prompt": "um gato"}) == ("m", "um gato")
    with pytest.raises(ValueError):
        modalities_api.prepare("video", {"model": "m"})


def test_chave_de_cliente_so_alcanca_caminhos_de_video_conhecidos():
    assert is_client_path("/v1/videos") and is_client_path("/v1/videos/openai~v1")
    assert is_client_path("/v1/videos/openai~v1/content")
    assert not is_client_path("/v1/videos/a/b/c") and not is_client_path("/api/config")


# ── adaptador ──

@pytest.mark.asyncio
async def test_adaptador_cria_consulta_e_baixa_com_a_chave():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers["authorization"],
                     request.headers.get("content-type")))
        if request.method == "POST":
            return httpx.Response(200, json={"id": "video_9", "status": "queued"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"MP4DATA", headers={"content-type": "video/mp4"})
        return httpx.Response(200, json={"id": "video_9", "status": "completed"})

    ad = _adapter()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        created = await ad.modality("video", {"model": "sora-2", "prompt": "x"}, c)
        assert json.loads(created.content)["id"] == "video_9"
        status = await ad.video_fetch("video_9", c)
        assert json.loads(status.content)["status"] == "completed"
        stream = await ad.video_fetch("video_9", c, content=True)
        assert await stream.aread() == b"MP4DATA"
        await stream.aclose()
    assert [(m, p) for m, p, _, _ in seen] == [("POST", "/v1/videos"), ("GET", "/v1/videos/video_9"),
                                              ("GET", "/v1/videos/video_9/content")]
    assert all(a == f"Bearer {KEY}" for _, _, a, _ in seen)
    assert seen[0][3] == "application/json" and seen[1][3] is None            # GET não manda corpo nem Content-Type


@pytest.mark.asyncio
async def test_adaptador_sem_video_recusa_e_id_com_barra_nao_escapa():
    ad = _adapter(modalities=["embeddings"])
    with pytest.raises(GatewayProviderError):
        await ad.video_fetch("v1", httpx.AsyncClient())
    paths = []

    async def handler(request):
        paths.append(request.url.raw_path.decode())
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        await _adapter().video_fetch("a/../b", c)
    assert paths == ["/v1/videos/a%2F..%2Fb"]                                  # nada de subir de pasta no provedor


# ── rotas ──

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "config_mgr", ConfigManager(config_path=str(tmp_path / "config.yaml")))
    monkeypatch.setattr(server, "SECRET_STORE", SecretStore(tmp_path / "s.json", use_dpapi=False))
    monkeypatch.setattr(server, "USAGE", UsageLedger(tmp_path / "usage.db"))
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(200, json={"id": "video_9", "object": "video", "status": "queued"})
        if request.url.path.endswith("/content"):
            return httpx.Response(200, content=b"MP4" * 1000, headers={"content-type": "video/mp4"})
        return httpx.Response(200, json={"id": "video_9", "status": "completed", "progress": 100})

    monkeypatch.setattr(server, "gateway_http_client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    ad = _adapter(models=["sora-2"])
    monkeypatch.setattr(server.gateway_tier_router, "adapters", [ad])
    monkeypatch.setattr(server.gateway_tier_router, "usage", server.USAGE)
    monkeypatch.setattr(server.gateway_tier_router, "aliases", {})
    monkeypatch.setattr(server.router, "default_offline", False)
    created = []
    yield seen, created
    for key_id in created:
        server.CLIENT_KEYS.delete(key_id)


def master():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS)


@pytest.mark.asyncio
async def test_fluxo_criar_consultar_e_baixar(api):
    seen, _ = api
    async with master() as c:
        made = await c.post("/v1/videos", json={"model": "sora-2", "prompt": "um gato andando"})
        assert made.status_code == 200 and made.json()["id"] == "openai~video_9"       # id leva o provedor
        vid = made.json()["id"]
        st = await c.get(f"/v1/videos/{vid}")
        assert st.status_code == 200 and st.json() == {"id": "openai~video_9", "status": "completed", "progress": 100}
        dl = await c.get(f"/v1/videos/{vid}/content")
        assert dl.status_code == 200 and dl.headers["content-type"] == "video/mp4" and dl.content == b"MP4" * 1000
    assert seen == [("POST", "/v1/videos"), ("GET", "/v1/videos/video_9"), ("GET", "/v1/videos/video_9/content")]


@pytest.mark.asyncio
async def test_rotas_de_video_erros(api, monkeypatch):
    async with master() as c:
        assert (await c.post("/v1/videos", json={"model": "sora-2"})).status_code == 400            # sem prompt
        assert (await c.get("/v1/videos/semprovedor")).status_code == 400                           # id sem provedor
        assert (await c.get("/v1/videos/outro~v1")).status_code == 404                              # provedor inexistente
        assert (await c.post("/v1/videos", json={"model": "modelo-sem-dono", "prompt": "x"})).status_code == 404
        monkeypatch.setattr(server.router, "default_offline", True)
        assert (await c.get("/v1/videos/openai~video_9")).status_code == 403                         # offline: nuvem barrada
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL) as anon:
        assert (await anon.get("/v1/videos/openai~video_9")).status_code == 401
        assert (await anon.post("/v1/videos", json={"model": "sora-2", "prompt": "x"})).status_code == 401


@pytest.mark.asyncio
async def test_prompt_com_segredo_nao_sai_para_o_provedor(api):
    seen, _ = api
    async with master() as c:
        r = await c.post("/v1/videos", json={"model": "sora-2",
                                              "prompt": "video mostrando a chave sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"})
    assert r.status_code in (403, 404, 503) and seen == []


@pytest.mark.asyncio
async def test_chave_de_cliente_cria_e_consulta_mas_so_com_modelo_liberado(api):
    _, created = api
    async with master() as c:
        made = (await c.post("/api/router/clients", json={"name": "estudio", "models": ["sora-2", "video/openai"],
                                                          "human_code": server.HUMAN_CODE.current()})).json()
        created.append(made["id"])
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL,
                           headers={"Authorization": f"Bearer {made['key']}"}) as k:
        vid = (await k.post("/v1/videos", json={"model": "sora-2", "prompt": "x"})).json()["id"]
        assert (await k.get(f"/v1/videos/{vid}")).status_code == 200
        assert (await k.get(f"/v1/videos/{vid}/content")).status_code == 200
        denied = await k.post("/v1/videos", json={"model": "outro", "prompt": "x"})
        assert denied.status_code == 403
        assert (await k.get("/api/config")).status_code == 401
    async with master() as c:                                              # chave sem "video/openai" não consulta
        limited = (await c.post("/api/router/clients", json={"name": "so-modelo", "models": ["sora-2"],
                                                             "human_code": server.HUMAN_CODE.current()})).json()
        created.append(limited["id"])
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL,
                           headers={"Authorization": f"Bearer {limited['key']}"}) as k2:
        assert (await k2.get("/v1/videos/openai~video_9")).status_code == 403
