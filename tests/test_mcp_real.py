"""Cliente MCP real: handshake, descoberta, chamada, política de lançamento, isolamento e falhas."""
import asyncio
import json
import os
from pathlib import Path

import httpx
import psutil
import pytest
from fastapi.testclient import TestClient

import app.server as server
from app.mcp.catalog import McpCatalog, McpPermissionDeniedError, McpSecurityError
from app.mcp.client import McpClientManager
from app.mcp.launch import McpLaunchError, build_env, validate_stdio_server
from app.mcp.schemas import McpServerConfig, McpToolCallRequest
from app.mcp.transport import HttpTransport, McpTransportError
from helpers import AUTH_HEADERS, TEST_BASE_URL

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")


def cfg(**kw):
    base = dict(id="fk", name="fake", transport="stdio", command="python", args=[SCRIPT], timeout_seconds=8.0)
    base.update(kw)
    return McpServerConfig(**base)


def manager(server_cfg=None, **kw):
    catalog = McpCatalog()
    catalog.register_server(server_cfg or cfg())
    return McpClientManager(catalog=catalog, enabled=True, roots_provider=lambda: [ROOT], **kw), catalog


async def ready(m, catalog, *names, effect="write"):
    """Descobre e libera (como um humano faria) as ferramentas pedidas."""
    await m.discover("fk")
    for n in names:
        catalog.set_tool_policy(f"fk.{n}", enabled=True)


def call(name, confirmed=True, **args):
    return McpToolCallRequest(tool_name=f"fk.{name}", arguments=args, confirmed_hitl=confirmed)


# ── política de lançamento ─────────────────────────────────────────────────────
def test_lancamento_valido_passa():
    validate_stdio_server(cfg(), [ROOT])
    validate_stdio_server(cfg(args=[SCRIPT, "--porta", "1"], env={"MEU_TOKEN": "x"}), [ROOT])


@pytest.mark.parametrize("kwargs", [
    {"args": []},                                                        # sem script
    {"args": ["-c", "import os; os.system('calc')"]},                    # código inline
    {"args": ["-m", "http.server"]},                                     # módulo arbitrário
    {"args": ["-"]},                                                     # script pelo stdin
    {"args": ["C:\\Windows\\System32\\evil.py"]},                        # fora das raízes / inexistente
    {"args": [SCRIPT.replace(".py", ".txt")]},                           # não é .py
    {"args": [SCRIPT] + ["x"] * 25},                                     # argumentos demais
    {"args": [SCRIPT, "x" * 600]},                                       # argumento gigante
    {"env": {"PYTHONPATH": "C:\\evil"}}, {"env": {"PATH": "C:\\evil"}}, {"env": {"LD_PRELOAD": "x"}},
    {"env": {"nome minusculo": "x"}}, {"env": {"OK": "x" * 5000}},
    {"command": "node"}, {"command": "bash"},
])
def test_lancamento_perigoso_e_recusado(kwargs):
    with pytest.raises((McpLaunchError, ValueError)):
        validate_stdio_server(cfg(**kwargs), [ROOT])


def test_script_fora_das_raizes_permitidas_e_recusado(tmp_path):
    outside = tmp_path / "srv.py"
    outside.write_text("print(1)")
    with pytest.raises(McpLaunchError, match="raiz permitida"):
        validate_stdio_server(cfg(args=[str(outside)]), [ROOT])
    validate_stdio_server(cfg(args=[str(outside)]), [tmp_path])          # dentro da SUA raiz: ok


def test_ambiente_do_processo_nao_herda_segredos():
    environ = {"PATH": "C:\\bin", "TEMP": "C:\\t", "HERA_API_KEY": "x" * 40, "DEEPSEEK_API_KEY": "sk-segredo",
               "AWS_SECRET_ACCESS_KEY": "y", "OUTRA": "z"}
    env = build_env(cfg(env={"MEU_FLAG": "1"}), environ)
    assert env["PATH"] == "C:\\bin" and env["MEU_FLAG"] == "1" and env["PYTHONUTF8"] == "1"
    assert not ({"HERA_API_KEY", "DEEPSEEK_API_KEY", "AWS_SECRET_ACCESS_KEY", "OUTRA"} & set(env))


# ── descoberta ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_descoberta_pagina_namespace_e_nasce_desligada_e_com_hitl():
    m, catalog = manager()
    try:
        tools = await m.discover("fk")
        names = {t.name for t in tools}
        assert {"fk.echo", "fk.add", "fk.fail", "fk.slow", "fk.huge", "fk.evil"} <= names       # 2 páginas
        assert "fk.nome com espaco!" not in names and len(tools) == 10                      # nome inválido ignorado
        for t in tools:
            assert (t.enabled, t.requires_hitl, t.effect) == (False, True, "write")            # nasce fechada
            assert t.remote_name == t.name.split(".", 1)[1] and t.fingerprint
        evil = catalog.get_tool("fk.evil")
        assert "\x00" not in evil.description and "\x1b" not in evil.description and len(evil.description) <= 1000
    finally:
        await m.close_all()


