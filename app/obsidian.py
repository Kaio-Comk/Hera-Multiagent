import os
from app.locations import resolve_vault_dir
import re
import datetime
from typing import List, Optional
from models.task import AgentTask, AgentResult

from app.vault_memory import ObsidianMemoryManager

class ObsidianVaultWriter:
    """Módulo de persistência durável no Obsidian Vault.
    
    Salva notas de histórico, lições aprendidas, decisões aprovadas e evidências de execução
    na pasta do projeto no segundo cérebro.
    """

    def __init__(self, vault_project_dir: Optional[str] = None):
        self.vault_project_dir = os.path.abspath(resolve_vault_dir(vault_project_dir))
        self.history_dir = os.path.join(self.vault_project_dir, "08 Histórico")
        self.decisions_dir = os.path.join(self.vault_project_dir, "04 Decisões")
        os.makedirs(self.history_dir, exist_ok=True)
        os.makedirs(self.decisions_dir, exist_ok=True)
        self.memory_manager = ObsidianMemoryManager(vault_project_dir=self.vault_project_dir)

    def _sanitize_filename(self, name: str) -> str:
        # Remover caracteres proibidos no Windows: <>:"/\|?*
        clean = re.sub(r'[<>:"/\\|?*]', '', name)
        return clean.strip()[:80]

    def record_execution(
        self,
        task: AgentTask,
        result: AgentResult,
        validation_items: Optional[List[str]] = None,
        lessons: Optional[str] = None
    ) -> str:
        """Gera e salva nota estruturada no formato padrão do Obsidian."""
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        safe_title = self._sanitize_filename(task.title)
        filename = f"{today_str} — Execução {task.id} — {safe_title}.md"
        filepath = os.path.join(self.history_dir, filename)

        status_str = result.status.value.lower()
        agent_name = task.assigned_agent or "desconhecido"
        model_name = task.assigned_model or "padrão"

        # Formatar validações
        validations_md = ""
        if validation_items:
            for item in validation_items:
                validations_md += f"- [x] {item}\n"
        elif result.validation_evidence:
            for item, passed in result.validation_evidence.items():
                box = "[x]" if passed else "[ ]"
                validations_md += f"- {box} {item}\n"
        else:
            validations_md = f"- [{'x' if status_str == 'succeeded' else ' '}] Execução concluída com código 0\n"

        # Formatar artefatos
        artifacts_md = ""
        if result.artifacts:
            artifacts_md = "\n".join(f"- `{art}`" for art in result.artifacts)
        else:
            artifacts_md = "- *(Nenhum artefato de arquivo gerado)*"

        # Lições ou observações
        lessons_md = lessons or (
            f"Executado por `{agent_name}` ({model_name}) em {result.execution_time_seconds:.2f}s.\n"
            f"Roteamento: {task.routing_reason or 'Direto'}."
        )

        raw_output = result.output_text.strip() if result.output_text else result.summary
        sanitized_output = self.memory_manager.sanitize_content(raw_output)

        content = f"""---
task_id: {task.id}
status: {status_str}
project: orquestrador-multiagente
created: {today_str}
agent: {agent_name}
model: {model_name}
tags: [orquestrador, execucao, historico]
---

# {task.title}

← [[Orquestrador — Home (MOC)]] · [[Plano de Fases e Roadmap]]

## 🎯 Objetivo
{task.instruction}

## 🏆 Resultado
{result.summary}

### Saída:
```text
{sanitized_output}
```

## ✅ Validação e Critérios
{validations_md.strip()}

## 📁 Artefatos
{artifacts_md}

## 💡 Observações e Auditoria
{lessons_md}
"""

        # Verificar se já existe nota duplicada
        if not os.path.exists(filepath) and self.memory_manager.is_duplicate_note(self.history_dir, content):
            return filepath

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return filepath

    def record_decision(
        self,
        decision_id: str,
        title: str,
        context: str,
        decision: str,
        consequences: str
    ) -> str:
        """Registra uma Nota de Decisão Arquitetural (ADR) na pasta 04 Decisões."""
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        safe_title = self._sanitize_filename(title)
        filename = f"ADR-{decision_id.upper()} — {safe_title}.md"
        filepath = os.path.join(self.decisions_dir, filename)

        content = f"""---
adr: {decision_id.upper()}
status: aprovada
data: {today_str}
tags: [orquestrador, decisao, adr]
---

# ADR-{decision_id.upper()}: {title}

← [[Orquestrador — Home (MOC)]] · [[Decisões e Riscos]]

## 1. Contexto e Problema
{context.strip()}

## 2. Decisão Adotada
{decision.strip()}

## 3. Consequências e Impactos
{consequences.strip()}
"""

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return filepath

    def record_hitl_merge(
        self,
        task_id: str,
        proposal_id: str,
        merged_commit: str,
        target_branch: str,
        feature_branch: str,
        diff_hash: str,
        reviewer: str = "operator",
    ) -> str:
        """Registra nota de auditoria de merge aprovado via HITL no histórico."""
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        filename = f"{today_str} — HITL Merge {proposal_id} — {task_id}.md"
        filepath = os.path.join(self.history_dir, filename)

        content = f"""---
task_id: {task_id}
proposal_id: {proposal_id}
merged_commit: {merged_commit}
target_branch: {target_branch}
feature_branch: {feature_branch}
diff_hash: {diff_hash}
reviewer: {reviewer}
tags: [orquestrador, hitl, merge, auditoria]
---

# 🛡️ Auditoria HITL: Merge Aprovado ({proposal_id})

← [[Orquestrador — Home (MOC)]] · [[08 Histórico/Home]]

- **Tarefa:** `{task_id}`
- **Feature Branch:** `{feature_branch}`
- **Target Branch:** `{target_branch}`
- **Commit Resultante:** `{merged_commit}`
- **SHA-256 do Diff:** `{diff_hash}`
- **Revisor Humano:** `{reviewer}`

> [!NOTE] Verificação Criptográfica
> O merge foi autorizado após conferência integral de hash de diff e nonce de uso único.
"""
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return filepath

