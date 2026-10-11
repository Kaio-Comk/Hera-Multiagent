import os
import pytest
from app.vault_memory import ObsidianMemoryManager
from app.obsidian import ObsidianVaultWriter

@pytest.fixture
def temp_vault(tmp_path):
    vault_dir = str(tmp_path / "TestVault")
    os.makedirs(os.path.join(vault_dir, "00 Contexto"), exist_ok=True)
    os.makedirs(os.path.join(vault_dir, "01 Arquitetura"), exist_ok=True)
    os.makedirs(os.path.join(vault_dir, "04 Decisões"), exist_ok=True)
    os.makedirs(os.path.join(vault_dir, "08 Histórico"), exist_ok=True)

    # Nota 1
    with open(os.path.join(vault_dir, "00 Contexto", "Visão e Objetivos.md"), "w", encoding="utf-8") as f:
        f.write("# Visão e Objetivos\nO orquestrador automatiza tarefas de programação local.")

    # Nota 2
    with open(os.path.join(vault_dir, "01 Arquitetura", "Roteador Inteligente.md"), "w", encoding="utf-8") as f:
        f.write("# Roteador Inteligente\nO roteador avalia complexidade, latência e custo de GPU.")

    return vault_dir

def test_vault_search_and_index(temp_vault):
    mem = ObsidianMemoryManager(vault_project_dir=temp_vault)

    # Pesquisar por "roteador"
    results = mem.search_context("roteador", max_notes=2)
    assert len(results) >= 1
    assert "Roteador Inteligente" in results[0]["title"]
    assert results[0]["score"] > 0

def test_resolve_wikilink(temp_vault):
    mem = ObsidianMemoryManager(vault_project_dir=temp_vault)

    content = mem.resolve_wikilink("[[Visão e Objetivos]]")
    assert content is not None
    assert "Visão e Objetivos" in content

    # Wikilink inexistente
    missing = mem.resolve_wikilink("[[Nota Fantasma]]")
    assert missing is None

def test_sanitize_secrets(temp_vault):
    mem = ObsidianMemoryManager(vault_project_dir=temp_vault)

    unsafe_text = (
        "Resultado da API com chave sk-1234567890abcdef12345678 e "
        "Bearer ghp_1234567890abcdef1234567890abcdef1234"
    )
    clean = mem.sanitize_content(unsafe_text)

    assert "sk-" not in clean
    assert "ghp_" not in clean
    assert "[REDACTED_SECRET]" in clean

def test_duplicate_detection(temp_vault):
    mem = ObsidianMemoryManager(vault_project_dir=temp_vault)
    hist_dir = os.path.join(temp_vault, "08 Histórico")

    note_content = "Nota de teste idêntica com texto específico"
    file_path = os.path.join(hist_dir, "nota_1.md")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(note_content)

    # Checar se detecta duplicata
    assert mem.is_duplicate_note(hist_dir, note_content) is True
    # Texto diferente não é duplicata
    assert mem.is_duplicate_note(hist_dir, "Outro texto totalmente diferente") is False

def test_record_decision_adr(temp_vault):
    writer = ObsidianVaultWriter(vault_project_dir=temp_vault)

    adr_path = writer.record_decision(
        decision_id="001",
        title="Uso de SQLite WAL",
        context="Precisamos de concorrência segura no Windows",
        decision="Ativar modo WAL no SQLite",
        consequences="Leituras simultâneas sem bloqueio de escrita"
    )

    assert os.path.exists(adr_path)
    with open(adr_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "ADR-001" in content
    assert "Uso de SQLite WAL" in content
    assert "Leituras simultâneas" in content
