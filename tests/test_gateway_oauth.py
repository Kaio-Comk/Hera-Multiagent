"""OAuth por provedor: authorization_code (PKCE), device_code, client_credentials, renovação e integração com o gateway."""
import asyncio
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from app.gateway.factory import build_gateway_adapters
from app.gateway.oauth import (
    OAuthError, OAuthKeyManager, OAuthManager, OAuthProvider, load_oauth_providers, pkce_pair,
)
from app.gateway.key_manager import NoKeyAvailableError
from app.gateway.providers import CloudOpenAIGatewayAdapter, GatewayProviderError
from app.gateway.secret_store import SecretStore
from helpers import AUTH_HEADERS, TEST_BASE_URL

TOKEN_URL = "https://idp.example.com/oauth/token"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


class IdP:
    """Servidor de autorização simulado: guarda o que recebeu e responde conforme o `grant_type`."""

    def __init__(self):
        self.calls, self.refresh_count, self.fail_refresh = [], 0, None
        self.device_answers = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append((str(request.url), form))
        if request.url.path.endswith("/device"):
            return httpx.Response(200, json={"device_code": "DEV", "user_code": "ABCD-1234", "interval": 5,
                                             "verification_uri": "https://idp.example.com/activate", "expires_in": 600})
        grant = form.get("grant_type")
        if grant == "authorization_code":
            if form.get("code") != "GOOD":
                return httpx.Response(400, json={"error": "invalid_grant"})
            return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600,
                                             "scope": "chat", "token_type": "Bearer"})
        if grant == "refresh_token":
            self.refresh_count += 1
            await asyncio.sleep(0.01)
            if self.fail_refresh:
                return httpx.Response(400, json={"error": self.fail_refresh, "error_description": "revogado"})
            return httpx.Response(200, json={"access_token": f"AT{self.refresh_count + 1}", "expires_in": 3600})
        if grant == "client_credentials":
            return httpx.Response(200, json={"access_token": "CC1", "expires_in": 100})
        if grant == "urn:ietf:params:oauth:grant-type:device_code":
            if self.device_answers:
                return self.device_answers.pop(0)
            return httpx.Response(200, json={"access_token": "DT1", "refresh_token": "DR1", "expires_in": 3600})
        return httpx.Response(400, json={"error": "unsupported_grant_type"})


def provider(grant="authorization_code", **kw):
    base = dict(name="idp", grant=grant, client_id="my-app", token_url=TOKEN_URL,
                auth_url="https://idp.example.com/oauth/authorize?tenant=t1", device_url="https://idp.example.com/device",
                scopes=("chat", "offline_access"))
    base.update(kw)
    return OAuthProvider(**base)


@pytest.fixture
def env(tmp_path):
    idp, clock = IdP(), Clock()
    store = SecretStore(tmp_path / "oauth.json", use_dpapi=False)
    client = httpx.AsyncClient(transport=httpx.MockTransport(idp))
    mgr = OAuthManager({"idp": provider()}, store, environ={"IDP_SECRET": "s3cr3t-value"}, clock=clock)
    return mgr, idp, clock, client, store


# ── configuração ──

def test_config_valida_e_ignora_o_que_e_inseguro():
    ok = {"grant": "authorization_code", "client_id": "a", "auth_url": "https://i.test/a", "token_url": "https://i.test/t"}
    cfg = {"oauth": {
        "bom": ok,
        "http-puro": {**ok, "token_url": "http://i.test/t"},
        "sem-client": {**ok, "client_id": ""},
        "fluxo-ruim": {**ok, "grant": "password"},
        "Nome Ruim": ok,
        "cc-sem-segredo": {"grant": "client_credentials", "client_id": "a", "token_url": "https://i.test/t"},
        "redir-ruim": {**ok, "redirect_uri": "http://evil.example.com/cb"},
        "redir-ok": {**ok, "redirect_uri": "http://127.0.0.1:8000/api/oauth/callback"},
        "extra": {**ok, "extra": {"audience": "api", "client_secret": "x", "state": "y"}},
    }}
    got = load_oauth_providers(cfg, url_validator=lambda url, **kw: url)
    assert sorted(got) == ["bom", "extra", "redir-ok"]
    assert dict(got["extra"].extra) == {"audience": "api"}                      # parâmetros sensíveis não passam por `extra`


