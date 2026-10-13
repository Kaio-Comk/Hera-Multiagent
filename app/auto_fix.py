import asyncio
from typing import Optional, Callable
from models.task import AgentTask, AgentResult, TaskStatus
from app.coordinator import Coordinator
from app.router import TaskRouter
from app.validator import TaskValidator, ValidationResult
from adapters import progress

class AutoFixEngine:
    """Motor de execução com validação objetiva e loop de auto-correção."""

    def __init__(
        self,
        router: TaskRouter,
        coordinator: Coordinator,
        validator: Optional[TaskValidator] = None,
        on_feedback: Optional[Callable[[str], None]] = None,
        console=None,
    ):
        self.console = console            # ConsoleBus opcional: a tarefa "fala" em tempo real
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

            # 2. Execução via adaptador (com o console ao vivo ligado para esta tentativa)
            token = None
            started = asyncio.get_event_loop().time()
            if self.console is not None:
                ctx = {"title": task.title, "engine": adapter.name, "model": model_name}
                label = f"tentativa {task.retry_count + 1}" if task.retry_count else ""
                self.console.emit("step_start", (reason or "") + (f" ({label})" if label else ""), run=task.id, step=task.id, **ctx)
                token = progress.bind(self.console.sink_for(task.id, task.id, **ctx))
            try:
                handle = await adapter.submit(task)
                result = await adapter.collect(handle)
            finally:
                if token is not None:
                    progress.unbind(token)
            if self.console is not None:
                self.console.emit("step_end", result.summary or "", run=task.id, step=task.id, title=task.title,
                                  engine=adapter.name, model=model_name, status=result.status.value,
                                  seconds=round(asyncio.get_event_loop().time() - started, 1), error=result.error_message or "",
                                  **{k: v for k, v in (result.metadata or {}).items() if v is not None})

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
