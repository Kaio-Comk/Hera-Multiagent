import pytest
from models.task import AgentTask, TaskStatus
from app.state_store import StateStore

@pytest.fixture
def temp_store(tmp_path):
    db_path = str(tmp_path / "test_store.db")
    return StateStore(db_path=db_path)

def test_create_and_get_task(temp_store):
    task = AgentTask(
        id="task-001",
        title="Verificar banco de dados",
        instruction="Criar tabelas no SQLite",
        task_type="coding"
    )
    created = temp_store.create_task(task)
    assert created.id == "task-001"

    fetched = temp_store.get_task("task-001")
    assert fetched is not None
    assert fetched.title == "Verificar banco de dados"
    assert fetched.status == TaskStatus.PENDING

def test_status_transitions(temp_store):
    task = AgentTask(
        id="task-002",
        title="Executar refatoracao",
        instruction="Refatorar modulo router",
        task_type="coding"
    )
    temp_store.create_task(task)

    # QUEUED -> RUNNING
    updated = temp_store.update_task_status(
        task_id="task-002",
        status=TaskStatus.RUNNING,
        assigned_agent="ollama",
        assigned_model="qwen2.5-coder:1.5b",
        routing_reason="Modelo local na GPU"
    )
    assert updated is True

    running_task = temp_store.get_task("task-002")
    assert running_task.status == TaskStatus.RUNNING
    assert running_task.assigned_agent == "ollama"
    assert running_task.assigned_model == "qwen2.5-coder:1.5b"

    # RUNNING -> SUCCEEDED
    temp_store.update_task_status("task-002", TaskStatus.SUCCEEDED)
    done_task = temp_store.get_task("task-002")
    assert done_task.status == TaskStatus.SUCCEEDED
    assert done_task.completed_at is not None

def test_list_tasks_by_status(temp_store):
    t1 = AgentTask(id="t-1", title="T1", instruction="i1", status=TaskStatus.PENDING)
    t2 = AgentTask(id="t-2", title="T2", instruction="i2", status=TaskStatus.RUNNING)
    temp_store.create_task(t1)
    temp_store.create_task(t2)

    pending_list = temp_store.list_tasks(status=TaskStatus.PENDING)
    assert len(pending_list) == 1
    assert pending_list[0].id == "t-1"

    all_tasks = temp_store.list_tasks()
    assert len(all_tasks) == 2

def test_audit_events(temp_store):
    task = AgentTask(id="t-audit", title="Audit", instruction="Test", task_type="test")
    temp_store.create_task(task)
    temp_store.update_task_status("t-audit", TaskStatus.RUNNING)

    events = temp_store.get_events("t-audit")
    assert len(events) >= 2
    assert events[0]["event_type"] == "TASK_CREATED"
    assert events[1]["event_type"] == "STATUS_CHANGED"
