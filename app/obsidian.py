import os
import re
import datetime
from typing import List, Optional
from models.task import AgentTask, AgentResult

class ObsidianVaultWriter:
    """Módulo de persistência durável no Obsidian Vault.
    
    Salva notas de histórico, lições aprendidas e evidências de execução
    na pasta '08 Histórico' do projeto no segundo cérebro.
    """

    def __init__(self, vault_project_dir: str = r"C:\Users\<usuario>\Documents\Obsidian Vault\Orquestrador Multiagente"):
        self.vault_project_dir = vault_project_dir
        self.history_dir = os.path.join(vault_project_dir, "08 Histórico")
        os.makedirs(self.history_dir, exist_ok=True)

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
{result.output_text.strip() if result.output_text else result.summary}
```

## ✅ Validação e Critérios
{validations_md.strip()}

## 📁 Artefatos
{artifacts_md}

## 💡 Observações e Auditoria
{lessons_md}
"""

        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)

        return filepath
