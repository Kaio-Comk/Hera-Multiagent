"""Fase 5: endurecimento extra (MCP em lista de permitidos, limite de corpo, IP validado na
conexão contra DNS rebinding e prazo total por requisição)."""
import asyncio
import socket

import httpcore
import httpx
import pytest
from fastapi.testclient import TestClient

import app.server as server
from app.config_manager import DEFAULT_CONFIG
from app.gateway.providers import GatewayProviderError, OllamaGatewayAdapter
from app.gateway.safe_transport import (BlockedDestinationError, SafeNetworkBackend, make_safe_async_client)
from app.gateway.schemas import ChatCompletionRequest, ChatMessage
from app.gateway.tier_router import TierRouter
from app.mcp.catalog import McpCatalog, McpSecurityError
from app.mcp.schemas import McpServerConfig
from helpers import AUTH_HEADERS, TEST_BASE_URL

client = TestClient(server.app, base_url=TEST_BASE_URL, headers=AUTH_HEADERS)
anon = TestClient(server.app, base_url=TEST_BASE_URL)


# ── MCP: allowlist ─────────────────────────────────────────────────────────────
def _register(catalog, command, n=[0]):
    n[0] += 1
    return catalog.register_server(McpServerConfig(id=f"s{n[0]}", name="x", transport="stdio", command=command))


@pytest.mark.parametrize("cmd", ["python", "python.exe", "PYTHON3", "py"])
def test_mcp_executavel_permitido_por_nome(cmd):
    assert _register(McpCatalog(), cmd)


@pytest.mark.parametrize("cmd", [
    "node", "uvx", "ruby", "perl", "curl", "wsl", "pwsh", "mshta", "rundll32",     # fora da lista (antes passavam)
    "C:\\evil\\python.exe", "/usr/bin/python", ".\\python", "..\\python",          # caminho disfarçado de python
    "python -c print(1)", "python;calc", "python|calc", "python&calc", "-python", "'python'",
])
def test_mcp_fora_da_lista_ou_com_caminho_ou_metacaractere_e_recusado(cmd):
    with pytest.raises(McpSecurityError):
        _register(McpCatalog(), cmd)


def test_mcp_lista_vem_do_config_e_a_lista_negra_continua_dando_a_mensagem_certa():
    assert DEFAULT_CONFIG["mcp"]["allowed_commands"] == ["python", "python3", "py"]
    assert _register(McpCatalog(allowed_commands=["node"]), "node")
    with pytest.raises(McpSecurityError, match="Comando proibido"):
        _register(McpCatalog(allowed_commands=["npx"]), "npx")                   # proibido vence a lista


# ── limite de corpo ────────────────────────────────────────────────────────────
JSON_H = {"Content-Type": "application/json"}


def test_corpo_grande_demais_e_413_antes_de_virar_json():
    big = b"x" * (1024 * 1024 + 10)
    r = client.post("/api/tasks/run", content=big, headers=JSON_H)
    assert r.status_code == 413 and "limite" in r.json()["detail"]


def test_limite_do_v1_e_maior_que_o_das_outras_rotas():
    two_mb = b"x" * (2 * 1024 * 1024)
    assert client.post("/api/tasks/run", content=two_mb, headers=JSON_H).status_code == 413
    assert client.post("/v1/chat/completions", content=two_mb, headers=JSON_H).status_code != 413   # 422: JSON inválido
    assert client.post("/v1/chat/completions", content=b"x" * (9 * 1024 * 1024), headers=JSON_H).status_code == 413


def test_corpo_chunked_sem_content_length_tambem_e_limitado():
    def gen():
        for _ in range(12):
            yield b"x" * 100_000                                                  # 1,2 MB sem Content-Length
    r = client.post("/api/tasks/run", content=gen(), headers=JSON_H)
    assert r.status_code == 413


def test_corpo_chunked_pequeno_chega_inteiro_ao_endpoint():
    import json

    def gen():
        body = json.dumps({"instruction": "x", "validation_commands": ["git branch -D x"]}).encode()
        yield body[:10]
        yield body[10:]
    r = client.post("/api/tasks/run", content=gen(), headers=JSON_H)
    assert r.status_code == 400 and r.json()["detail"]["error"] == "validation_command_blocked"   # corpo remontado