def test_url_reprovada_pelo_ssrf_e_ignorada():
    from app.gateway.provider_policy import SSRFSecurityError

    def blocked(url, **kw):
        raise SSRFSecurityError("interno")

    cfg = {"oauth": {"x": {"grant": "device_code", "client_id": "a", "token_url": "https://10.0.0.1/t",
                           "device_url": "https://10.0.0.1/d"}}}
    assert load_oauth_providers(cfg, url_validator=blocked) == {}


def test_pkce_s256():
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128
    assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert pkce_pair()[0] != verifier


# ── authorization_code ──

@pytest.mark.asyncio
async def test_fluxo_authorization_code_completo(env):
    mgr, idp, clock, client, store = env
    started = await mgr.start("idp", "http://127.0.0.1:8000/api/oauth/callback", client)
    url = urlsplit(started["authorize_url"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert q["tenant"] == "t1" and q["response_type"] == "code" and q["client_id"] == "my-app"      # query original preservada
    assert q["code_challenge_method"] == "S256" and q["scope"] == "chat offline_access"
    assert q["redirect_uri"] == "http://127.0.0.1:8000/api/oauth/callback" and len(q["state"]) >= 40
    assert not mgr.status("idp")["connected"] and mgr.status("idp")["pending"]

    assert await mgr.callback(q["state"], "GOOD", client) == "idp"
    form = idp.calls[-1][1]
    verifier = form["code_verifier"]
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode() == q["code_challenge"]
    assert form["redirect_uri"] == q["redirect_uri"] and form["client_id"] == "my-app" and "client_secret" not in form
    st = mgr.status("idp")
    assert st["connected"] and st["has_refresh"] and st["expires_in"] == 3600 and "AT1" not in json.dumps(st)
    assert "AT1" in store.get_raw("oauth:idp")                                   # token só no cofre


@pytest.mark.asyncio
async def test_state_e_de_uso_unico_expira_e_desconhecido_falha(env):
    mgr, idp, clock, client, _ = env
    state = parse_qs(urlsplit((await mgr.start("idp", "http://127.0.0.1/cb", client))["authorize_url"]).query)["state"][0]
    with pytest.raises(OAuthError):
        await mgr.callback("state-inventado", "GOOD", client)
    await mgr.callback(state, "GOOD", client)
    with pytest.raises(OAuthError):                                              # replay do mesmo state
        await mgr.callback(state, "GOOD", client)
    old = parse_qs(urlsplit((await mgr.start("idp", "http://127.0.0.1/cb", client))["authorize_url"]).query)["state"][0]
    clock.now += 601
    with pytest.raises(OAuthError):                                              # passou de 10 minutos
        await mgr.callback(old, "GOOD", client)


@pytest.mark.asyncio
async def test_codigo_recusado_nao_conecta_e_erro_nao_vaza_segredo(env):
    mgr, idp, clock, client, _ = env
    state = parse_qs(urlsplit((await mgr.start("idp", "http://127.0.0.1/cb", client))["authorize_url"]).query)["state"][0]
    with pytest.raises(OAuthError) as exc:
        await mgr.callback(state, "RUIM", client)
    assert "invalid_grant" in str(exc.value) and not mgr.status("idp")["connected"]


@pytest.mark.asyncio
async def test_limite_de_conexoes_pendentes(env):
    mgr, _, _, client, _ = env
    for _ in range(20):
        await mgr.start("idp", "http://127.0.0.1/cb", client)
    with pytest.raises(OAuthError) as exc:
        await mgr.start("idp", "http://127.0.0.1/cb", client)
    assert exc.value.status_code == 429
    with pytest.raises(OAuthError) as exc:
        await mgr.start("nao-existe", "http://x/cb", client)
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_segredo_do_app_vai_do_ambiente_para_o_token_endpoint(env):
    mgr, idp, _, client, _ = env
    mgr.providers["idp"] = provider(client_secret_env="IDP_SECRET")
    state = parse_qs(urlsplit((await mgr.start("idp", "http://127.0.0.1/cb", client))["authorize_url"]).query)["state"][0]
    await mgr.callback(state, "GOOD", client)
    assert idp.calls[-1][1]["client_secret"] == "s3cr3t-value"


# ── renovação ──

async def _connect(mgr, client):
    state = parse_qs(urlsplit((await mgr.start("idp", "http://127.0.0.1/cb", client))["authorize_url"]).query)["state"][0]
    await mgr.callback(state, "GOOD", client)


@pytest.mark.asyncio
async def test_renova_so_quando_esta_vencendo_e_guarda_o_refresh_antigo(env):
    mgr, idp, clock, client, store = env
    await _connect(mgr, client)
    assert await mgr.access_token("idp", client) == "AT1" and idp.refresh_count == 0
    clock.now += 3600 - 30                                                       # dentro da folga de 60 s: já renova
    assert await mgr.access_token("idp", client) == "AT2" and idp.refresh_count == 1
    assert json.loads(store.get_raw("oauth:idp"))["refresh_token"] == "RT1"      # a resposta não trouxe novo refresh
    assert idp.calls[-1][1]["refresh_token"] == "RT1"


@pytest.mark.asyncio
async def test_pedidos_simultaneos_renovam_uma_vez_so(env):
    mgr, idp, clock, client, _ = env
    await _connect(mgr, client)
    clock.now += 4000
    tokens = await asyncio.gather(*[mgr.access_token("idp", client) for _ in range(8)])
    assert set(tokens) == {"AT2"} and idp.refresh_count == 1


@pytest.mark.asyncio
async def test_refresh_revogado_desconecta_e_sem_login_da_erro(env):
    mgr, idp, clock, client, store = env
    with pytest.raises(OAuthError) as exc:
        await mgr.access_token("idp", client)                                    # nunca conectou
    assert exc.value.status_code == 401
    await _connect(mgr, client)
    clock.now += 4000
    idp.fail_refresh = "invalid_grant"
    with pytest.raises(OAuthError):
        await mgr.access_token("idp", client)
    assert not mgr.status("idp")["connected"] and store.get_raw("oauth:idp") == ""


@pytest.mark.asyncio
async def test_invalidate_forca_renovar_e_disconnect_apaga(env):
    mgr, idp, _, client, store = env
    await _connect(mgr, client)
    mgr.invalidate("idp")
    assert mgr.cached_token("idp") is None
    assert await mgr.access_token("idp", client) == "AT2"
    mgr.disconnect("idp")
    assert not mgr.status("idp")["connected"] and store.get_raw("oauth:idp") == ""


# ── client_credentials e device_code ──

@pytest.mark.asyncio
async def test_client_credentials_conecta_na_hora_e_renova(env):
    mgr, idp, clock, client, _ = env
    mgr.providers["idp"] = provider("client_credentials", client_secret_env="IDP_SECRET")
    assert (await mgr.start("idp", "", client)) == {"grant": "client_credentials", "connected": True}
    form = idp.calls[-1][1]
    assert form["grant_type"] == "client_credentials" and form["client_secret"] == "s3cr3t-value"
    assert await mgr.access_token("idp", client) == "CC1"
    clock.now += 200
    n = len(idp.calls)
    assert await mgr.access_token("idp", client) == "CC1" and len(idp.calls) == n + 1     # venceu: pediu outro


@pytest.mark.asyncio
async def test_device_code_pendente_slow_down_sucesso_e_negado(env):
    mgr, idp, clock, client, _ = env
    mgr.providers["idp"] = provider("device_code")
    info = await mgr.start("idp", "", client)
    assert info["user_code"] == "ABCD-1234" and info["verification_uri"].endswith("/activate") and info["interval"] == 5
    idp.device_answers = [httpx.Response(400, json={"error": "authorization_pending"}),
                          httpx.Response(400, json={"error": "slow_down"})]
    clock.now += 6
    assert (await mgr.poll_device("idp", client)) == {"status": "pending", "retry_in": 5}
    clock.now += 6
    r = await mgr.poll_device("idp", client)
    assert r["status"] == "pending" and r["retry_in"] == 10                      # slow_down: +5 s
    calls = len(idp.calls)
    assert (await mgr.poll_device("idp", client))["retry_in"] >= 1               # poll cedo demais nem chama o servidor
    assert len(idp.calls) == calls
    clock.now += 11
    assert (await mgr.poll_device("idp", client)) == {"status": "connected"} and mgr.status("idp")["connected"]
    await mgr.start("idp", "", client)
    clock.now += 6
    idp.device_answers = [httpx.Response(400, json={"error": "access_denied"})]
    assert (await mgr.poll_device("idp", client))["status"] == "denied"
    with pytest.raises(OAuthError):
        await mgr.poll_device("idp", client)                                     # nada em andamento


@pytest.mark.asyncio
async def test_device_code_expira(env):
    mgr, _, clock, client, _ = env
    mgr.providers["idp"] = provider("device_code")
    await mgr.start("idp", "", client)
    clock.now += 601
    assert (await mgr.poll_device("idp", client))["status"] == "expired"


# ── gerenciador de chaves + adaptador ──

def _adapter(mgr, name="idp-api"):
    return CloudOpenAIGatewayAdapter(name=name, base_url="https://api.openai.com/v1", default_model="m",
                                     key_manager=OAuthKeyManager(mgr, "idp"))


@pytest.mark.asyncio
async def test_adaptador_usa_o_token_oauth_e_renova_antes_de_enviar(env):
    mgr, idp, clock, idp_client, _ = env
    await _connect(mgr, idp_client)
    seen = []

    async def api(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["authorization"])
        return httpx.Response(200, json={"ok": 1})

    ad = _adapter(mgr)
    async with httpx.AsyncClient(transport=httpx.MockTransport(api)) as c:
        mgr_client = idp_client                                                  # o `prepare` usa o cliente do adaptador
        await ad.modality("embeddings", {"model": "m", "input": "x"}, c)
        assert seen == ["Bearer AT1"]
        clock.now += 4000                                                        # vence: o próximo envio renova primeiro
        mgr._test_client = mgr_client
        mgr_calls = idp.refresh_count
        # o adaptador chama prepare(client) com o cliente DO PROVEDOR; aqui ele não alcança o IdP simulado
        await ad.key_manager.prepare(idp_client)
        await ad.modality("embeddings", {"model": "m", "input": "x"}, c)
    assert idp.refresh_count == mgr_calls + 1 and seen[-1] == "Bearer AT2"


@pytest.mark.asyncio
async def test_401_do_provedor_invalida_o_token_e_429_poe_em_espera(env):
    mgr, idp, clock, idp_client, _ = env
    await _connect(mgr, idp_client)
    ad = _adapter(mgr)

    async def unauthorized(request):
        return httpx.Response(401)

    async with httpx.AsyncClient(transport=httpx.MockTransport(unauthorized)) as c:
        with pytest.raises(GatewayProviderError) as exc:
            await ad.modality("embeddings", {"model": "m", "input": "x"}, c)
    assert exc.value.status_code == 401 and "AT1" not in str(exc.value)
    assert mgr.cached_token("idp") is None                                       # token descartado; refresh renova no próximo uso

    mgr2_km = OAuthKeyManager(mgr, "idp")
    await mgr.access_token("idp", idp_client)
    mgr2_km.report_rate_limited("k", 30)
    with pytest.raises(NoKeyAvailableError) as exc2:
        mgr2_km.acquire()
    assert exc2.value.retry_after and exc2.value.retry_after <= 30


def test_gerenciador_sem_login_nao_entrega_chave(env):
    mgr, *_ = env
    km = OAuthKeyManager(mgr, "idp")
    assert len(km) == 1
    with pytest.raises(NoKeyAvailableError) as exc:
        km.acquire()
    assert exc.value.retry_after is None
    assert km.status()[0]["state"] == "disabled"


def test_fabrica_liga_provedor_com_oauth_e_ignora_oauth_desconhecido(env):
    mgr, *_ = env
    cfg = {"providers": {
        "com-oauth": {"enabled": True, "tier": "cloud_cheap", "base_url": "https://api.openai.com/v1",
                      "oauth": "idp", "default_model": "m"},
        "oauth-fantasma": {"enabled": True, "tier": "cloud_cheap", "base_url": "https://api.openai.com/v1",
                           "oauth": "nao-existe", "default_model": "m"}}}
    names = [a.name for a in build_gateway_adapters(cfg, environ={}, oauth=mgr)]
    assert names == ["ollama", "com-oauth"]
    assert [a.name for a in build_gateway_adapters(cfg, environ={})] == ["ollama"]      # sem gerenciador: ignora


# ── rotas ──

@pytest.fixture
def api(tmp_path, monkeypatch):
    idp, clock = IdP(), Clock()
    store = SecretStore(tmp_path / "oauth.json", use_dpapi=False)
    mgr = OAuthManager({"idp": provider()}, store, clock=clock)
    monkeypatch.setattr(server, "oauth_manager", mgr)
    monkeypatch.setattr(server, "gateway_http_client", httpx.AsyncClient(transport=httpx.MockTransport(idp)))
    return mgr, idp


def master():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS)


def anon():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL)


