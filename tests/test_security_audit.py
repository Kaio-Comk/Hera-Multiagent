import os
import pytest
from unittest.mock import AsyncMock, patch

from models.task import AgentTask, AgentResult, TaskStatus
from app.policy_engine import PolicyEngine
from app.validator import TaskValidator
from app.privacy_guard import PrivacyGuard
from app.router import TaskRouter
from app.worktree_manager import WorktreeManager, WorktreeError
from adapters.ollama import OllamaAdapter
from adapters.nine_router import NineRouterAdapter
from adapters.claude_code import ClaudeCodeAdapter
from adapters.antigravity import AntigravityAdapter

# 1. Testes de Shell Injection e Bypass de Executável (P0)
def test_policy_engine_blocks_shell_chaining_and_redirection():
    engine = PolicyEngine()
    chaining_attacks = [
        'pytest tests; powershell -Command "Write-Output bypass"',
        'pytest tests & whoami',
        'pytest tests && dir',
        'pytest tests | echo bypass',
        'pytest tests > output.txt',
        'pytest tests < input.txt',
        'pytest tests `dir`',
        'pytest tests $ENV:SECRET'
    ]
    for attack in chaining_attacks:
        allowed, reason = engine.evaluate_command(attack)
        assert not allowed, f"Ataque '{attack}' deveria ser bloqueado: {reason}"
        assert "metacaracteres" in reason.lower() or "bloqueado" in reason.lower()

def test_policy_engine_blocks_fake_binaries():
    engine = PolicyEngine()
    fake_binaries = [
        "pytestevil --help",
        "pytest_malicious tests",
        "git_fake status",
        "python_evil -m pytest"
    ]
    for cmd in fake_binaries:
        allowed, reason = engine.evaluate_command(cmd)
        assert not allowed, f"Binário não autorizado '{cmd}' deveria ser bloqueado: {reason}"

def test_policy_engine_blocks_arbitrary_python_scripts_and_eval():
    engine = PolicyEngine()
    arbitrary_scripts = [
        "python definitely_not_safe.py",
        "python malicious_script.py --run",
        'python -c "import os; os.system(\'calc\')"',
        "python -m os"
    ]
    for cmd in arbitrary_scripts:
        allowed, reason = engine.evaluate_command(cmd)
        assert not allowed, f"Execução arbitrária '{cmd}' deveria ser bloqueada: {reason}"

def test_policy_engine_allows_safe_module_executions():
    engine = PolicyEngine()
    safe_commands = [
        "pytest tests",
        "python -m pytest tests",
        "python -m py_compile models/task.py",
        "git status",
        "git diff",
        "git log -n 5",
        "ruff check .",
        "flake8 app",
        "mypy models"
    ]
    for cmd in safe_commands:
        allowed, reason = engine.evaluate_command(cmd)
        assert allowed, f"Comando seguro '{cmd}' deveria ser permitido: {reason}"

# 2. Testes de Pre-flight DLP e Privacidade contra envio à Nuvem (P0)
def test_privacy_guard_detects_credentials():
    tokens = [
        "sk-proj-1234567890abcdef1234567890",
        "ghp_1234567890abcdef12345678901234567890",
        "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        "AKIAIOSFODNN7EXAMPLE",
        "password = 'super_secret_password_123'"
    ]
    for tok in tokens:
        has_secret, types = PrivacyGuard.scan_content(f"Conecte com {tok}")
        assert has_secret, f"Segredo não detectado pelo DLP: {tok}"
        assert len(types) > 0

@pytest.mark.asyncio
async def test_router_dlp_forces_local_ollama_on_secrets():
    adapters = {
        "ollama": OllamaAdapter(),
        "9router": NineRouterAdapter()
    }
    router = TaskRouter(adapters=adapters)

    # Tarefa com chave de API tentando ir para 9Router (nuvem)
    task = AgentTask(
        id="t-sec-01",
        title="Tarefa com Segredo",
        instruction="Use a chave sk-proj-1234567890abcdef1234567890 para testar",
        assigned_agent="9router"
    )

    adapter, model, reason = await router.route(task)
    # Gate DLP deve forçar Ollama e barrar 9Router
    assert adapter.name == "ollama"
    assert "Gate DLP ativado" in reason

