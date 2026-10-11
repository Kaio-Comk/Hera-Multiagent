import pytest
from models.task import AgentTask
from adapters.ollama import OllamaAdapter
from adapters.nine_router import NineRouterAdapter
from adapters.claude_code import ClaudeCodeAdapter
from adapters.antigravity import AntigravityAdapter
from app.router import TaskRouter

@pytest.fixture
def mock_router():
    adapters = {
        "ollama": OllamaAdapter(),
        "9router": NineRouterAdapter(),
        "claude_code": ClaudeCodeAdapter(),
        "antigravity": AntigravityAdapter(),
    }
    return TaskRouter(adapters=adapters)

@pytest.mark.asyncio
async def test_route_manual_override(mock_router):
    task = AgentTask(
        id="t-man",
        title="Manual",
        instruction="run ollama",
        assigned_agent="ollama",
        assigned_model="qwen3.5:4b"
    )
    adapter, model, reason = await mock_router.route(task)
    assert adapter.name == "ollama"
    assert model == "qwen3.5:4b"
    assert "manual" in reason.lower()

@pytest.mark.asyncio
async def test_route_coding_task(mock_router):
    task = AgentTask(
        id="t-code",
        title="Refactor router",
        instruction="refatorar codigo",
        task_type="coding"
    )
    adapter, model, reason = await mock_router.route(task)
    # Prefer Antigravity or Claude Code for coding
    assert adapter.name in ("antigravity", "claude_code")
    assert "Codificação" in reason

@pytest.mark.asyncio
async def test_route_research_task(mock_router):
    task = AgentTask(
        id="t-res",
        title="Pesquisar libs",
        instruction="pesquisa na web ou nuvem",
        task_type="research"
    )
    adapter, model, reason = await mock_router.route(task)
    assert adapter.name == "9router"

@pytest.mark.asyncio
async def test_route_offline_task(mock_router):
    task = AgentTask(
        id="t-off",
        title="Dado sigiloso",
        instruction="processar offline",
        task_type="offline"
    )
    adapter, model, reason = await mock_router.route(task)
    assert adapter.name == "ollama"
    assert "GPU local" in reason
