import os
import pytest
from models.task import AgentTask, AgentResult, TaskStatus
from app.state_store import StateStore
from app.coordinator import Coordinator
from app.router import TaskRouter
from app.validator import TaskValidator
from app.auto_fix import AutoFixEngine

@pytest.mark.asyncio
async def test_validator_command_success():
    validator = TaskValidator()
    task = AgentTask(
        id="t-val-01",
        title="Teste Validação Sucesso",
        instruction="Executar comando simples",
        validation_commands=['python -c "print(\'teste ok\')"']
    )
    result = AgentResult(
        task_id="t-val-01",
        status=TaskStatus.RUNNING,
        summary="Executado"
    )

    val_res = await validator.validate(task, result)
    assert val_res.passed is True
    assert val_res.failure_summary is None
    assert val_res.commands_executed['python -c "print(\'teste ok\')"']["passed"] is True

@pytest.mark.asyncio
async def test_validator_command_failure():
    validator = TaskValidator()
    task = AgentTask(
        id="t-val-02",
        title="Teste Validação Falha",
        instruction="Executar comando com erro",
        validation_commands=['python -c "import sys; sys.exit(42)"']
    )
    result = AgentResult(
        task_id="t-val-02",
        status=TaskStatus.RUNNING,
        summary="Executado"
    )

    val_res = await validator.validate(task, result)
    assert val_res.passed is False
    assert "exit 42" in val_res.failure_summary
    assert val_res.suggested_fix_prompt is not None

@pytest.mark.asyncio
async def test_validator_artifacts_check(tmp_path):
    validator = TaskValidator()

    valid_file = str(tmp_path / "valid.py")
    with open(valid_file, "w") as f:
        f.write("print('ok')")

    invalid_file = str(tmp_path / "missing.py")

    task = AgentTask(
        id="t-val-art",
        title="Teste Artefatos",
        instruction="Gerar arquivos"
    )
    result = AgentResult(
        task_id="t-val-art",
        status=TaskStatus.RUNNING,
        summary="Arquivos gerados",
        artifacts=[valid_file, invalid_file]
    )

    val_res = await validator.validate(task, result)
    assert val_res.passed is False
    assert val_res.artifacts_verified[valid_file] is True
    assert val_res.artifacts_verified[invalid_file] is False
    assert "missing.py" in val_res.failure_summary

class FlakyMockAdapter:
    """Simula um agente que falha na primeira tentativa e acerta na segunda."""
    def __init__(self, succeeds_on_attempt: int = 1):
        self.succeeds_on_attempt = succeeds_on_attempt
        self.call_count = 0

    @property
    def name(self) -> str:
        return "flaky"

    async def submit(self, task: AgentTask):
        from models.agent import RunHandle
        return RunHandle(task_id=task.id)

    async def collect(self, handle):
        attempt = self.call_count
        self.call_count += 1
        if attempt < self.succeeds_on_attempt:
            return AgentResult(
                task_id=handle.task_id,
                status=TaskStatus.RUNNING,
                summary="Código gerado com bug sintático"
            )
        else:
            return AgentResult(
                task_id=handle.task_id,
                status=TaskStatus.RUNNING,
                summary="Código corrigido e funcional"
            )

@pytest.mark.asyncio
async def test_autofix_success_after_retry(tmp_path):
    db_path = str(tmp_path / "autofix_test.db")
    store = StateStore(db_path=db_path)
    coord = Coordinator(store=store)

    adapter = FlakyMockAdapter(succeeds_on_attempt=1)
    router = TaskRouter(adapters={"flaky": adapter})

    # Validador: falha na tentativa 0, passa na tentativa 1
    class DynamicValidator(TaskValidator):
        async def validate(self, task, result, workspace=None):
            if "bug sintático" in result.summary:
                return await super().validate(
                    task,
                    result,
                    workspace
                )
            from app.validator import ValidationResult
            return ValidationResult(passed=True)

    dyn_validator = DynamicValidator()
    notifications = []
    engine = AutoFixEngine(
        router=router,
        coordinator=coord,
        validator=dyn_validator,
        on_feedback=lambda msg: notifications.append(msg)
    )

    task = AgentTask(
        id="t-autofix-01",
        title="Auto Fix Task",
        instruction="Criar script",
        validation_commands=['python -c "import sys; sys.exit(1)"'],
        max_retries=2,
        assigned_agent="flaky"
    )

    result = await engine.execute_task_with_retry(task)

    assert result.status == TaskStatus.SUCCEEDED
    assert task.retry_count == 1
    assert any("AUTO-CORREÇÃO" in n for n in notifications)

    # Verificar banco SQLite
    saved_task = store.get_task("t-autofix-01")
    assert saved_task.status == TaskStatus.SUCCEEDED
    assert saved_task.retry_count == 1

@pytest.mark.asyncio
async def test_autofix_exhaustion(tmp_path):
    db_path = str(tmp_path / "autofix_exhaust_test.db")
    store = StateStore(db_path=db_path)
    coord = Coordinator(store=store)

    adapter = FlakyMockAdapter(succeeds_on_attempt=99)  # Nunca acerta
    router = TaskRouter(adapters={"flaky": adapter})

    engine = AutoFixEngine(
        router=router,
        coordinator=coord,
        validator=TaskValidator(),
        on_feedback=lambda msg: None
    )

    task = AgentTask(
        id="t-exhaust-01",
        title="Exhaustion Task",
        instruction="Comando que sempre quebra",
        validation_commands=['python -c "import sys; sys.exit(1)"'],
        max_retries=2,
        assigned_agent="flaky"
    )

    result = await engine.execute_task_with_retry(task)

    assert result.status == TaskStatus.FAILED
    assert task.retry_count == 2
    assert "Reprovado na validação após 2 tentativas" in result.summary

    saved_task = store.get_task("t-exhaust-01")
    assert saved_task.status == TaskStatus.FAILED
    assert saved_task.retry_count == 2
