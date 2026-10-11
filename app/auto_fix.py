import asyncio
from typing import Optional, Callable
from models.task import AgentTask, AgentResult, TaskStatus
from app.coordinator import Coordinator
from app.router import TaskRouter
from app.validator import TaskValidator, ValidationResult

class AutoFixEngine:
    """Motor de execução com validação objetiva e loop de auto-correção."""

    def __init__(
        self,
        router: TaskRouter,
        coordinator: Coordinator,
        validator: Optional[TaskValidator] = None,
        on_feedback: Optional[Callable[[str], None]] = None
    ):
        self.router = router
        self.coordinator = coordinator
        self.validator = validator or TaskValidator()
        self.on_feedback = on_feedback

    def _notify(self, msg: str):
        if self.on_feedback:
            self.on_feedback(msg)

    async def execute_task_with_retry(
        self,
        task: AgentTask,
        workspace: Optional[str] = None
    ) -> AgentResult:
        """Executa a tarefa e, caso reprovada na validação, repassa o erro para o modelo em até max_retries."""
        original_instruction = task.instruction

        # Garantir persistência da tarefa no SQLite
        if not self.coordinator.store.get_task(task.id):
            self.coordinator.submit_task(task)

        while task.retry_count <= task.max_retries:
            # 1. Roteamento inteligente
            adapter, model_name, reason = await self.router.route(task)
            task.assigned_agent = adapter.name
            task.assigned_model = model_name
            task.routing_reason = reason

            self.coordinator.start_execution(
                task_id=task.id,
                agent_name=adapter.name,
                model_name=model_name,
                reason=reason
            )

            # 2. Execução via adaptador
            handle = await adapter.submit(task)
            result = await adapter.collect(handle)

            # 3. Validação objetiva
            criteria_count = len(task.acceptance_criteria) + len(task.validation_commands)
            self.coordinator.start_validation(task.id, criteria_count=criteria_count)

            val_result: ValidationResult = await self.validator.validate(task, result, workspace=workspace)

            # 4. Caso tenha passado na validação
            if val_result.passed:
                result.status = TaskStatus.SUCCEEDED
                # Preencher evidências de testes executados
                for cmd, rep in val_result.commands_executed.items():
                    result.validation_evidence[cmd] = rep["passed"]
                for art, exists in val_result.artifacts_verified.items():
                    result.validation_evidence[f"artifact:{art}"] = exists
                for crit in task.acceptance_criteria:
                    result.validation_evidence[crit] = True

                self.coordinator.complete_task(task.id, result)
                return result

            # 5. Validação falhou -> Checar política de retry
            if task.retry_count < task.max_retries:
                task.retry_count += 1
                self.coordinator.store.update_task_status(
                    task_id=task.id,
                    status=TaskStatus.RUNNING,
                    retry_count=task.retry_count
                )
                self.coordinator.store.record_event(
                    task_id=task.id,
                    event_type="RETRY_TRIGGERED",
                    message=f"Tentativa {task.retry_count}/{task.max_retries}: Falha na validação automatizada",
                    payload={"failure": val_result.failure_summary}
                )

                self._notify(
                    f"🔄 **[AUTO-CORREÇÃO]** Tentativa {task.retry_count}/{task.max_retries} para `{task.id}`. "
                    f"Testes reprovados. Repassando traceback para o modelo..."
                )

                # Incrementar instrução com traceback específico
                task.instruction = f"{original_instruction}\n\n{val_result.suggested_fix_prompt}"
            else:
                # Esgotou tentativas de correção
                result.status = TaskStatus.FAILED
                result.summary = f"Reprovado na validação após {task.max_retries} tentativas de correção"
                result.error_message = val_result.failure_summary

                self._notify(
                    f"❌ **[FALHA]** Tarefa `{task.id}` esgotou o limite de {task.max_retries} retries "
                    f"sem aprovação objetiva."
                )

                self.coordinator.complete_task(task.id, result)
                return result

        return result