@pytest.mark.asyncio
async def test_rotas_exigem_token_e_codigo_humano(api):
    mgr, _ = api
    async with anon() as a:
        assert (await a.get("/api/oauth/providers")).status_code == 401
        assert (await a.post("/api/oauth/idp/start", json={})).status_code == 401
        assert (await a.post("/api/oauth/idp/poll")).status_code == 401
    async with master() as c:
        assert (await c.get("/api/oauth/providers")).json()["providers"][0]["connected"] is False
        # provedores de nuvem nao pedem mais o codigo humano (14/10/2026); so exigem o token da API (401 acima)
        assert (await c.post("/api/oauth/idp/disconnect", json={})).status_code == 200


@pytest.mark.asyncio
async def test_fluxo_pelas_rotas_com_callback_publico(api):
    mgr, idp = api
    code = server.HUMAN_CODE.current()
    async with master() as c:
        started = (await c.post("/api/oauth/idp/start", json={"human_code": code})).json()
    q = parse_qs(urlsplit(started["authorize_url"]).query)
    assert q["redirect_uri"][0].endswith("/api/oauth/callback") and "code_challenge" in q
    async with anon() as browser:                                                # o navegador volta SEM o token da API
        bad = await browser.get("/api/oauth/callback", params={"state": "inventado", "code": "GOOD"})
        assert bad.status_code == 400 and "inventado" not in bad.text           # nada do que veio na URL é refletido
        denied = await browser.get("/api/oauth/callback", params={"error": "access_denied", "state": q["state"][0]})
        assert denied.status_code == 400
        ok = await browser.get("/api/oauth/callback", params={"state": q["state"][0], "code": "GOOD"})
        assert ok.status_code == 200 and "conectado" in ok.text.lower() and ok.headers["cache-control"] == "no-store"
        assert (await browser.get("/api/oauth/callback", params={"state": q["state"][0], "code": "GOOD"})).status_code == 400
        assert (await browser.get("/api/oauth/providers")).status_code == 401  # só o callback é público
    async with master() as c:
        st = (await c.get("/api/oauth/providers")).json()["providers"][0]
        assert st["connected"] and "AT1" not in json.dumps(st)
        off = await c.post("/api/oauth/idp/disconnect", json={"human_code": server.HUMAN_CODE.current()})
        assert off.status_code == 200 and off.json()["provider"]["connected"] is False
        assert (await c.post("/api/oauth/nao-existe/disconnect", json={"human_code": server.HUMAN_CODE.current()})).status_code == 404


