import os
import pytest
import sqlite3
import time
from unittest.mock import AsyncMock, patch, MagicMock

from models.task import AgentTask, AgentResult, TaskStatus
from app.policy_engine import PolicyEngine
from app.validator import TaskValidator
from app.state_store import StateStore
from app.benchmark import LocalBenchmark, BenchmarkResult

def test_policy_engine_blocks_dangerous_commands():
    engine = PolicyEngine()
    dangerous_commands = [
        "rm -rf /",
        "rmdir /s mydir",
        "del /f /s /q C:\\data",
        "format C:",
        "git reset --hard HEAD~1",
        "git push origin main --force",
        "shutdown /s /t 0"
    ]
    for cmd in dangerous_commands:
        allowed, reason = engine.evaluate_command(cmd)
        assert not allowed, f"Comando '{cmd}' deveria ter sido bloqueado, mas foi permitido: {reason}"
        assert "bloqueado" in reason.lower()

def test_policy_engine_blocks_protected_paths_and_traversal():
    engine = PolicyEngine()
    forbidden_paths = [
        "type C:\\Windows\\System32\\drivers\\etc\\hosts",
        "cat ../../Windows/win.ini",
        "dir %SystemRoot%",
        "ls C:\\Program Files"
    ]
    for cmd in forbidden_paths:
        allowed, reason = engine.evaluate_command(cmd)
        assert not allowed, f"Comando com path protegido '{cmd}' deveria ter sido bloqueado: {reason}"

def test_policy_engine_allows_whitelisted_commands():
    engine = PolicyEngine()
    safe_commands = [
        "pytest tests/test_state_store.py",
        "python -m pytest",
        "python -m py_compile app/state_store.py",
        "git status",
        "git diff",
        "git log -n 5",
        "ruff check .",
        "flake8 app"
    ]
    for cmd in safe_commands:
        allowed, reason = engine.evaluate_command(cmd)
        assert allowed, f"Comando seguro '{cmd}' deveria ter sido permitido: {reason}"

def test_policy_engine_loads_yaml_policies(tmp_path):
    custom_yaml = tmp_path / "custom_policy.yaml"
    custom_yaml.write_text("""
allowed_commands:
  - "echo safe"
forbidden_patterns:
  - "dangerous_tool"
""")
    engine = PolicyEngine(policy_path=str(custom_yaml))
    allowed1, _ = engine.evaluate_command("echo safe")
    assert allowed1
    allowed2, _ = engine.evaluate_command("dangerous_tool --exec")
    assert not allowed2

@pytest.mark.asyncio
async def test_task_validator_policy_interception():
    validator = TaskValidator()
    task = AgentTask(
        id="t-sec-test",
        title="Tentativa Maliciosa",
        instruction="Remover arquivos",
        task_type="coding",
        validation_commands=["rm -rf /", "format d:"]
    )
    result = AgentResult(
        task_id="t-sec-test",
        status=TaskStatus.SUCCEEDED,
        summary="Tentativa de bypass"
    )

    validation = await validator.validate(task, result)
    assert not validation.passed
    assert "rm -rf /" in validation.commands_executed
    assert validation.commands_executed["rm -rf /"]["exit_code"] == -403
    assert "Violação de política" in validation.commands_executed["rm -rf /"]["stderr"]

def test_state_store_crash_recovery(tmp_path):
    db_file = tmp_path / "test_recovery.db"
    store = StateStore(db_path=str(db_file))

    # Criar tarefas em múltiplos estados
    t1 = store.create_task(AgentTask(id="t-run", title="Executando", instruction="...", task_type="coding"))
    store.update_task_status("t-run", TaskStatus.RUNNING)

    t2 = store.create_task(AgentTask(id="t-val", title="Validando", instruction="...", task_type="coding"))
    store.update_task_status("t-val", TaskStatus.VALIDATING)

    t3 = store.create_task(AgentTask(id="t-ok", title="Sucesso", instruction="...", task_type="coding"))
    store.update_task_status("t-ok", TaskStatus.SUCCEEDED)

    # Executar recuperação
    recovered_ids = store.recover_interrupted_tasks()
    assert set(recovered_ids) == {"t-run", "t-val"}

    # Checar estados atualizados
    assert store.get_task("t-run").status == TaskStatus.FAILED
    assert store.get_task("t-val").status == TaskStatus.FAILED
    assert store.get_task("t-ok").status == TaskStatus.SUCCEEDED

    # Checar evento de auditoria
    events = store.get_events("t-run")
    assert any(e["event_type"] == "CRASH_RECOVERY" for e in events)

@pytest.mark.asyncio
async def test_benchmark_calculation_and_execution():
    bm = LocalBenchmark()
    
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "eval_count": 100,
        "eval_duration": 1_000_000_000,  # 1 segundo em ns
        "prompt_eval_count": 25,
        "prompt_eval_duration": 200_000_000,
        "total_duration": 1_200_000_000,
        "response": "def quicksort(arr): return arr"
    }
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = mock_resp
        res = await bm.run_benchmark(model="test-model", prompt="teste", max_tokens=50)
        
        assert res.model == "test-model"
        assert res.tokens_generated == 100
        assert res.tokens_per_second == 100.0
        assert res.total_duration_sec == 1.2
        assert "quicksort" in res.sample_response