@pytest.mark.asyncio
async def test_router_offline_precedence_over_manual_assignment():
    adapters = {
        "ollama": OllamaAdapter(),
        "9router": NineRouterAdapter()
    }
    router = TaskRouter(adapters=adapters)

    # Tarefa marcada como offline, mas usuário pediu 9Router
    task = AgentTask(
        id="t-off-02",
        title="Dado Sigiloso",
        instruction="Analise este documento confidencial",
        task_type="offline",
        assigned_agent="9router"
    )

    adapter, model, reason = await router.route(task)
    assert adapter.name == "ollama"
    assert "offline" in reason.lower()

@pytest.mark.asyncio
async def test_nine_router_adapter_blocks_outbound_secrets():
    adapter = NineRouterAdapter()
    task = AgentTask(
        id="t-nr-sec",
        title="Envio Inseguro",
        instruction="Enviar sk-proj-1234567890abcdef1234567890 para a API"
    )

    from models.agent import RunHandle
    handle = RunHandle(task_id=task.id)

    # _execute_chat deve bloquear antes de qualquer post HTTP
    result = await adapter._execute_chat(task, handle)
    assert result.status == TaskStatus.FAILED
    assert "Gate DLP" in result.summary
    assert "openai_key" in result.error_message

# 3. Testes de Worktree Manager e Isolamento de Path (P1)
def test_worktree_manager_rejects_path_traversal():
    wm = WorktreeManager(repo_root=os.getcwd())
    dangerous_task_ids = [
        "../../escape",
        "..\\..\\escape",
        "task/with/slashes",
        "task;rmdir",
        "task$name"
    ]
    for bad_id in dangerous_task_ids:
        with pytest.raises(WorktreeError):
            wm._validate_task_id(bad_id)

# 4. Testes de Validador e Critérios de Aceite (P1)
@pytest.mark.asyncio
async def test_validator_rejects_cancelled_task():
    validator = TaskValidator()
    task = AgentTask(id="t-canc", title="Cancelada", instruction="...")
    result = AgentResult(task_id="t-canc", status=TaskStatus.CANCELLED, summary="Abortada")

    val_res = await validator.validate(task, result)
    assert val_res.passed is False
    assert "CANCELLED" in val_res.failure_summary

@pytest.mark.asyncio
async def test_validator_marks_unverified_criteria_without_tests():
    validator = TaskValidator()
    task = AgentTask(
        id="t-unver",
        title="Sem Testes",
        instruction="Fazer algo",
        acceptance_criteria=["Critério 1", "Critério 2"],
        validation_commands=[]  # Sem testes objetivos
    )
    result = AgentResult(task_id="t-unver", status=TaskStatus.SUCCEEDED, summary="Pronto")

    val_res = await validator.validate(task, result)
    assert val_res.passed is True
    assert val_res.criteria_status["Critério 1"] == "UNVERIFIED"
    assert val_res.criteria_status["Critério 2"] == "UNVERIFIED"

# 5. Testes de Workspace em Adaptadores CLI (P1)
@pytest.mark.asyncio
async def test_claude_code_adapter_rejects_invalid_workspace():
    adapter = ClaudeCodeAdapter()
    task = AgentTask(
        id="t-cc-inv",
        title="Workspace Invalido",
        instruction="...",
        workspace="C:\\Caminho\\Inexistente\\XYZ_123"
    )
    from models.agent import RunHandle
    res = await adapter._execute_cli(task, RunHandle(task_id=task.id))
    assert res.status == TaskStatus.FAILED
    assert "não existe ou é inválido" in res.error_message

@pytest.mark.asyncio
async def test_antigravity_adapter_rejects_invalid_workspace():
    adapter = AntigravityAdapter()
    task = AgentTask(
        id="t-agy-inv",
        title="Workspace Invalido",
        instruction="...",
        workspace="C:\\Caminho\\Inexistente\\XYZ_456"
    )
    from models.agent import RunHandle
    res = await adapter._execute_cli(task, RunHandle(task_id=task.id))
    assert res.status == TaskStatus.FAILED
    assert "não existe ou é inválido" in res.error_message
