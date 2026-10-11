import os
import tempfile
import pytest
from models.task import AgentTask, AgentResult, TaskStatus
from app.state_store import StateStore
from app.coordinator import Coordinator

@pytest.fixture
def temp_coordinator(tmp_path):
    db_path = str(tmp_path / "test_coordinator.db")
    store = StateStore(db_path=db_path)
    notifications = []

    def capture_feedback(msg: str):
        notifications.append(msg)

    coord = Coordinator(store=store, on_progress=capture_feedback)
    coord.notifications = notifications
    return coord

def test_coordinator_workflow_with_feedback(temp_coordinator):
    coord = temp_coordinator

    # 1. Submeter tarefa
    task = AgentTask(
        id="coord-task-1",
        title="Criar endpoint de status",
        instruction="Adicionar rota /health",
        acceptance_criteria=["Retornar 200 OK"]
    )
    coord.submit_task(task)
    assert len(coord.notifications) == 1
    assert "FILA" in coord.notifications[0]
    assert "coord-task-1" in coord.notifications[0]

    # 2. Iniciar execução
    coord.start_execution("coord-task-1", agent_name="ollama", model_name="qwen2.5-coder:1.5b", reason="Código rápido em GPU")
    assert len(coord.notifications) == 2
    assert "EXECUTANDO" in coord.notifications[1]
    assert "qwen2.5-coder:1.5b" in coord.notifications[1]

    # 3. Validação
    coord.start_validation("coord-task-1", criteria_count=1)
    assert len(coord.notifications) == 3
    assert "VALIDAÇÃO" in coord.notifications[2]

    # 4. Conclusão com sucesso
    result = AgentResult(
        task_id="coord-task-1",
        status=TaskStatus.SUCCEEDED,
        summary="Endpoint /health criado e testado",
        artifacts=["app/health.py"],
        execution_time_seconds=1.8
    )
    coord.complete_task("coord-task-1", result)
    assert len(coord.notifications) == 4
    assert "SUCESSO" in coord.notifications[3]
    assert "app/health.py" in coord.notifications[3]

    # Conferir persistência no SQLite
    saved_task = coord.store.get_task("coord-task-1")
    assert saved_task.status == TaskStatus.SUCCEEDED
    assert saved_task.assigned_agent == "ollama"
