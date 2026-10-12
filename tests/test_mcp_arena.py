import pytest
import asyncio
from fastapi.testclient import TestClient

from app.mcp.schemas import (
    McpServerConfig,
    McpToolDefinition,
    McpToolCallRequest,
)
from app.mcp.catalog import McpCatalog, McpSecurityError, McpPermissionDeniedError
from app.mcp.client import McpClientManager

from app.arena.schemas import (
    ArenaCandidate,
    ArenaRunRequest,
)
from app.arena.runner import ArenaRunner
from app.gateway.privacy import GatewayPrivacyManager
from app.server import app
from helpers import AUTH_HEADERS, TEST_BASE_URL


# ==================== TESTES MCP ====================

def test_mcp_server_security_validations():
    """Valida bloqueio de comandos proibidos (zero-Node) e URLs SSRF."""
    catalog = McpCatalog()

    # 1. Bloqueio de npm/npx
    with pytest.raises(McpSecurityError, match="Comando proibido"):
        catalog.register_server(McpServerConfig(
            id="srv-node",
            name="Node Server",
            transport="stdio",
            command="npx"
        ))

    # 2. Bloqueio de URL SSRF para streamable_http
    with pytest.raises(McpSecurityError, match="anti-SSRF"):
        catalog.register_server(McpServerConfig(
            id="srv-cloud",
            name="Cloud Server",
            transport="streamable_http",
            url="http://192.168.1.50/mcp"
        ))

    # 3. Servidor stdio legítimo
    srv_valid = catalog.register_server(McpServerConfig(
        id="srv-py",
        name="Python Local Server",
        transport="stdio",
        command="python"
    ))
    assert srv_valid.id == "srv-py"


def test_mcp_permissions_and_hitl_enforcement():
    """Garante que ferramentas de escrita/destrutivas exijam autorização HITL."""
    catalog = McpCatalog()
    catalog.register_server(McpServerConfig(id="srv-1", name="Srv", transport="stdio", command="python"))

    # Ferramenta read-only
    catalog.register_tool(McpToolDefinition(
        name="read_file_tool",
        description="Lê arquivo",
        server_id="srv-1",
        effect="read",
        requires_hitl=False
    ))

    # Ferramenta destrutiva
    catalog.register_tool(McpToolDefinition(
        name="delete_database_tool",
        description="Apaga banco",
        server_id="srv-1",
        effect="destructive"
    ))

    # Leitura não exige HITL
    tool_read = catalog.check_permission("read_file_tool", confirmed_hitl=False)
    assert tool_read.name == "read_file_tool"

    # Destrutiva sem confirmação falha com McpPermissionDeniedError
    with pytest.raises(McpPermissionDeniedError, match="exige confirmação humana"):
        catalog.check_permission("delete_database_tool", confirmed_hitl=False)

    # Com confirmação passa
    tool_destr = catalog.check_permission("delete_database_tool", confirmed_hitl=True)
    assert tool_destr.name == "delete_database_tool"


@pytest.mark.asyncio
async def test_mcp_client_dlp_and_schema_validation():
    """Verifica validação de JSON Schema e sanitização DLP na saída da ferramenta."""
    catalog = McpCatalog()
    catalog.register_server(McpServerConfig(id="srv-1", name="Srv", transport="stdio", command="python"))
    catalog.register_tool(McpToolDefinition(
        name="fetch_secret_data",
        description="Retorna dados",
        server_id="srv-1",
        parameters_schema={
            "required": ["item_id"],
            "properties": {
                "item_id": {"type": "integer"}
            }
        }
    ))

    client = McpClientManager(catalog=catalog, enabled=True)

    # Handler que simula retorno acidental de credencial
    def fake_handler(item_id: int):
        return f"item {item_id}: chave sk-1234567890abcdef1234567890abcdef gerada."

    client.register_tool_handler("fetch_secret_data", fake_handler)

    # 1. Erro de schema: argumento do tipo errado
    with pytest.raises(McpSecurityError, match="esperado inteiro"):
        await client.execute_tool(McpToolCallRequest(
            tool_name="fetch_secret_data",
            arguments={"item_id": "texto_invalido"}
        ))

    # 2. Execução com sucesso + Sanitização DLP
    res = await client.execute_tool(McpToolCallRequest(
        tool_name="fetch_secret_data",
        arguments={"item_id": 42}
    ))
    assert res.success is True
    assert "sk-1234567890abcdef" not in res.output
    assert "[REDACTED_API_KEY]" in res.output


@pytest.mark.asyncio
async def test_mcp_feature_flag_disabled():
    """Garante que a desativação da feature flag desabilite a execução do MCP."""
    client = McpClientManager(enabled=False)
    with pytest.raises(McpSecurityError, match="desabilitada por Feature Flag"):
        await client.execute_tool(McpToolCallRequest(
            tool_name="any_tool",
            arguments={}
        ))


