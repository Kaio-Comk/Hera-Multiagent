"""Fase 1: autenticacao local. Cada teste reproduz um ataque que funcionava antes da correcao
(qualquer site aberto no navegador chamando http://127.0.0.1:8000)."""
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.security import LocalSecurityMiddleware, SecuritySettings, load_or_create_token
from app.server import SECURITY, app
from helpers import AUTH_HEADERS, TEST_BASE_URL, TEST_TOKEN

ANON = TestClient(app, base_url=TEST_BASE_URL)                    # sem credencial
AUTH = TestClient(app, base_url=TEST_BASE_URL, headers=AUTH_HEADERS)

ROTAS_PROTEGIDAS = [
    ("GET", "/api/config"), ("GET", "/api/health"), ("GET", "/api/tasks"), ("GET", "/api/hardware"),
    ("GET", "/api/vault/search?q=x"), ("GET", "/v1/models"), ("GET", "/api/hitl/proposals"),
    ("GET", "/api/mcp/servers"), ("GET", "/api/arena/runs"),
    ("POST", "/api/tasks/run"), ("POST", "/api/config"), ("POST", "/api/hitl/nonces"),
    ("POST", "/api/hitl/approve"), ("POST", "/api/mcp/execute"), ("POST", "/v1/chat/completions"),
    ("POST", "/api/ollama/pull"), ("POST", "/api/arena/run"),
]


@pytest.mark.parametrize("method,path", ROTAS_PROTEGIDAS)
def test_sem_token_nenhuma_rota_responde(method, path):
    r = ANON.request(method, path, json={})
    assert r.status_code == 401, f"{method} {path} respondeu {r.status_code} sem token"
    assert r.headers.get("www-authenticate") == "Bearer"


def test_token_errado_e_recusado_e_os_dois_formatos_validos_funcionam():
    assert ANON.get("/api/config", headers={"X-Hera-Token": "x" * 40}).status_code == 401
    assert ANON.get("/api/config", headers={"Authorization": "Bearer " + "y" * 40}).status_code == 401
    assert ANON.get("/api/config", headers={"X-Hera-Token": TEST_TOKEN}).status_code == 200
    assert ANON.get("/api/config", headers={"Authorization": f"Bearer {TEST_TOKEN}"}).status_code == 200
    assert ANON.get("/v1/models", headers={"Authorization": f"Bearer {TEST_TOKEN}"}).status_code == 200


def test_publicas_so_a_ui_e_o_healthz_sem_dados():
    assert ANON.get("/healthz").json() == {"status": "ok"}
    assert ANON.get("/").status_code == 200
    assert "ollama" not in ANON.get("/healthz").text.lower()          # nada de modelos nem erros internos


def test_ui_recebe_o_token_embutido_e_nao_e_cacheavel():
    r = ANON.get("/")
    assert f'window.HERA_TOKEN = "{TEST_TOKEN}"' in r.text
    assert r.headers["cache-control"] == "no-store"


def test_host_forjado_e_recusado_mesmo_com_token_valido():          # DNS rebinding
    for host in ("evil.com", "evil.com:8000", "127.0.0.1.evil.com", "10.0.0.5"):
        r = AUTH.get("/api/config", headers={"Host": host})
        assert r.status_code == 400, host
    assert AUTH.get("/api/config", headers={"Host": "localhost:8000"}).status_code == 200
    assert AUTH.get("/api/config", headers={"Host": "[::1]:8000"}).status_code == 200


def test_origin_externo_e_recusado_mesmo_com_token_valido():        # CSRF / leitura cross-site
    for origin in ("http://evil.com", "https://evil.com", "null", "http://127.0.0.1:9999",
                   "http://localhost:8000"):                          # outra origem, mesmo que local
        r = AUTH.get("/api/config", headers={"Origin": origin})
        assert r.status_code == 403, origin
    assert AUTH.get("/api/config", headers={"Origin": "http://127.0.0.1:8000"}).status_code == 200


def test_preflight_cors_de_site_externo_nao_recebe_permissao():
    r = ANON.options("/api/tasks/run", headers={
        "Origin": "http://evil.com", "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type,x-hera-token"})
    assert r.status_code == 403
    assert "access-control-allow-origin" not in {k.lower() for k in r.headers}


