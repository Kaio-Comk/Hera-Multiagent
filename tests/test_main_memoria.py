"""Ligacao do snapshot da memoria do Hera no execute_task (opt-in por HERA_MEMORY_SNAPSHOT)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import main
from app.memory.store import MemoryStore
from models.task import TaskStatus


def _orquestrador(capturado: list):
    orq = object.__new__(main.MultiagentOrchestrator)
    orq.memory_manager = SimpleNamespace(extract_context_from_instruction=lambda _i: "")
    orq._print_feedback = lambda _m: None

    async def executar(task, workspace=None):
        capturado.append(task.instruction)
        return SimpleNamespace(status=TaskStatus.FAILED)

    orq.autofix = SimpleNamespace(execute_task_with_retry=executar)
    return orq


async def test_snapshot_entra_na_instrucao_quando_ligado(tmp_path, monkeypatch):
    MemoryStore(tmp_path).save("preferencia-testes", "como testar", "usuario", "sempre escrever o teste antes do codigo")
    monkeypatch.setenv("HERA_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("HERA_MEMORY_SNAPSHOT", "1")
    capturado: list = []
    await _orquestrador(capturado).execute_task("t", "preciso de testes para o modulo", save_to_obsidian=False)
    assert "sempre escrever o teste antes do codigo" in capturado[0]
    assert capturado[0].rstrip().endswith("preciso de testes para o modulo")


async def test_instrucao_intacta_quando_desligado(tmp_path, monkeypatch):
    MemoryStore(tmp_path).save("preferencia-testes", "como testar", "usuario", "sempre escrever o teste antes do codigo")
    monkeypatch.setenv("HERA_MEMORY_DIR", str(tmp_path))
    monkeypatch.setenv("HERA_MEMORY_SNAPSHOT", "0")
    capturado: list = []
    await _orquestrador(capturado).execute_task("t", "preciso de testes para o modulo", save_to_obsidian=False)
    assert capturado == ["preciso de testes para o modulo"]