# ==================== TESTES ARENA ====================

class FakeAdapter:
    def __init__(self, name: str, delay: float = 0.01):
        self.name = name
        self.delay = delay

    async def generate(self, prompt: str, model: str, system: str = None):
        await asyncio.sleep(self.delay)
        return f"Resposta de {self.name} para o modelo {model}: código gerado com sucesso."


@pytest.mark.asyncio
async def test_arena_runner_local_only_privacy_enforcement():
    """Garante que prompts sensíveis/locais descartem candidatos cloud na Arena."""
    adapters = {
        "ollama": FakeAdapter("OllamaLocal", delay=0.01),
        "9router": FakeAdapter("Cloud9Router", delay=0.01),
    }
    privacy_mgr = GatewayPrivacyManager(default_offline=False)
    runner = ArenaRunner(adapters=adapters, privacy_manager=privacy_mgr, enabled=True)

    req = ArenaRunRequest(
        prompt="Gerar código contendo segredo confidencial sk-1234567890abcdef1234567890abcdef",
        candidates=[
            ArenaCandidate(model_id="qwen2.5-coder:1.5b", adapter_name="ollama", is_local=True),
            ArenaCandidate(model_id="gpt-4o", adapter_name="9router", is_local=False),
        ]
    )

    report = await runner.run_arena(req)
    assert report.privacy_classification == "local_only"

    # Candidato local rodou
    local_res = next(r for r in report.results if r.model_id == "qwen2.5-coder:1.5b")
    assert local_res.status == "completed"

    # Candidato cloud foi descartado pela barreira de privacidade
    cloud_res = next(r for r in report.results if r.model_id == "gpt-4o")
    assert cloud_res.status == "skipped_privacy"
    assert "política de privacidade" in cloud_res.error


@pytest.mark.asyncio
async def test_arena_runner_telemetry_and_winner():
    """Verifica coleta de TTFT, duração, tokens/s e eleição de modelo mais rápido."""
    adapters = {
        "fast_model": FakeAdapter("FastModel", delay=0.01),
        "slow_model": FakeAdapter("SlowModel", delay=0.05),
    }
    privacy_mgr = GatewayPrivacyManager(default_offline=False)
    runner = ArenaRunner(adapters=adapters, privacy_manager=privacy_mgr, enabled=True)

    req = ArenaRunRequest(
        prompt="Escreva uma função que calcula fibonacci.",
        candidates=[
            ArenaCandidate(model_id="fast_cand", adapter_name="fast_model", is_local=True),
            ArenaCandidate(model_id="slow_cand", adapter_name="slow_model", is_local=True),
        ]
    )

    report = await runner.run_arena(req)
    assert report.fastest_model == "fast_cand"
    assert len(report.results) == 2
    for r in report.results:
        assert r.status == "completed"
        assert r.duration_ms > 0
        assert r.tokens_per_second > 0


@pytest.mark.asyncio
async def test_arena_feature_flag_disabled():
    """Garante que a desativação da feature flag impeça a execução da Arena."""
    runner = ArenaRunner(enabled=False)
    with pytest.raises(RuntimeError, match="desabilitada por Feature Flag"):
        await runner.run_arena(ArenaRunRequest(prompt="teste", candidates=[]))


# ==================== TESTES DE ENDPOINTS FASTAPI ====================

def test_mcp_and_arena_fastapi_endpoints():
    """Verifica endpoints REST de MCP e Arena via TestClient."""
    client = TestClient(app, base_url=TEST_BASE_URL, headers=AUTH_HEADERS)

    # 1. MCP Servers & Tools
    resp_servers = client.get("/api/mcp/servers")
    assert resp_servers.status_code == 200
    assert isinstance(resp_servers.json(), list)

    resp_tools = client.get("/api/mcp/tools")
    assert resp_tools.status_code == 200
    assert isinstance(resp_tools.json(), list)

    # 2. Registrar servidor e ferramenta via API
    resp_reg_srv = client.post("/api/mcp/servers", json={
        "id": "srv-api-test",
        "name": "Servidor Teste API",
        "transport": "stdio",
        "command": "python"
    })
    assert resp_reg_srv.status_code == 200

    resp_reg_tool = client.post("/api/mcp/tools", json={
        "name": "tool_echo",
        "description": "Echo de teste",
        "server_id": "srv-api-test",
        "effect": "read"
    })
    assert resp_reg_tool.status_code == 200

    # 3. Listar Runs da Arena
    resp_arena_runs = client.get("/api/arena/runs")
    assert resp_arena_runs.status_code == 200
    assert isinstance(resp_arena_runs.json(), list)