def test_sem_token_o_corpo_nem_e_lido():
    r = anon.post("/api/tasks/run", content=b"x" * (2 * 1024 * 1024), headers=JSON_H)
    assert r.status_code == 401


# ── transporte seguro: IP validado na CONEXÃO ──────────────────────────────────
class FakeInner(httpcore.AsyncNetworkBackend):
    def __init__(self):
        self.calls = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.calls.append((host, port))
        return "STREAM"

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        return "UNIX"

    async def sleep(self, seconds):
        pass


def _resolve_to(monkeypatch, *ips):
    def fake(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in ips]
    monkeypatch.setattr(socket, "getaddrinfo", fake)


async def _connect(host, port):
    inner = FakeInner()
    return await SafeNetworkBackend(inner).connect_tcp(host, port), inner


@pytest.mark.asyncio
@pytest.mark.parametrize("ip", ["169.254.169.254", "10.0.0.5", "192.168.1.1", "172.16.0.9", "127.0.0.1", "0.0.0.0"])
async def test_nome_externo_que_resolve_para_interno_e_bloqueado(monkeypatch, ip):     # DNS rebinding
    _resolve_to(monkeypatch, ip)
    with pytest.raises(BlockedDestinationError):
        await _connect("api.openai.com", 443)


@pytest.mark.asyncio
async def test_todos_os_ips_sao_checados_nao_so_o_primeiro(monkeypatch):
    _resolve_to(monkeypatch, "8.8.8.8", "10.0.0.5")
    with pytest.raises(BlockedDestinationError):
        await _connect("api.openai.com", 443)


@pytest.mark.asyncio
async def test_publico_conecta_no_ip_validado_e_nao_no_nome(monkeypatch):
    _resolve_to(monkeypatch, "8.8.8.8")
    stream, inner = await _connect("api.openai.com", 443)
    assert stream == "STREAM" and inner.calls == [("8.8.8.8", 443)]                  # pinning: conectou no IP


@pytest.mark.asyncio
async def test_loopback_so_para_host_local_em_porta_local_autorizada(monkeypatch):
    _resolve_to(monkeypatch, "127.0.0.1")
    _, inner = await _connect("localhost", 11434)                                    # Ollama
    assert inner.calls == [("127.0.0.1", 11434)]
    _, inner = await _connect("127.0.0.1", 20128)                                    # 9Router
    for host, port in (("localhost", 22), ("localhost", 8000), ("127.0.0.1", 6379)):  # outras portas locais
        with pytest.raises(BlockedDestinationError):
            await _connect(host, port)
    with pytest.raises(BlockedDestinationError):                                      # nome externo -> 127.0.0.1:11434
        await _connect("evil.example.com", 11434)


@pytest.mark.asyncio
async def test_localhost_apontando_para_a_rede_local_e_bloqueado(monkeypatch):
    _resolve_to(monkeypatch, "192.168.0.10")                                          # hosts file / DNS adulterado
    with pytest.raises(BlockedDestinationError):
        await _connect("localhost", 11434)


@pytest.mark.asyncio
async def test_falha_de_dns_e_unix_socket_falham_fechado(monkeypatch):
    def boom(*a, **k):
        raise socket.gaierror("sem dns")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(BlockedDestinationError):
        await _connect("qualquer.coisa", 443)
    with pytest.raises(BlockedDestinationError):
        await SafeNetworkBackend(FakeInner()).connect_unix_socket("/var/run/docker.sock")


@pytest.mark.asyncio
async def test_cliente_seguro_de_verdade_recusa_destinos_internos_sem_rede():
    c = make_safe_async_client(timeout=5.0)
    assert isinstance(c._transport._pool._network_backend, SafeNetworkBackend)
    assert c._trust_env is False
    async with c:
        for url in ("http://169.254.169.254/latest/meta-data", "http://10.1.2.3/", "http://127.0.0.1:22/",
                    "http://[::1]:8000/"):
            with pytest.raises(httpx.ConnectError) as exc:
                await c.get(url)
            assert "bloqueada" in str(exc.value).lower() or "bloqueado" in str(exc.value).lower(), url