@pytest.mark.asyncio
async def test_redescobrir_mantem_a_decisao_humana_se_nada_mudou_e_invalida_se_mudou():
    m, catalog = manager()
    try:
        await ready(m, catalog, "echo")
        await m.discover("fk")
        assert catalog.get_tool("fk.echo").enabled is True                    # igual: a decisão continua
        catalog._tools["fk.echo"] = catalog._tools["fk.echo"].model_copy(update={"fingerprint": "outro"})
        await m.discover("fk")
        assert catalog.get_tool("fk.echo").enabled is False                   # "mudou": volta a ser bloqueada
    finally:
        await m.close_all()


def test_politica_humana_sobre_a_ferramenta():
    m, catalog = manager()
    from app.mcp.session import RemoteTool
    catalog.register_discovered_tools("fk", [RemoteTool("echo", "d", {})])
    assert catalog.set_tool_policy("fk.echo", enabled=True).enabled is True
    with pytest.raises(McpSecurityError, match="precisam exigir HITL"):
        catalog.set_tool_policy("fk.echo", enabled=True, requires_hitl=False)         # write sem HITL: não
    ok = catalog.set_tool_policy("fk.echo", enabled=True, effect="read", requires_hitl=False)
    assert ok.effect == "read" and ok.requires_hitl is False
    with pytest.raises(McpSecurityError):
        catalog.set_tool_policy("fk.nao_existe", enabled=True)


# ── execução ───────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_ferramenta_desligada_ou_sem_confirmacao_nao_roda():
    m, catalog = manager()
    try:
        await m.discover("fk")
        with pytest.raises(McpSecurityError, match="desabilitada"):
            await m.execute_tool(call("echo", text="oi"))
        catalog.set_tool_policy("fk.echo", enabled=True)
        with pytest.raises(McpPermissionDeniedError):                         # liberada, mas exige HITL
            await m.execute_tool(call("echo", confirmed=False, text="oi"))
        res = await m.execute_tool(call("echo", text="olá ✓"))
        assert res.success and res.output == "olá ✓"
    finally:
        await m.close_all()


@pytest.mark.asyncio
async def test_chamada_real_valida_o_schema_antes_de_falar_com_o_servidor():
    m, catalog = manager()
    try:
        await ready(m, catalog, "echo", "add", "fail")
        assert (await m.execute_tool(call("add", a=3, b=4))).output == "7"
        for bad in (dict(a=1), dict(a="1", b=2), dict(a=True, b=2)):
            with pytest.raises(McpSecurityError):
                await m.execute_tool(call("add", **bad))
        with pytest.raises(McpSecurityError, match="não permitidos"):
            await m.execute_tool(call("echo", text="x", extra="y"))            # additionalProperties: false
        res = await m.execute_tool(call("fail"))                               # isError do servidor
        assert res.success is False and "de propósito" in res.error
    finally:
        await m.close_all()


@pytest.mark.asyncio
async def test_saida_do_servidor_e_nao_confiavel_dlp_e_truncamento():
    m, catalog = manager()
    try:
        await ready(m, catalog, "leak", "huge")
        leak = await m.execute_tool(call("leak"))
        assert "sk-abcdef0123456789" not in leak.output and "[REDACTED_API_KEY]" in leak.output
        huge = await m.execute_tool(call("huge"))
        assert len(huge.output) < 101_000 and "truncada" in huge.output
    finally:
        await m.close_all()


@pytest.mark.asyncio
async def test_pedido_do_servidor_ao_cliente_e_recusado_sampling():
    m, catalog = manager()
    try:
        await ready(m, catalog, "ask_client")
        res = await m.execute_tool(call("ask_client"))
        assert res.success and "-32601" in res.output                           # o cliente recusou o sampling
    finally:
        await m.close_all()


@pytest.mark.asyncio
async def test_o_processo_recebe_ambiente_minimo_sem_os_segredos_do_hera():
    assert os.environ.get("HERA_API_KEY")                                      # existe no processo de teste (conftest)
    m, catalog = manager(cfg(env={"MEU_FLAG": "1"}))
    try:
        await ready(m, catalog, "env")
        names = (await m.execute_tool(call("env"))).output.split(",")
        assert "HERA_API_KEY" not in names and "HERA_DATA_DIR" not in names
        assert "MEU_FLAG" in names and "PYTHONUTF8" in names
    finally:
        await m.close_all()


