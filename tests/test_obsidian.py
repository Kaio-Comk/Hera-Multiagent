import os
import pytest
from models.task import AgentTask, AgentResult, TaskStatus
from app.vault.obsidian import ObsidianVaultWriter

def test_obsidian_writer_creates_note(tmp_path):
    vault_dir = str(tmp_path / "Vault")
    writer = ObsidianVaultWriter(vault_project_dir=vault_dir)

    task = AgentTask(
        id="task-obs-01",
        title="Validar Endpoint Health",
        instruction="Criar endpoint /health e validar com pytest",
        assigned_agent="ollama",
        assigned_model="qwen2.5-coder:1.5b",
        routing_reason="Execução local em GPU"
    )

    result = AgentResult(
        task_id="task-obs-01",
        status=TaskStatus.SUCCEEDED,
        summary="Endpoint testado com sucesso",
        output_text="HTTP 200 OK",
        artifacts=["app/health.py"],
        execution_time_seconds=1.25,
        validation_evidence={"pytest": True, "syntax_check": True}
    )

    note_path = writer.record_execution(task, result)

    assert os.path.exists(note_path)
    with open(note_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "task_id: task-obs-01" in content
    assert "status: succeeded" in content
    assert "Validar Endpoint Health" in content
    assert "qwen2.5-coder:1.5b" in content
    assert "[x] pytest" in content
    assert "app/health.py" in content