@pytest.mark.asyncio
async def test_callback_com_html_malicioso_no_erro_e_escapado(api):
    mgr, idp = api
    async with anon() as browser:
        r = await browser.get("/api/oauth/callback", params={"state": "<script>alert(1)</script>", "code": "x"})
    assert "<script>" not in r.text


@pytest.mark.asyncio
async def test_erro_do_oauth_segue_o_formato_detail_que_a_ui_le(api):
    async with master() as c:
        r = await c.post("/api/oauth/idp/poll")                                   # nada em andamento
    assert r.status_code == 400 and r.json()["detail"]["error"] == "oauth_error"
    assert "andamento" in r.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_link_de_verificacao_so_aceita_https(env):
    mgr, idp, clock, client, _ = env
    mgr.providers["idp"] = provider("device_code")
    from app.gateway.oauth import _https_or_empty
    assert _https_or_empty("https://idp.example.com/activate") == "https://idp.example.com/activate"
    for bad in ("javascript:alert(1)", "data:text/html,x", "http://idp.example.com", "https://x y", "", None, "//evil.test"):
        assert _https_or_empty(bad) == ""

    async def evil(request):
        return httpx.Response(200, json={"device_code": "D", "user_code": "X", "interval": 5,
                                         "verification_uri": "javascript:alert(1)",
                                         "verification_uri_complete": "data:text/html,<script>1</script>"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(evil)) as c:
        info = await mgr.start("idp", "", c)
    assert info["verification_uri"] == "" and info["verification_uri_complete"] is None


def test_config_yaml_mantem_as_secoes_search_e_oauth(tmp_path):
    """Sem isso o carregador descartava `search:` e `oauth:` e a configuracao nunca chegava ao servidor."""
    import yaml
    from app.config_manager import ConfigManager
    path = tmp_path / "config.yaml"
    path.write_text("""general: {}
search:
  brave: {enabled: true, api_key_env: BRAVE_API_KEY}
oauth:
  meu:
    grant: client_credentials
    client_id: a
    client_secret_env: S
    token_url: https://idp.example.com/t
""", encoding="utf-8")
    mgr = ConfigManager(config_path=str(path))
    cfg = mgr.load_config()
    assert cfg["search"]["brave"]["enabled"] is True and list(cfg["oauth"]) == ["meu"]
    mgr.save_config({**cfg, "gateway": {"aliases": {}}})                       # salvar por outra rota não apaga as seções
    again = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "search" in again and "oauth" in again
    assert ConfigManager(config_path=str(tmp_path / "novo.yaml")).load_config()["search"] == {}