# ── falhas ─────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_timeout_encerra_o_servidor_e_a_proxima_chamada_reinicia():
    m, catalog = manager(cfg(timeout_seconds=1.0))
    try:
        await ready(m, catalog, "slow", "echo")
        pid1 = m._sessions["fk"].transport._proc.pid
        res = await m.execute_tool(call("slow"))
        assert res.success is False and "Timeout" in res.error
        assert not psutil.pid_exists(pid1) or psutil.Process(pid1).status() == psutil.STATUS_ZOMBIE   # morreu
        ok = await m.execute_tool(call("echo", text="de novo"))                  # reiniciou sozinho
        assert ok.success and ok.output == "de novo"
        assert m._sessions["fk"].transport._proc.pid != pid1
    finally:
        await m.close_all()


@pytest.mark.asyncio
async def test_servidor_que_morre_no_meio_da_chamada_vira_erro_e_reinicia():
    m, catalog = manager()
    try:
        await ready(m, catalog, "die", "echo")
        res = await m.execute_tool(call("die"))
        assert res.success is False and "encerrou" in res.error
        assert (await m.execute_tool(call("echo", text="vivo"))).output == "vivo"
    finally:
        await m.close_all()


@pytest.mark.asyncio
async def test_versao_de_protocolo_desconhecida_e_recusada_sem_deixar_processo(monkeypatch):
    m, _ = manager(cfg(env={"FAKE_PROTOCOL": "1999-01-01"}))
    with pytest.raises(McpTransportError, match="Não foi possível iniciar"):
        await m.discover("fk")
    assert "fk" not in m._sessions


@pytest.mark.asyncio
async def test_script_inexistente_nao_sobe_e_close_all_encerra_tudo():
    m, catalog = manager()
    await m.discover("fk")
    pid = m._sessions["fk"].transport._proc.pid
    assert psutil.pid_exists(pid)
    await m.close_all()
    await asyncio.sleep(0.3)
    assert not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    bad, _ = manager(cfg(args=[str(Path(SCRIPT).with_name("nao_existe.py"))]))
    with pytest.raises(McpTransportError):
        await bad.discover("fk")


@pytest.mark.asyncio
async def test_servidor_desabilitado_ou_desconhecido_nao_e_descoberto():
    m, _ = manager(cfg(enabled=False))
    with pytest.raises(McpSecurityError):
        await m.discover("fk")
    with pytest.raises(McpSecurityError):
        await m.discover("nao-existe")


# ── HTTP (streamable) ──────────────────────────────────────────────────────────
def http_server_handler(calls):
    async def handler(request: httpx.Request) -> httpx.Response:
        msg = json.loads(request.content)
        calls.append((msg.get("method"), request.headers.get("mcp-session-id"), request.headers.get("mcp-protocol-version")))
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            return httpx.Response(200, headers={"Mcp-Session-Id": "sess-1"}, json={
                "jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": "2025-06-18", "capabilities": {},
                                                        "serverInfo": {"name": "http-fake"}}})
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "tools/list":                                              # resposta em SSE
            body = "data: " + json.dumps({"jsonrpc": "2.0", "id": mid, "result": {"tools": [
                {"name": "ola", "description": "diz olá", "inputSchema": {"type": "object"}}]}}) + "\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body)
        if method == "tools/call":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid, "result": {
                "content": [{"type": "text", "text": "olá do http"}, {"type": "image", "data": "AAAA"}]}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "x"}})
    return handler