def test_nenhuma_resposta_libera_cors_curinga():
    for path in ("/", "/healthz", "/api/config"):
        r = AUTH.get(path, headers={"Origin": "http://127.0.0.1:8000"})
        assert r.headers.get("access-control-allow-origin") in (None,)


def test_cabecalhos_contra_clickjacking_em_todas_as_respostas():
    for r in (ANON.get("/"), ANON.get("/api/config"), AUTH.get("/api/config")):
        assert r.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert r.headers["x-content-type-options"] == "nosniff"


# ── WebSocket ──────────────────────────────────────────────────────────────────
# O cliente de teste do Starlette manda `Host: testserver` no WebSocket; passamos o Host real
# para cada teste falhar (ou passar) pelo MOTIVO que ele afirma.
WS_HOST = {"Host": "127.0.0.1:8000"}


def _ws_motivo(url, headers):
    with pytest.raises(WebSocketDisconnect) as exc:
        with ANON.websocket_connect(url, headers=headers):
            pass
    return exc.value.reason


def test_websocket_sem_token_ou_com_token_errado_e_fechado():
    assert _ws_motivo("/ws/events", WS_HOST) == "Token ausente ou invalido"
    assert _ws_motivo("/ws/events?token=" + "z" * 40, WS_HOST) == "Token ausente ou invalido"


def test_websocket_de_origem_externa_e_recusado_mesmo_com_token():   # CSWSH
    url = f"/ws/events?token={TEST_TOKEN}"
    assert _ws_motivo(url, {**WS_HOST, "Origin": "http://evil.com"}) == "Origin nao permitido"
    assert _ws_motivo(url, {"Host": "evil.com"}) == "Host nao permitido"


def test_websocket_com_token_e_origem_propria_conecta():
    with ANON.websocket_connect(f"/ws/events?token={TEST_TOKEN}",
                                headers={**WS_HOST, "Origin": "http://127.0.0.1:8000"}) as ws:
        ws.send_text("ping")


def test_websocket_aceita_token_so_na_query_e_http_nao():
    # navegadores nao enviam cabecalhos no WebSocket, mas a query NAO vale para HTTP comum
    assert ANON.get(f"/api/config?token={TEST_TOKEN}").status_code == 401


# ── limite de taxa ─────────────────────────────────────────────────────────────
def test_limite_de_taxa_por_token_nas_rotas_caras():
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route

    async def ok(request):
        return PlainTextResponse("ok")
    mini = Starlette(routes=[Route("/api/arena/run", ok, methods=["POST"]), Route("/api/outra", ok)])
    settings = SecuritySettings(token="t" * 32, rate_limits={"/api/arena/run": (3, 60.0)})
    mini.add_middleware(LocalSecurityMiddleware, settings=settings)
    c = TestClient(mini, base_url="http://127.0.0.1:8000", headers={"X-Hera-Token": "t" * 32})
    assert [c.post("/api/arena/run").status_code for _ in range(3)] == [200, 200, 200]
    r = c.post("/api/arena/run")
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert c.get("/api/outra").status_code == 200                      # rota sem limite


# ── token ──────────────────────────────────────────────────────────────────────
def test_token_e_criado_uma_vez_e_reaproveitado(tmp_path):
    f = tmp_path / "data" / ".hera_token"
    t1 = load_or_create_token(f, environ={})
    assert len(t1) >= 32 and f.read_text(encoding="utf-8") == t1
    assert load_or_create_token(f, environ={}) == t1


def test_variavel_de_ambiente_tem_prioridade_e_token_curto_e_ignorado(tmp_path):
    f = tmp_path / ".hera_token"
    assert load_or_create_token(f, environ={"HERA_API_KEY": "a" * 30}) == "a" * 30
    assert not f.exists()
    assert load_or_create_token(f, environ={"HERA_API_KEY": "curto"}) != "curto"


def test_hosts_extras_por_ambiente(tmp_path):
    s = SecuritySettings.from_env(tmp_path / "t", environ={"HERA_ALLOWED_HOSTS": "meu-pc.local, Outro"})
    assert {"meu-pc.local", "outro", "127.0.0.1"} <= s.allowed_hosts


def test_token_da_sessao_nunca_aparece_em_respostas_de_api():
    for path in ("/api/config", "/api/health", "/api/tasks", "/v1/models"):
        assert SECURITY.token not in AUTH.get(path).text
