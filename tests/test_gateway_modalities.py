"""Modalidades além do chat: embeddings, imagens, fala e transcrição (`/v1/embeddings`, `/v1/images/generations`,
`/v1/audio/speech`, `/v1/audio/transcriptions`)."""
import json

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from app.config_manager import ConfigManager
from app.gateway import modalities_api
from app.gateway.key_manager import KeyManager
from app.gateway.providers import (
    CloudOpenAIGatewayAdapter, GatewayProviderError, ModalityResponse, OllamaGatewayAdapter,
)
from app.gateway.secret_store import SecretStore
from app.gateway.tier_router import TierRouter
from app.gateway.usage import UsageLedger
from helpers import AUTH_HEADERS, TEST_BASE_URL
from test_router_embutido import Fake


class FakeMod(Fake):
    """Provedor falso que atende modalidades (e registra o que recebeu)."""

    def __init__(self, name, kinds=("embeddings", "images", "speech", "transcription"), **kw):
        super().__init__(name, **kw)
        self.kinds, self.calls = set(kinds), []

    def supports(self, kind):
        return kind in self.kinds

    async def modality(self, kind, payload, client, files=None):
        self.calls.append((kind, payload, files))
        if self.error:
            raise self.error
        if kind == "speech":
            return ModalityResponse(b"\x00AUDIO", "audio/mpeg")
        return ModalityResponse(json.dumps({"kind": kind, "by": self.name, "model": payload["model"],
                                            "usage": {"prompt_tokens": 7, "total_tokens": 7}}).encode())


CLIENT = httpx.AsyncClient()


# ── validação ──

def test_prepare_valida_cada_modalidade():
    assert modalities_api.prepare("embeddings", {"model": "m", "input": ["a", "b"]}) == ("m", "a\nb")
    assert modalities_api.prepare("embeddings", {"model": "m", "input": "x"}) == ("m", "x")
    assert modalities_api.prepare("images", {"model": "m", "prompt": "um gato"}) == ("m", "um gato")
    assert modalities_api.prepare("speech", {"model": "m", "input": "olá", "voice": "alloy"}) == ("m", "olá")
    for kind, bad in (("embeddings", {"model": "m"}), ("embeddings", {"model": "m", "input": []}),
                      ("embeddings", {"model": "m", "input": [{"x": 1}]}), ("embeddings", {"input": "x"}),
                      ("images", {"model": "m", "prompt": " "}), ("speech", {"model": "m", "input": "x"}),
                      ("speech", {"model": "m", "input": "x" * 5000, "voice": "v"}), ("embeddings", [1])):
        with pytest.raises(ValueError):
            modalities_api.prepare(kind, bad)
    with pytest.raises(ValueError):
        modalities_api.prepare("embeddings", {"model": "m", "input": ["x"] * 3000})


# ── adaptadores ──

