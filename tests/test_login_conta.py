"""Entrar com a sua conta: OpenRouter (PKCE oficial, devolve uma chave sua) e Claude Code (CLI oficial)."""
import base64
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from app.config_manager import ConfigManager
from app.gateway.oauth import KEY_LOGINS, OAuthError, OAuthManager
from app.gateway.secret_store import SecretStore
from helpers import AUTH_HEADERS, TEST_BASE_URL


class Clock:
    now = 1_000_000.0

    def __call__(self):
        return self.now


KEY = "sk-or-v1-" + "a1b2c3d4" * 6


def exchange_ok(seen):
    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), json.loads(request.content)))
        body = seen[-1][1]
        return httpx.Response(200, json={"key": KEY}) if body.get("code") == "BOM" else httpx.Response(400, json={"error": "bad"})
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def manager():
    return OAuthManager({}, SecretStore.__new__(SecretStore), environ={}, clock=Clock())


# ── fluxo da chave (OpenRouter) ──

def test_url_de_login_segue_a_documentacao_do_servico():
    m = manager()
    info = m.start_key_login("openrouter", "http://127.0.0.1:8000/api/oauth/callback")
    url = urlsplit(info["authorize_url"])
    q = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert f"{url.scheme}://{url.netloc}{url.path}" == "https://openrouter.ai/auth" == KEY_LOGINS["openrouter"]["auth_url"]
    assert q["callback_url"] == "http://localhost:8000/api/oauth/callback"           # a doc fala em localhost
    assert q["code_challenge_method"] == "S256" and len(q["state"]) >= 40 and q["key_label"] == "Hera"
    with pytest.raises(OAuthError) as exc:
        m.start_key_login("groq", "http://localhost:8000/cb")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_troca_do_codigo_envia_o_verifier_e_entrega_a_chave():
    m, seen, got = manager(), [], []
    m.on_key = lambda name, key: got.append((name, key))
    info = m.start_key_login("openrouter", "http://localhost:8000/api/oauth/callback")
    q = {k: v[0] for k, v in parse_qs(urlsplit(info["authorize_url"]).query).items()}
    async with exchange_ok(seen) as c:
        assert await m.callback(q["state"], "BOM", c) == "openrouter"
    url, body = seen[0]
    assert url == "https://openrouter.ai/api/v1/auth/keys" and body["code"] == "BOM" and body["code_challenge_method"] == "S256"
    assert base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest()).rstrip(b"=").decode() == q["code_challenge"]
    assert got == [("openrouter", KEY)]


@pytest.mark.asyncio
async def test_state_unico_codigo_ruim_e_chave_invalida_nao_entregam_nada():
    m, seen, got = manager(), [], []
    m.on_key = lambda name, key: got.append(key)
    state = lambda: parse_qs(urlsplit(m.start_key_login("openrouter", "http://localhost:8000/cb")["authorize_url"]).query)["state"][0]   # noqa: E731
    async with exchange_ok(seen) as c:
        s1 = state()
        await m.callback(s1, "BOM", c)
        with pytest.raises(OAuthError):
            await m.callback(s1, "BOM", c)                                          # replay
        with pytest.raises(OAuthError) as exc:
            await m.callback(state(), "RUIM", c)
        assert exc.value.status_code == 400 and KEY not in str(exc.value)
        with pytest.raises(OAuthError):
            await m.callback("state-inventado", "BOM", c)
    assert got == [KEY]

    async def short(request):
        return httpx.Response(200, json={"key": "curta"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(short)) as c2:
        with pytest.raises(OAuthError):
            await m.callback(state(), "BOM", c2)                                    # chave fora do formato
    assert got == [KEY]


@pytest.mark.asyncio
async def test_sem_destino_para_a_chave_falha_sem_vazar():
    m, seen = manager(), []
    st = parse_qs(urlsplit(m.start_key_login("openrouter", "http://localhost:8000/cb")["authorize_url"]).query)["state"][0]
    async with exchange_ok(seen) as c:
        with pytest.raises(OAuthError) as exc:
            await m.callback(st, "BOM", c)
    assert exc.value.status_code == 500 and KEY not in str(exc.value)


# ── rotas ──

@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "config_mgr", ConfigManager(config_path=str(tmp_path / "config.yaml")))
    monkeypatch.setattr(server, "SECRET_STORE", SecretStore(tmp_path / "keys.json", use_dpapi=False))
    seen = []
    monkeypatch.setattr(server, "gateway_http_client", exchange_ok(seen))
    monkeypatch.setattr(server.gateway_tier_router, "adapters", list(server.gateway_tier_router.adapters))
    server._CLAUDE_STATUS_CACHE.update(at=0.0, value=None)
    return seen


def master():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS)


@pytest.mark.asyncio
async def test_login_openrouter_pelas_rotas_liga_o_provedor_com_a_chave(api):
    async with master() as c:
        assert (await c.post("/api/router/login/groq/start", json={"human_code": server.HUMAN_CODE.current()})).status_code == 404
        started = (await c.post("/api/router/login/openrouter/start", json={"human_code": server.HUMAN_CODE.current()})).json()
    state = parse_qs(urlsplit(started["authorize_url"]).query)["state"][0]
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL) as browser:      # volta do navegador, sem token
        bad = await browser.get("/api/oauth/callback", params={"state": state, "code": "RUIM"})
        assert bad.status_code == 400
        state2 = parse_qs(urlsplit((await _again(c_start=True))["authorize_url"]).query)["state"][0]
        ok = await browser.get("/api/oauth/callback", params={"state": state2, "code": "BOM"})
        assert ok.status_code == 200 and "conectado" in ok.text.lower()
    async with master() as c:
        st = (await c.get("/api/router/state")).json()
    prov = next(p for p in st["providers"] if p["name"] == "openrouter")
    assert prov["enabled"] is True and prov["keys_stored"] == 1 and prov["active"] is True
    assert KEY not in json.dumps(st)                                                 # a chave nunca volta pela API
    assert server.SECRET_STORE.get("openrouter") == [KEY]


