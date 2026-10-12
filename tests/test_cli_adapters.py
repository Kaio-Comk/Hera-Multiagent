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
