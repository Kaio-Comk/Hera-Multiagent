import os
import shutil

import pytest
from models.task import AgentTask, TaskStatus
from adapters.base import AgentAdapter
from adapters.claude_code import ClaudeCodeAdapter
from adapters.antigravity import AntigravityAdapter

def _binary_available(adapter) -> bool:
    """Os testes "live" precisam do CLI real instalado (nao existe no runner do CI)."""
    path = getattr(adapter, "binary_path", "") or ""
    return bool(path) and (os.path.exists(path) or shutil.which(path) is not None)

# --- Claude Code Tests ---

def test_claude_code_implements_protocol():
    adapter = ClaudeCodeAdapter()
    assert isinstance(adapter, AgentAdapter)
    assert adapter.name == "claude_code"

@pytest.mark.asyncio
async def test_claude_code_capabilities():
    adapter = ClaudeCodeAdapter()
    caps = await adapter.capabilities()
    assert caps.supports_code_edit is True
    assert caps.supports_terminal is True
    assert caps.is_local_offline is False

@pytest.mark.asyncio
async def test_claude_code_live_health():
    adapter = ClaudeCodeAdapter()
    if not _binary_available(adapter):
        pytest.skip(f"Claude Code CLI nao encontrado: {adapter.binary_path}")
    health = await adapter.health_check()
    assert health.healthy is True
    assert "Claude Code" in health.version

# --- Antigravity Tests ---

def test_antigravity_implements_protocol():
    adapter = AntigravityAdapter()
    assert isinstance(adapter, AgentAdapter)
    assert adapter.name == "antigravity"

@pytest.mark.asyncio
async def test_antigravity_capabilities():
    adapter = AntigravityAdapter()
    caps = await adapter.capabilities()
    assert caps.supports_code_edit is True
    assert caps.supports_terminal is True
    assert caps.supports_vision is True

@pytest.mark.asyncio
async def test_antigravity_live_health():
    adapter = AntigravityAdapter()
    if not _binary_available(adapter):
        pytest.skip(f"Antigravity CLI (agy) nao encontrado: {adapter.binary_path}")
    health = await adapter.health_check()
    assert health.healthy is True
    assert health.version  # nao fixa a versao do CLI (mudava a cada atualizacao)


@pytest.mark.asyncio
async def test_agente_em_silencio_alem_do_limite_e_encerrado_antes_do_prazo_total():
    import sys
    import time
    from adapters.cli_stream import IdleTimeout, run_streaming

    class Mudo:
        def feed(self, line):
            pass

    started = time.monotonic()
    with pytest.raises(IdleTimeout):
        await run_streaming([sys.executable, "-c", "import time; time.sleep(30)"], None, 60, Mudo(), idle_limit=1.0)
    assert time.monotonic() - started < 20          # nao esperou os 30 s do processo nem os 60 s do prazo


@pytest.mark.asyncio
async def test_agente_que_fala_dentro_do_limite_nao_e_cortado():
    import sys
    from adapters.cli_stream import run_streaming

    class Mudo:
        def feed(self, line):
            pass

    code, out, _ = await run_streaming([sys.executable, "-c", "import time\nfor i in range(3):\n print(i, flush=True); time.sleep(0.5)"],
                                       None, 30, Mudo(), idle_limit=3.0)
    assert code == 0 and "2" in out