async def _again(c_start):
    async with master() as c:
        return (await c.post("/api/router/login/openrouter/start", json={"human_code": server.HUMAN_CODE.current()})).json()


@pytest.mark.asyncio
async def test_preset_do_openrouter_anuncia_o_login_por_conta(api):
    async with master() as c:
        presets = {p["id"]: p for p in (await c.get("/api/router/state")).json()["presets"]}
    assert presets["openrouter"]["login"] == "key_login" and "login" not in presets["groq"]


# ── Claude Code (CLI oficial) ──

@pytest.mark.asyncio
async def test_status_do_claude_mascara_o_email_e_usa_cache(api, monkeypatch):
    calls = []

    async def fake_run(args, timeout):
        calls.append(args)
        return 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai", "email": "pessoa@exemplo.com",
                              "subscriptionType": "pro", "orgId": "segredo-org"})

    monkeypatch.setattr(server, "_run_claude", fake_run)
    monkeypatch.setattr(server, "_claude_binary", lambda: "claude")
    async with master() as c:
        r = (await c.get("/api/auth/claude/status")).json()
        again = (await c.get("/api/auth/claude/status")).json()
        fresh = (await c.get("/api/auth/claude/status", params={"fresh": "true"})).json()
    assert r == {"available": True, "logged_in": True, "method": "claude.ai", "subscription": "pro", "account": "p***@exemplo.com"}
    assert again == r and "segredo-org" not in json.dumps(r) and "pessoa@" not in json.dumps(r)
    assert calls == [["auth", "status", "--json"]] * 2                              # a 2a chamada veio do cache; `fresh` refaz


@pytest.mark.asyncio
async def test_status_do_claude_sem_cli_ou_com_erro(api, monkeypatch):
    monkeypatch.setattr(server, "_claude_binary", lambda: None)
    async with master() as c:
        none = (await c.get("/api/auth/claude/status")).json()
        assert none["available"] is False and none["logged_in"] is False
        server._CLAUDE_STATUS_CACHE.update(at=0.0, value=None)
        monkeypatch.setattr(server, "_claude_binary", lambda: "claude")

        async def broken(args, timeout):
            return 1, "lixo"

        monkeypatch.setattr(server, "_run_claude", broken)
        bad = (await c.get("/api/auth/claude/status")).json()
    assert bad["available"] is True and bad["logged_in"] is False and "error" in bad


@pytest.mark.asyncio
async def test_login_do_claude_exige_codigo_e_abre_o_terminal_oficial(api, monkeypatch):
    monkeypatch.setattr(server, "_claude_binary", lambda: "C:/fake/claude.exe")
    opened = []
    monkeypatch.setattr(server.subprocess, "Popen", lambda args, **kw: opened.append((args, kw)))
    monkeypatch.setattr(server.sys, "platform", "win32")
    async with master() as c:
        ok = await c.post("/api/auth/claude/login", json={"human_code": server.HUMAN_CODE.current(), "mode": "console"})
        assert ok.status_code == 200 and ok.json()["started"] is True
        assert (await c.post("/api/auth/claude/login", json={"human_code": server.HUMAN_CODE.current(), "mode": "x; rm -rf"})).status_code == 422
    args, kw = opened[0]
    assert args == ["C:/fake/claude.exe", "auth", "login", "--console"] and "shell" not in kw      # sem shell, args fixos


@pytest.mark.asyncio
async def test_login_do_claude_fora_do_windows_devolve_o_comando(api, monkeypatch):
    monkeypatch.setattr(server, "_claude_binary", lambda: "claude")
    monkeypatch.setattr(server.sys, "platform", "linux")
    async with master() as c:
        r = (await c.post("/api/auth/claude/login", json={"human_code": server.HUMAN_CODE.current()})).json()
    assert r["started"] is False and r["command"] == "claude auth login --claudeai"


@pytest.mark.asyncio
async def test_logout_do_claude(api, monkeypatch):
    monkeypatch.setattr(server, "_claude_binary", lambda: "claude")
    ran = []

    async def fake_run(args, timeout):
        ran.append(args)
        return 0, ""

    monkeypatch.setattr(server, "_run_claude", fake_run)
    async with master() as c:
        assert (await c.post("/api/auth/claude/logout", json={})).status_code == 200
        monkeypatch.setattr(server, "_run_claude", lambda a, t: _fail())
    assert ran == [["auth", "logout"]]


async def _fail():
    return 1, ""


@pytest.mark.asyncio
async def test_rotas_de_login_exigem_token(api):
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL) as anon:
        for method, url in (("get", "/api/auth/claude/status"), ("post", "/api/auth/claude/login"),
                            ("post", "/api/auth/claude/logout"), ("post", "/api/router/login/openrouter/start")):
            assert (await getattr(anon, method)(url)).status_code == 401, url
