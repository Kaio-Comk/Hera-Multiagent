import pytest
import time
from app.process_manager import ProcessManager, ProcessState


def test_process_manager_lifecycle():
    pm = ProcessManager()
    rec = pm.register("task-123", "run-abc", 999999, "claude_code")
    assert rec.task_id == "task-123"
    assert rec.state == ProcessState.RUNNING

    pm.heartbeat("task-123")
    assert pm.get("task-123").heartbeat_at >= rec.started_at

    active = pm.list_active()
    assert len(active) == 1
    assert active[0]["task_id"] == "task-123"

    # Terminar processo mockado
    res = pm.terminate("task-123", reason="user_cancelled")
    # No Windows, taskkill para PID 999999 retorna sem crash
    assert pm.get("task-123").state in (ProcessState.TERMINATED, ProcessState.FAILED)
    assert pm.get("task-123").termination_reason == "user_cancelled"