@pytest.mark.asyncio
async def test_nuvem_envia_para_o_caminho_certo_com_a_chave():
    seen = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200, json={"ok": True})

    ad = CloudOpenAIGatewayAdapter(name="deep", base_url="https://api.deepseek.com/v1", default_model="m",
                                   key_manager=KeyManager(["k" * 20], label="deep"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        for kind, path in (("embeddings", "/v1/embeddings"), ("images", "/v1/images/generations"),
                           ("speech", "/v1/audio/speech")):
            r = await ad.modality(kind, {"model": "m", "input": "x"}, c)
            assert r.status == 200 and json.loads(r.content) == {"ok": True}
            assert seen[-1][0] == path and seen[-1][1] == "Bearer " + "k" * 20
    assert not ad.supports("chat") and ad.supports("embeddings")


@pytest.mark.asyncio
async def test_nuvem_transcricao_manda_multipart_sem_content_type_json():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["ct"], seen["body"], seen["path"] = request.headers["content-type"], request.content, request.url.path
        return httpx.Response(200, json={"text": "olá"})

    ad = CloudOpenAIGatewayAdapter(name="g", base_url="https://api.groq.com/openai/v1", default_model="m",
                                   key_manager=KeyManager(["k" * 20], label="g"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await ad.modality("transcription", {"model": "whisper"}, c, files={"file": ("a.wav", b"RIFFdata", "audio/wav")})
    assert seen["path"] == "/openai/v1/audio/transcriptions" and seen["ct"].startswith("multipart/form-data; boundary=")
    assert b"RIFFdata" in seen["body"] and b'name="model"' in seen["body"] and json.loads(r.content) == {"text": "olá"}


@pytest.mark.asyncio
async def test_nuvem_respeita_a_lista_de_modalidades_e_trata_429_e_401():
    ad = CloudOpenAIGatewayAdapter(name="x", base_url="https://api.deepseek.com/v1", default_model="m",
                                   key_manager=KeyManager(["k" * 20], label="x"), modalities=["embeddings"])
    assert ad.supports("embeddings") and not ad.supports("images")
    with pytest.raises(GatewayProviderError) as exc:
        await ad.modality("images", {}, CLIENT)
    assert exc.value.status_code == 404 and exc.value.retryable

    async def limited(request):
        return httpx.Response(429, headers={"retry-after": "5"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as c:
        with pytest.raises(GatewayProviderError) as exc:
            await ad.modality("embeddings", {"model": "m", "input": "x"}, c)
    assert exc.value.status_code == 429 and exc.value.retryable


@pytest.mark.asyncio
async def test_ollama_embeddings_no_formato_openai():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/embed" and json.loads(request.content) == {"model": "nomic", "input": ["a", "b"]}
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2], [0.3, 0.4]], "prompt_eval_count": 6})

    ad = OllamaGatewayAdapter()
    assert ad.supports("embeddings") and not ad.supports("images")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        r = await ad.modality("embeddings", {"model": "nomic", "input": ["a", "b"]}, c)
    d = json.loads(r.content)
    assert d["object"] == "list" and [e["index"] for e in d["data"]] == [0, 1] and d["data"][1]["embedding"] == [0.3, 0.4]
    assert d["usage"] == {"prompt_tokens": 6, "total_tokens": 6}
    with pytest.raises(GatewayProviderError):
        await ad.modality("images", {}, CLIENT)


# ── roteador ──

@pytest.mark.asyncio
async def test_roteador_escolhe_quem_atende_e_faz_fallback():
    a = FakeMod("a", models=("emb",), error=GatewayProviderError("fora", status_code=503, retryable=True))
    b = FakeMod("b", models=("emb",))
    only_chat = FakeMod("c", kinds=(), models=("emb",))                      # não atende a modalidade: nunca é chamado
    r = TierRouter(adapters=[a, b, only_chat])
    resp = await r.route_modality("embeddings", "emb", "texto", {"input": "texto"}, CLIENT)
    assert json.loads(resp.content)["by"] == "b" and a.calls and not only_chat.calls
    assert b.calls[0][1] == {"input": "texto", "model": "emb"}


@pytest.mark.asyncio
async def test_roteador_endereco_direto_apelido_e_modelo_sem_atendente():
    a, b = FakeMod("a", models=("ma",)), FakeMod("b", models=("mb",))
    r = TierRouter(adapters=[a, b])
    r.aliases = {"vetores": "b/mb"}
    assert json.loads((await r.route_modality("embeddings", "b/mb", "x", {"input": "x"}, CLIENT)).content)["by"] == "b"
    assert json.loads((await r.route_modality("embeddings", "vetores", "x", {"input": "x"}, CLIENT)).content)["model"] == "mb"
    with pytest.raises(GatewayProviderError) as exc:
        await r.route_modality("images", "nao-existe", "x", {}, CLIENT)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_roteador_erro_nao_retentavel_para_na_hora():
    a = FakeMod("a", models=("m",), error=GatewayProviderError("pedido ruim", status_code=400, retryable=False))
    b = FakeMod("b", models=("m",))
    with pytest.raises(GatewayProviderError) as exc:
        await TierRouter(adapters=[a, b]).route_modality("images", "m", "x", {}, CLIENT)
    assert exc.value.status_code == 400 and not b.calls


@pytest.mark.asyncio
async def test_dlp_e_modo_offline_barram_a_nuvem():
    cloud = FakeMod("nuvem", models=("emb",))
    r = TierRouter(adapters=[cloud])
    with pytest.raises(GatewayProviderError) as exc:                        # offline: só local, e o local falso não existe
        await r.route_modality("embeddings", "emb", "oi", {"input": "oi"}, CLIENT, force_offline=True)
    assert exc.value.status_code in (403, 404, 503) and not cloud.calls
    with pytest.raises(GatewayProviderError):                               # segredo no texto: DLP barra a nuvem
        await r.route_modality("embeddings", "emb", "minha chave sk-proj-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                               {"input": "x"}, CLIENT)
    assert not cloud.calls


# ── rotas HTTP ──

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "config_mgr", ConfigManager(config_path=str(tmp_path / "config.yaml")))
    monkeypatch.setattr(server, "SECRET_STORE", SecretStore(tmp_path / "s.json", use_dpapi=False))
    monkeypatch.setattr(server, "USAGE", UsageLedger(tmp_path / "usage.db"))
    fake = FakeMod("groq", models=("emb-1",))
    monkeypatch.setattr(server.gateway_tier_router, "adapters", [fake])
    monkeypatch.setattr(server.gateway_tier_router, "usage", server.USAGE)
    monkeypatch.setattr(server.gateway_tier_router, "aliases", {})
    monkeypatch.setattr(server.router, "default_offline", False)
    monkeypatch.setitem(server.RUNTIME_FLAGS, "rtk", False)
    created = []
    yield fake, created
    for key_id in created:
        server.CLIENT_KEYS.delete(key_id)


def master():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS)


@pytest.mark.asyncio
async def test_rotas_json_embeddings_imagem_e_fala(api):
    fake, _ = api
    async with master() as c:
        e = await c.post("/v1/embeddings", json={"model": "emb-1", "input": "olá"})
        assert e.status_code == 200 and e.json()["kind"] == "embeddings" and e.headers["content-type"].startswith("application/json")
        i = await c.post("/v1/images/generations", json={"model": "emb-1", "prompt": "um gato"})
        assert i.status_code == 200 and i.json()["kind"] == "images"
        s = await c.post("/v1/audio/speech", json={"model": "emb-1", "input": "olá", "voice": "alloy"})
        assert s.status_code == 200 and s.content == b"\x00AUDIO" and s.headers["content-type"] == "audio/mpeg"
    assert [k for k, _, _ in fake.calls] == ["embeddings", "images", "speech"]


@pytest.mark.asyncio
async def test_rota_transcricao_multipart(api):
    fake, _ = api
    async with master() as c:
        r = await c.post("/v1/audio/transcriptions", data={"model": "emb-1", "language": "pt"},
                         files={"file": ("a.wav", b"RIFF1234", "audio/wav")})
        assert r.status_code == 200 and r.json()["kind"] == "transcription"
        kind, payload, files = fake.calls[-1]
        assert payload == {"language": "pt", "model": "emb-1"} and files["file"][1] == b"RIFF1234" and files["file"][2] == "audio/wav"
        assert (await c.post("/v1/audio/transcriptions", data={"model": "emb-1"})).status_code == 400   # sem arquivo
        assert (await c.post("/v1/audio/transcriptions", files={"file": ("a.wav", b"x", "audio/wav")})).status_code == 400
        big = await c.post("/v1/audio/transcriptions", data={"model": "emb-1"},
                           files={"file": ("a.wav", b"\0" * (modalities_api.MAX_AUDIO_BYTES + 1), "audio/wav")})
        assert big.status_code == 413 and big.json()["error"]["type"] == "request_too_large"


@pytest.mark.asyncio
async def test_rotas_erros_no_formato_openai_e_sem_token(api):
    async with master() as c:
        bad = await c.post("/v1/embeddings", json={"model": "emb-1"})
        assert bad.status_code == 400 and bad.json()["error"]["type"] == "invalid_request_error"
        none = await c.post("/v1/embeddings", json={"model": "sem-dono", "input": "x"})
        assert none.status_code == 404 and none.json()["error"]["type"] == "not_found"
        assert (await c.post("/v1/embeddings", content=b"nao json", headers={"content-type": "application/json"})).status_code == 400
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL) as anon:
        assert (await anon.post("/v1/embeddings", json={"model": "emb-1", "input": "x"})).status_code == 401


@pytest.mark.asyncio
async def test_rota_com_chave_de_cliente_e_uso_registrado(api):
    fake, created = api
    async with master() as c:
        made = (await c.post("/api/router/clients", json={"name": "rag", "models": ["emb-1"],
                                                          "human_code": server.HUMAN_CODE.current()})).json()
        created.append(made["id"])
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL,
                           headers={"Authorization": f"Bearer {made['key']}"}) as k:
        assert (await k.post("/v1/embeddings", json={"model": "emb-1", "input": "x"})).status_code == 200
        denied = await k.post("/v1/embeddings", json={"model": "outro", "input": "x"})
        assert denied.status_code == 403 and denied.json()["error"]["type"] == "permission_denied"
        assert (await k.get("/api/config")).status_code == 401
    today = server.USAGE.today(provider="groq")
    assert today["requests"] >= 1 and today["tokens"] >= 7                    # tokens do embeddings entram no uso