def http_manager(handler):
    server_cfg = McpServerConfig(id="fk", name="http", transport="streamable_http", url="https://mcp.example.com/mcp")
    catalog = McpCatalog()
    catalog._servers["fk"] = server_cfg            # o registro normal resolve DNS (anti-SSRF); aqui não há rede
    m = McpClientManager(catalog=catalog, enabled=True, transport_factory=lambda s: HttpTransport(
        s.url, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    return m, catalog


@pytest.mark.asyncio
async def test_http_handshake_sessao_sse_e_chamada():
    calls = []
    m, catalog = http_manager(http_server_handler(calls))
    try:
        tools = await m.discover("fk")
        assert [t.name for t in tools] == ["fk.ola"]
        catalog.set_tool_policy("fk.ola", enabled=True)
        res = await m.execute_tool(McpToolCallRequest(tool_name="fk.ola", arguments={}, confirmed_hitl=True))
        assert res.success and res.output == "olá do http\n[conteúdo 'image' omitido]"
        assert calls[0][1] is None and calls[1][1] == "sess-1" and calls[-1][1] == "sess-1"   # sessão enviada depois
        assert calls[-1][2] == "2025-06-18"                                                    # versão negociada
    finally:
        await m.close_all()


@pytest.mark.asyncio
async def test_http_argumentos_com_segredo_nao_vao_para_servidor_remoto():
    calls = []
    m, catalog = http_manager(http_server_handler(calls))
    try:
        await m.discover("fk")
        catalog.set_tool_policy("fk.ola", enabled=True)
        with pytest.raises(McpSecurityError, match="segredo"):
            await m.execute_tool(McpToolCallRequest(tool_name="fk.ola", confirmed_hitl=True,
                                                    arguments={"q": "minha chave sk-" + "abcdef0123456789" * 2}))
        assert not any(c[0] == "tools/call" for c in calls)                     # nada saiu
    finally:
        await m.close_all()


@pytest.mark.asyncio
async def test_http_erro_do_servidor_vira_resultado_de_falha():
    async def handler(request):
        msg = json.loads(request.content)
        if msg.get("method") in ("initialize",):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": "2025-06-18", "capabilities": {}}})
        if msg.get("method") == "notifications/initialized":
            return httpx.Response(202)
        if msg.get("method") == "tools/list":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": [
                {"name": "x", "description": "", "inputSchema": {}}]}})
        return httpx.Response(500, text="boom")
    m, catalog = http_manager(handler)
    try:
        await m.discover("fk")
        catalog.set_tool_policy("fk.x", enabled=True)
        res = await m.execute_tool(McpToolCallRequest(tool_name="fk.x", arguments={}, confirmed_hitl=True))
        assert res.success is False and "HTTP 500" in res.error
    finally:
        await m.close_all()


# ── API de ponta a ponta ───────────────────────────────────────────────────────
@pytest.fixture
def api_limpa():
    yield
    for name in [n for n in server.mcp_catalog._tools if n.startswith("api-srv.")]:
        server.mcp_catalog._tools.pop(name, None)
    server.mcp_catalog._servers.pop("api-srv", None)


def test_api_registrar_descobrir_liberar_e_executar_exige_codigo_humano_em_cada_passo(api_limpa):
    code = lambda: server.HUMAN_CODE.current()     # noqa: E731
    reg = {"id": "api-srv", "name": "Servidor API", "transport": "stdio", "command": "python", "args": [SCRIPT],
           "env": {"MEU_SEGREDO": "valor-secreto"}, "timeout_seconds": 8.0}
    with TestClient(server.app, base_url=TEST_BASE_URL, headers=AUTH_HEADERS) as c:
        server.mcp_client.enabled = True
        # 1. registrar (executa código): sem código = 403; script fora das raízes = 400
        assert c.post("/api/mcp/servers", json=reg).status_code == 403
        r = c.post("/api/mcp/servers", json={**reg, "args": ["-c", "print(1)"], "human_code": code()})
        assert r.status_code == 400
        r = c.post("/api/mcp/servers", json={**reg, "human_code": code()})
        assert r.status_code == 200 and r.json()["env"] == {"MEU_SEGREDO": "***"}      # env nunca volta em claro
        assert "valor-secreto" not in c.get("/api/mcp/servers").text
        # 2. descobrir (lança o processo): exige código; as ferramentas nascem desligadas
        assert c.post("/api/mcp/servers/api-srv/discover", json={}).status_code == 403
        r = c.post("/api/mcp/servers/api-srv/discover", json={"human_code": code()})
        assert r.status_code == 200 and all(t["enabled"] is False for t in r.json())
        assert c.post("/api/mcp/servers/nao-existe/discover", json={"human_code": code()}).status_code == 400
        # 3. executar antes de liberar: bloqueado
        r = c.post("/api/mcp/execute", json={"tool_name": "api-srv.echo", "arguments": {"text": "oi"},
                                             "human_code": code()})
        assert r.status_code == 400
        # 4. liberar (decisão humana): exige código
        pol = {"name": "api-srv.echo", "enabled": True}
        assert c.post("/api/mcp/tools/policy", json=pol).status_code == 403
        assert c.post("/api/mcp/tools/policy", json={**pol, "human_code": code()}).status_code == 200
        assert c.post("/api/mcp/tools/policy", json={**pol, "requires_hitl": False, "human_code": code()}).status_code == 400
        # 5. executar: sem código = 403 (HITL); com código = roda no servidor MCP de verdade
        r = c.post("/api/mcp/execute", json={"tool_name": "api-srv.echo", "arguments": {"text": "oi"},
                                             "confirmed_hitl": True})
        assert r.status_code == 403 and r.json()["detail"]["error"] == "human_code_required"
        r = c.post("/api/mcp/execute", json={"tool_name": "api-srv.echo", "arguments": {"text": "olá API"},
                                             "human_code": code()})
        assert r.status_code == 200 and r.json()["success"] is True and r.json()["output"] == "olá API"
    # saiu do `with`: o lifespan encerrou os servidores MCP
    assert server.mcp_client._sessions == {}
