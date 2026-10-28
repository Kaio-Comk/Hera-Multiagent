import asyncio
import re
import time
from typing import Dict, List, Set, Optional, Tuple, Callable
from collections import defaultdict, deque

from models.task import AgentTask, AgentResult, TaskStatus
from app.coordinator import Coordinator
from app.router import TaskRouter
from app.validator import TaskValidator
from adapters import progress
from app.quota import is_quota_error, seconds_until_reset

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
        on_feedback: Optional[Callable[[str], None]] = None,
        console=None,
        run_id: Optional[str] = None,
    ):
        self.console = console            # ConsoleBus opcional: o plano "fala" em tempo real
        self.run_id = run_id
        self.router = router
        self.coordinator = coordinator
        self.validator = validator or TaskValidator()
        self.max_concurrency = max_concurrency
        self.on_feedback = on_feedback

    def _notify(self, msg: str):
        if self.on_feedback:
            self.on_feedback(msg)

    BLOCKED_MARK = "BLOQUEADO:"
    MAX_DEP_CHARS = 12000

    @staticmethod
    def _is_optional(task: AgentTask, result: Optional[AgentResult]) -> bool:
        """Passo cujo titulo comeca com OPCIONAL: se ele proprio falhar (nao se foi cancelado), os dependentes seguem."""
        return (result is not None and result.status == TaskStatus.FAILED
                and bool(re.match(r"\W*OPCIONAL\b", task.title or "", re.IGNORECASE)))

    def _is_blocked(self, result: AgentResult) -> bool:
        # aceita "BLOQUEADO:", "BLOQUEADO (parcial):", "**BLOQUEADO**"... no inicio da resposta
        return bool(re.match(r"\W*BLOQUEAD[OA]\b", (result.output_text or "").lstrip(), re.IGNORECASE))

    def _with_context(self, task: AgentTask, results: Dict[str, AgentResult]) -> str:
        """Instrucao do passo + saida (cortada) dos passos de que ele depende + regra de reportar bloqueio."""
        if "[RESULTADOS DOS PASSOS ANTERIORES]" in task.instruction:
            return task.instruction                       # ja montada (nova tentativa)
        text = task.instruction
        blocks = []
        for dep in sorted(task.dependencies):
            res = results.get(dep)
            if res is not None and res.output_text:
                blocks.append(f"--- {dep} ---\n{res.output_text.strip()[:self.MAX_DEP_CHARS]}")
        if blocks:
            text += "\n\n[RESULTADOS DOS PASSOS ANTERIORES]\n" + "\n\n".join(blocks)
        if self.run_id:
            text += (f"\n\n[REGRA] Se NAO conseguir cumprir o OBJETIVO PRINCIPAL deste passo (sem acesso a pasta/arquivo, "
                     f"dado essencial ausente, etc.), comece a resposta com `{self.BLOCKED_MARK}` e explique. Nunca finja que "
                     f"concluiu. Se o objetivo principal foi cumprido e so faltou um detalhe secundario que exige acesso que "
                     f"voce nao tem (ex.: medir algo num servico remoto), NAO use `{self.BLOCKED_MARK}`: entregue o resultado e "
                     f"liste o que faltou numa linha final `PENDENCIAS:`. Nunca consulte nem altere bancos/servicos de producao "
                     f"por conta propria a menos que o passo peca isso explicitamente. "
                     f"[REGRA] NUNCA abra nem leia arquivos de segredos (.env, *.pem, *.jks, *.p12, *.session, credenciais): "
                     f"para saber nomes de variaveis use o .env.example. Edite so o que o passo pede (em documentos, altere as "
                     f"secoes necessarias, sem reescrever o arquivo inteiro nem mudar o idioma). Nao deixe arquivos temporarios "
                     f"na pasta do projeto. Nao faca commit/push a menos que o passo peca.")
        return text

    def _step_id(self, task: AgentTask) -> str:
        prefix = f"{self.run_id}-" if self.run_id else ""
        return task.id[len(prefix):] if prefix and task.id.startswith(prefix) else task.id

    async def _heartbeat(self, step: str, ctx: dict, started: float) -> None:
        """A cada 30 s sem novidade, avisa que o passo continua (motores que so respondem no fim ficam mudos por minutos)."""
        try:
            while True:
                await asyncio.sleep(15)
                if self.console.idle_seconds(self.run_id, step) >= 28:
                    minutes, seconds = divmod(int(time.monotonic() - started), 60)
                    self.console.emit("heartbeat", f"ainda trabalhando ({minutes}min {seconds:02d}s)...",
                                      run=self.run_id, step=step, **ctx)
        except asyncio.CancelledError:
            return

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
                # 0. Passa o resultado dos passos anteriores e (nos planos) o protocolo de bloqueio
                task.instruction = self._with_context(task, results)

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

                # 2. Execução no adaptador (com o console ao vivo ligado para ESTE passo)
                step = self._step_id(task)
                started = time.monotonic()
                token = heartbeat = None
                if self.console is not None and self.run_id:
                    ctx = {"title": task.title, "engine": adapter.name, "model": model_name}
                    self.console.emit("step_start", reason or "", run=self.run_id, step=step, **ctx)
                    token = progress.bind(self.console.sink_for(self.run_id, step, **ctx))
                    heartbeat = asyncio.create_task(self._heartbeat(step, ctx, started))
                try:
                    for _attempt in range(3):
                        handle = await adapter.submit(task)
                        result = await adapter.collect(handle)
                        text = f"{result.error_message or ''} {result.summary or ''}"
                        if result.status == TaskStatus.SUCCEEDED or not is_quota_error(text):
                            break
                        # o motor (assinatura) bateu no limite de uso: marca como esgotado e refaz o passo em outro motor
                        wait = seconds_until_reset(text)
                        self.router.registrar_limite(adapter.name, wait, text)
                        alt = self.router.fallback_adapter(task, adapter.name)
                        minutos = max(1, wait // 60)
                        if alt is None:
                            result.error_message = (f"{adapter.name} atingiu o limite de uso (volta em ~{minutos} min) e nao ha outro motor "
                                                    f"adequado ligado para refazer este passo. Detalhe: {text.strip()[:300]}")
                            break
                        old = adapter.name
                        adapter, model_name = alt
                        task.assigned_agent, task.assigned_model = adapter.name, model_name
                        task.routing_reason = f"{old} sem limite de uso (volta em ~{minutos} min): passo refeito em {adapter.name}"
                        self._notify(f"🔁 [DAG] {old} atingiu o limite de uso; `{task.id}` sera refeita em {adapter.name}.")
                        if self.console is not None and self.run_id:
                            self.console.emit("info", task.routing_reason, run=self.run_id, step=step)
                            ctx = {"title": task.title, "engine": adapter.name, "model": model_name}
                finally:
                    if heartbeat is not None:
                        heartbeat.cancel()
                    if token is not None:
                        progress.unbind(token)

                # 3. Validação objetiva
                criteria_count = len(task.acceptance_criteria) + len(task.validation_commands)
                self.coordinator.start_validation(task.id, criteria_count=criteria_count)
                if self.run_id and not task.validation_commands:
                    quais = (f"os {len(task.acceptance_criteria)} critério(s) de aceite só orientam o agente"
                             if task.acceptance_criteria else "sem critério de aceite")
                    self._notify(f"⚠️ [DAG] `{task.id}` não tem comando de teste ({quais}): "
                                 f"só o protocolo BLOQUEADO confere o resultado; revise a saída.")

                val_result = await self.validator.validate(task, result)
                if val_result.passed and self.run_id and self._is_blocked(result):
                    # o motor terminou sem erro, mas disse que nao conseguiu fazer o passo: nao e sucesso
                    result.status = TaskStatus.FAILED
                    result.error_message = "O passo reportou BLOQUEADO: " + result.output_text.strip()[:400]
                    result.summary = "Passo bloqueado (o motor nao conseguiu executar)"
                    self.coordinator.complete_task(task.id, result)
                    if self.console is not None:
                        self.console.emit("step_end", result.summary, run=self.run_id, step=step, title=task.title,
                                          engine=adapter.name, model=model_name, status=result.status.value,
                                          seconds=round(time.monotonic() - started, 1), error=result.error_message)
                    return task.id, result
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
                if self.console is not None and self.run_id:
                    self.console.emit("step_end", result.summary or "", run=self.run_id, step=step, title=task.title,
                                      engine=adapter.name, model=model_name, status=result.status.value,
                                      seconds=round(time.monotonic() - started, 1), error=result.error_message or "",
                                      **{k: v for k, v in (result.metadata or {}).items() if v is not None})
                return task.id, result

        running_futures: Dict[str, asyncio.Task] = {}

        while len(succeeded_ids) + len(failed_ids) < total_tasks:
            # 1. Obter tarefas prontas
            active_or_done = succeeded_ids | failed_ids | active_ids
            # passo OPCIONAL que falhou nao derruba quem depende dele (o consolidador roda e registra a pendencia)
            optional_failed = {t for t in failed_ids if self._is_optional(dag.tasks[t], results.get(t))}
            ready_tasks = dag.get_ready_tasks(succeeded_ids | optional_failed, active_or_done)

            # Se alguma dependência falhou, cancelar dependentes que nunca poderão rodar
            for task_id, task in dag.tasks.items():
                if task_id not in active_or_done:
                    deps = dag.dependencies[task_id]
                    if deps.intersection(failed_ids - optional_failed):
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
                        if self.console is not None and self.run_id:
                            self.console.emit("step_end", res.summary, run=self.run_id, step=self._step_id(task), title=task.title,
                                              status="CANCELLED", seconds=0, error=res.error_message or "")

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
            try:
                done, _ = await asyncio.wait(
                    running_futures.values(),
                    return_when=asyncio.FIRST_COMPLETED
                )
            except asyncio.CancelledError:
                # o plano foi cancelado (botao "Parar"): os passos em andamento sao tarefas separadas e precisam parar junto
                for fut in running_futures.values():
                    fut.cancel()
                await asyncio.gather(*running_futures.values(), return_exceptions=True)
                raise

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
