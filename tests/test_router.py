import pytest
from models.agent import HealthStatus
from models.task import AgentTask
from adapters.ollama import OllamaAdapter
from adapters.hera_router import HeraRouterAdapter
from adapters.claude_code import ClaudeCodeAdapter
from adapters.antigravity import AntigravityAdapter
from app.router import TaskRouter

@pytest.fixture
def mock_router():
    ollama = OllamaAdapter()

    async def _no_ar():                 # o roteador agora confere o Ollama; o teste nao pode depender da maquina
        return HealthStatus(healthy=True)
    ollama.health_check = _no_ar
    adapters = {
        "ollama": ollama,
        "hera_router": HeraRouterAdapter(call=None, models=lambda: [], has_cloud=lambda: True),
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
    assert adapter.name == "hera_router"

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
