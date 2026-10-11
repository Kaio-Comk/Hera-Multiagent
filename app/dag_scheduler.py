import asyncio
from typing import Dict, List, Set, Optional, Tuple, Callable
from collections import defaultdict, deque

from models.task import AgentTask, AgentResult, TaskStatus
from app.coordinator import Coordinator
from app.router import TaskRouter
from app.validator import TaskValidator

class CycleDetectedError(Exception):
    """Lançado quando há ciclo de dependência circular no grafo de tarefas."""
    pass

class DependencyNotFoundError(Exception):
    """Lançado quando uma dependência referencia uma tarefa inexistente no DAG."""
    pass

class TaskDAG:
    """Estrutura e validação de Grafo Acíclico Direcionado (DAG) de tarefas."""

    def __init__(self):
        self.tasks: Dict[str, AgentTask] = {}
        self.dependencies: Dict[str, Set[str]] = defaultdict(set)
        self.dependents: Dict[str, Set[str]] = defaultdict(set)

    def add_task(self, task: AgentTask) -> None:
        """Adiciona uma tarefa e suas dependências ao grafo."""
        self.tasks[task.id] = task
        for dep_id in task.dependencies:
            self.dependencies[task.id].add(dep_id)
            self.dependents[dep_id].add(task.id)

    def validate(self) -> None:
        """Valida se o grafo é acíclico e se todas as dependências existem."""
        # 1. Checar se todas as dependências existem no grafo
        for task_id, deps in self.dependencies.items():
            for dep in deps:
                if dep not in self.tasks:
                    raise DependencyNotFoundError(
                        f"Tarefa '{task_id}' depende de '{dep}', mas ela não existe no DAG."
                    )

        # 2. Algoritmo de Kahn para detecção de ciclos
        in_degree = {t_id: len(self.dependencies.get(t_id, set())) for t_id in self.tasks}
        queue = deque([t_id for t_id, deg in in_degree.items() if deg == 0])
        visited_count = 0

        while queue:
            curr = queue.popleft()
            visited_count += 1
            for dependent in self.dependents[curr]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if visited_count != len(self.tasks):
            raise CycleDetectedError("Ciclo detectado no grafo de tarefas! O plano não é um DAG válido.")

    def get_ready_tasks(
        self,
        succeeded_ids: Set[str],
        active_or_done_ids: Set[str]
    ) -> List[AgentTask]:
        """Retorna tarefas cujas dependências foram todas concluídas com sucesso."""
        ready = []
        for task_id, task in self.tasks.items():
            if task_id in active_or_done_ids:
                continue
            # Todas as dependências devem estar em succeeded_ids
            deps = self.dependencies[task_id]
            if deps.issubset(succeeded_ids):
                ready.append(task)
        return ready

class DAGScheduler:
    """Agendador e executor paralelo de tarefas com controle de concorrência."""

    def __init__(
        self,
        router: TaskRouter,
        coordinator: Coordinator,
        validator: Optional[TaskValidator] = None,
        max_concurrency: int = 2,
        on_feedback: Optional[Callable[[str], None]] = None
    ):
        self.router = router
        self.coordinator = coordinator
        self.validator = validator or TaskValidator()
        self.max_concurrency = max_concurrency
        self.on_feedback = on_feedback

    def _notify(self, msg: str):
        if self.on_feedback:
            self.on_feedback(msg)

    async def execute_dag(self, dag: TaskDAG) -> Dict[str, AgentResult]:
        """Executa todas as tarefas do DAG respeitando dependências e concorrência máxima."""
        dag.validate()

        total_tasks = len(dag.tasks)
        self._notify(f"🌐 [DAG] Iniciando execução de plano com {total_tasks} tarefas (Concorrência máx: {self.max_concurrency})")

        succeeded_ids: Set[str] = set()
        failed_ids: Set[str] = set()
        active_ids: Set[str] = set()
        results: Dict[str, AgentResult] = {}

        semaphore = asyncio.Semaphore(self.max_concurrency)

        # Submeter todas as tarefas inicialmente ao coordenador / SQLite
        for task in dag.tasks.values():
            self.coordinator.submit_task(task)

        async def run_single_task(task: AgentTask) -> Tuple[str, AgentResult]:
            async with semaphore:
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

                # 2. Execução no adaptador
                handle = await adapter.submit(task)
                result = await adapter.collect(handle)

                # 3. Validação objetiva
                criteria_count = len(task.acceptance_criteria) + len(task.validation_commands)
                self.coordinator.start_validation(task.id, criteria_count=criteria_count)

                val_result = await self.validator.validate(task, result)
                if val_result.passed:
                    result.status = TaskStatus.SUCCEEDED
                    for crit in task.acceptance_criteria:
                        result.validation_evidence[crit] = True
                    for cmd, rep in val_result.commands_executed.items():
                        result.validation_evidence[cmd] = rep["passed"]
                else:
                    result.status = TaskStatus.FAILED
                    result.summary = f"Reprovado na validação: {val_result.failure_summary}"
                    result.error_message = val_result.failure_summary

                # 4. Finalização no SQLite
                self.coordinator.complete_task(task.id, result)
                return task.id, result

        running_futures: Dict[str, asyncio.Task] = {}

        while len(succeeded_ids) + len(failed_ids) < total_tasks:
            # 1. Obter tarefas prontas
            active_or_done = succeeded_ids | failed_ids | active_ids
            ready_tasks = dag.get_ready_tasks(succeeded_ids, active_or_done)

            # Se alguma dependência falhou, cancelar dependentes que nunca poderão rodar
            for task_id, task in dag.tasks.items():
                if task_id not in active_or_done:
                    deps = dag.dependencies[task_id]
                    if deps.intersection(failed_ids):
                        # Pelo menos uma dependência falhou
                        failed_ids.add(task_id)
                        res = AgentResult(
                            task_id=task_id,
                            status=TaskStatus.CANCELLED,
                            summary=f"Cancelada automaticamente: dependência prévia falhou",
                            error_message="Falha em tarefa antecedente no DAG"
                        )
                        self.coordinator.complete_task(task_id, res)
                        results[task_id] = res
                        self._notify(f"🚫 [DAG] Tarefa `{task_id}` cancelada pois dependência antecedente falhou.")

            # 2. Iniciar tarefas prontas
            for task in ready_tasks:
                active_ids.add(task.id)
                self._notify(f"⚡ [PARALELO] Disparando `{task.id}` ({task.title})...")
                fut = asyncio.create_task(run_single_task(task))
                running_futures[task.id] = fut

            if not running_futures:
                # Nenhuma tarefa em execução e não há novas tarefas prontas
                break

            # 3. Aguardar conclusão de pelo menos uma tarefa em andamento
            done, _ = await asyncio.wait(
                running_futures.values(),
                return_when=asyncio.FIRST_COMPLETED
            )

            # 4. Processar tarefas concluídas
            for fut in done:
                task_id, result = await fut
                running_futures.pop(task_id, None)
                active_ids.remove(task_id)
                results[task_id] = result

                if result.status == TaskStatus.SUCCEEDED:
                    succeeded_ids.add(task_id)
                    self._notify(f"✅ [DAG] Tarefa `{task_id}` concluída com sucesso.")
                else:
                    failed_ids.add(task_id)
                    self._notify(f"❌ [DAG] Tarefa `{task_id}` finalizada com erro: {result.summary}")

        self._notify(
            f"🏁 [DAG] Execução finalizada! Sucessos: {len(succeeded_ids)}/{total_tasks}, "
            f"Falhas/Canceladas: {len(failed_ids)}/{total_tasks}"
        )
        return results