@pytest.mark.asyncio
async def test_cliente_seguro_deixa_o_ollama_local_passar_pela_guarda():
    async with make_safe_async_client(timeout=2.0) as c:
        try:
            await c.get("http://127.0.0.1:11434/api/tags")
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:   # sem Ollama: recusa ou (no Windows) timeout
            assert not isinstance(exc.__cause__, BlockedDestinationError)   # ...mas NUNCA barrado pela política
        except httpx.HTTPError:
            pass                                                    # há um Ollama de verdade respondendo: ótimo


# ── prazo total por requisição ─────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_prazo_total_corta_requisicao_lenta_com_504():
    async def lento(request):
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(lento)) as http:
        router = TierRouter(adapters=[OllamaGatewayAdapter()], total_timeout_seconds=0.2)
        req = ChatCompletionRequest(model="m", messages=[ChatMessage(role="user", content="oi")])
        with pytest.raises(GatewayProviderError) as exc:
            await router.route_and_execute(req, http)
    assert exc.value.status_code == 504 and "prazo total" in str(exc.value)


@pytest.mark.asyncio
async def test_requisicao_rapida_passa_dentro_do_prazo():
    async def ok(request):
        return httpx.Response(200, json={"message": {"content": "oi"}, "done": True,
                                         "prompt_eval_count": 1, "eval_count": 1})
    async with httpx.AsyncClient(transport=httpx.MockTransport(ok)) as http:
        router = TierRouter(adapters=[OllamaGatewayAdapter()], total_timeout_seconds=5)
        req = ChatCompletionRequest(model="m", messages=[ChatMessage(role="user", content="oi")])
        resp = await router.route_and_execute(req, http)
    assert resp.choices[0].message.content == "oi"


def test_config_padrao_tem_prazo_total():
    assert DEFAULT_CONFIG["gateway"]["total_timeout_seconds"] == 180.0


# ── regressão: `localhost` resolve para ::1 antes de 127.0.0.1 (Ollama só escuta em IPv4) ──────────────
class FlakyInner(FakeInner):
    """Recusa os IPs listados em `refuse`; registra todas as tentativas."""

    def __init__(self, refuse=()):
        super().__init__()
        self.refuse = set(refuse)

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.calls.append((host, port))
        if host in self.refuse:
            raise httpcore.ConnectError(f"recusado {host}")
        return "STREAM"


@pytest.mark.asyncio
async def test_localhost_tenta_ipv4_primeiro_mesmo_que_o_dns_devolva_ipv6_antes(monkeypatch):
    def fake(host, port, *a, **k):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    inner = FlakyInner()
    assert await SafeNetworkBackend(inner).connect_tcp("localhost", 11434) == "STREAM"
    assert inner.calls == [("127.0.0.1", 11434)]                   # IPv4 primeiro: sem esperar o ::1 recusar


@pytest.mark.asyncio
async def test_se_um_ip_validado_recusa_cai_para_o_proximo_e_se_todos_recusam_levanta(monkeypatch):
    _resolve_to(monkeypatch, "8.8.8.8", "8.8.4.4")
    inner = FlakyInner(refuse={"8.8.8.8"})
    assert await SafeNetworkBackend(inner).connect_tcp("api.openai.com", 443) == "STREAM"
    assert inner.calls == [("8.8.8.8", 443), ("8.8.4.4", 443)]
    inner = FlakyInner(refuse={"8.8.8.8", "8.8.4.4"})
    with pytest.raises(httpcore.ConnectError):
        await SafeNetworkBackend(inner).connect_tcp("api.openai.com", 443)
    assert len(inner.calls) == 2


@pytest.mark.asyncio
async def test_localhost_de_verdade_com_servidor_so_em_ipv4(monkeypatch):
    """O cenário real do bug: servidor escutando só em 127.0.0.1 e o cliente chamando `localhost`."""
    import app.gateway.safe_transport as st

    async def handle(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()
    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    monkeypatch.setattr(st, "ALLOWED_LOCAL_PORTS", {port})
    try:
        async with make_safe_async_client(timeout=5.0) as c:
            r = await c.get(f"http://localhost:{port}/")
        assert r.status_code == 200 and r.text == "ok"
    finally:
        srv.close()
        await srv.wait_closed()
