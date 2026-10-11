import time
from typing import List, Optional, Callable
from models.task import AgentTask, AgentResult, TaskStatus
from app.state_store import StateStore

class Coordinator:
    """Coordenador central com emissão de feedback em tempo real para o chat."""

    def __init__(self, store: Optional[StateStore] = None, on_progress: Optional[Callable[[str], None]] = None):
        self.store = store or StateStore()
        # Callback para enviar feedback ao chat
        self.on_progress = on_progress or self._default_chat_output

    def _default_chat_output(self, message: str):
        print(f"\n{message}")

    def notify(self, emoji: str, stage: str, message: str):
        """Emite notificação estruturada para o chat do usuário."""
        formatted = f"{emoji} **[{stage}]** {message}"
        self.on_progress(formatted)

    def submit_task(self, task: AgentTask) -> AgentTask:
        """Adiciona uma nova tarefa à fila e notifica o chat."""
        task.status = TaskStatus.QUEUED
        created = self.store.create_task(task)
        self.notify("📋", "FILA", f"Tarefa `{task.id}` ({task.title}) adicionada à fila.")
        return created

    def start_execution(self, task_id: str, agent_name: str, model_name: Optional[str] = None, reason: Optional[str] = None):
        """Inicia a execução da tarefa e avisa o chat qual agente/modelo está trabalhando."""
        model_info = f" ({model_name})" if model_name else ""
        self.store.update_task_status(
            task_id=task_id,
            status=TaskStatus.RUNNING,
            assigned_agent=agent_name,
            assigned_model=model_name,
            routing_reason=reason
        )
        self.notify("⚙️", "EXECUTANDO", f"Agente `{agent_name}`{model_info} assumiu a tarefa `{task_id}`. Motivo: {reason or 'Roteamento padrão'}.")

    def start_validation(self, task_id: str, criteria_count: int):
        """Inicia a etapa de validação objetiva."""
        self.store.update_task_status(task_id, TaskStatus.VALIDATING)
        self.notify("🧪", "VALIDAÇÃO", f"Executando {criteria_count} verificações e testes objetivos para `{task_id}`...")

    def complete_task(self, task_id: str, result: AgentResult):
        """Conclui a tarefa e registra o sucesso ou falha com detalhes no chat."""
        if result.status == TaskStatus.SUCCEEDED:
            self.store.update_task_status(task_id, TaskStatus.SUCCEEDED)
            art_info = f" Artefatos: {', '.join(result.artifacts)}" if result.artifacts else ""
            self.notify("✅", "SUCESSO", f"Tarefa `{task_id}` aprovada em {result.execution_time_seconds:.1f}s!{art_info}\n> {result.summary}")
        else:
            self.store.update_task_status(task_id, TaskStatus.FAILED)
            err = result.error_message or "Falha em critérios de validação."
            self.notify("❌", "FALHA", f"Tarefa `{task_id}` falhou: {err}")

    def cancel_task(self, task_id: str, reason: str = "Cancelado pelo usuário"):
        """Cancela uma tarefa da fila."""
        self.store.update_task_status(task_id, TaskStatus.CANCELLED)
        self.notify("⏹️", "CANCELADO", f"Tarefa `{task_id}` interrompida: {reason}")
