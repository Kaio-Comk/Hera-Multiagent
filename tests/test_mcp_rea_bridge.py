"""A ponte do REA (tools/mcp_rea_bridge.py) precisa passar na política de lançamento do MCP sem afrouxá-la."""
import sys
from pathlib import Path

import pytest

from app.mcp.catalog import McpCatalog, McpSecurityError
from app.mcp.launch import McpLaunchError, build_argv, validate_stdio_server
from app.mcp.schemas import McpServerConfig

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import mcp_rea_bridge  # noqa: E402


def _cfg(**kw):
    base = dict(id="rea", name="REA", transport="stdio", command="python", args=[str(ROOT / "tools" / "mcp_rea_bridge.py")])
    base.update(kw)
    return McpServerConfig(**base)


def test_ponte_passa_na_politica_de_lancamento_e_no_catalogo():
    validate_stdio_server(_cfg(), [ROOT])
    McpCatalog().register_server(_cfg())                 # allowlist padrão (só python) continua valendo
    assert build_argv(_cfg())[1].endswith("mcp_rea_bridge.py")


def test_allowlist_continua_proibindo_npx_e_o_rea_direto():
    with pytest.raises(McpSecurityError):
        McpCatalog().register_server(_cfg(command="npx", args=["rea-agents", "mcp"]))
    with pytest.raises(McpSecurityError):
        McpCatalog().register_server(_cfg(command="rea", args=["mcp"]))


def test_ponte_fora_do_projeto_e_recusada(tmp_path):
    fora = tmp_path / "mcp_rea_bridge.py"
    fora.write_text("print(1)")
    with pytest.raises(McpLaunchError):
        validate_stdio_server(_cfg(args=[str(fora)]), [ROOT])


def test_comando_da_ponte_usa_node_local_sem_npx():
    if not mcp_rea_bridge.REA_ENTRY.is_file():
        pytest.skip("REA não instalado em tools/rea")
    cmd = mcp_rea_bridge.build_command()
    assert cmd[1:] == [str(mcp_rea_bridge.REA_ENTRY), "mcp"]
    assert "npx" not in cmd[0].lower()
