import time
import asyncio
from typing import Dict, Any, Optional, List
import httpx

from models.task import AgentTask, AgentResult, TaskStatus
from models.agent import HealthStatus, AgentCapabilities, RunHandle
from adapters.base import AgentAdapter

class NineRouterAdapter:
    """Adaptador para o roteador local 9Router (porta 20128).
    
    Compatível com o formato OpenAI API (/v1/chat/completions e /v1/models).
    Usado para triagem rápida, segunda opinião e acesso a modelos em nuvem/gratuitos.
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:20128/v1",
        default_model: str = "alicode-intl/qwen3.5-plus",
        timeout: float = 60.0
    ):
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.timeout = timeout
        self._active_runs: Dict[str, asyncio.Task] = {}
        self._results: Dict[str, AgentResult] = {}

    @property
    def name(self) -> str:
        return "9router"

    async def health_check(self) -> HealthStatus:
        """Verifica se o servidor 9Router local está ativo e lista os modelos disponíveis."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.base_url}/models")
                if resp.status_code == 200:
                    data = resp.json()
                    models_list = [m["id"] for m in data.get("data", []) if "id" in m]
                    return HealthStatus(
                        healthy=True,
                        version="0.5.99",
                        available_models=models_list
                    )
                else:
                    return HealthStatus(
                        healthy=False,
                        error_message=f"9Router retornou HTTP {resp.status_code}"
                    )
        except Exception as e:
            return HealthStatus(
                healthy=False,
                error_message=f"Falha de conexão com 9Router em {self.base_url}: {str(e)}"
            )

    async def capabilities(self) -> AgentCapabilities:
        """Retorna as capacidades suportadas pelo 9Router."""
        return AgentCapabilities(
            supports_code_edit=False,
            supports_terminal=False,
            supports_streaming=True,
            supports_json_schema=True,
            supports_vision=True,
            is_local_offline=False
        )

    def _build_messages(self, task: AgentTask) -> List[Dict[str, str]]:
        system_content = (
            "Você é um agente técnico e direto integrado ao Orquestrador Multiagente.\n"
            "Responda estritamente à tarefa de forma concisa, sem preâmbulos desnecessários."
        )

        user_content = f"Tarefa: {task.title}\nTipo: {task.task_type}\nInstrução:\n{task.instruction}"
        if task.acceptance_criteria:
            user_content += "\n\nCritérios de Aceite:\n" + "\n".join(f"- {c}" for c in task.acceptance_criteria)

        return [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content}
        ]

    async def _execute_chat(self, task: AgentTask, handle: RunHandle) -> AgentResult:
        model_name = task.assigned_model or self.default_model
        messages = self._build_messages(task)
        start_time = handle.start_time

        payload = {
            "model": model_name,
            "messages": messages,
            "temperature": 0.2
        }

        try:
            timeout_cfg = httpx.Timeout(task.timeout_seconds or self.timeout)
            async with httpx.AsyncClient(timeout=timeout_cfg) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload
                )

                elapsed = time.time() - start_time

                if response.status_code == 200:
                    data = response.json()
                    choices = data.get("choices", [])
                    content = ""
                    if choices:
                        content = choices[0].get("message", {}).get("content", "").strip()

                    usage = data.get("usage", {})
                    tokens = usage.get("total_tokens", 0)

                    summary = f"Concluído via 9Router ({model_name}) em {elapsed:.2f}s"
                    if tokens > 0:
                        summary += f" ({tokens} tokens)"

                    return AgentResult(
                        task_id=task.id,
                        status=TaskStatus.SUCCEEDED,
                        summary=summary,
                        output_text=content,
                        execution_time_seconds=round(elapsed, 2)
                    )
                else:
                    return AgentResult(
                        task_id=task.id,
                        status=TaskStatus.FAILED,
                        summary=f"Erro HTTP {response.status_code} do 9Router",
                        error_message=response.text,
                        execution_time_seconds=round(elapsed, 2)
                    )

        except asyncio.CancelledError:
            elapsed = time.time() - start_time
            return AgentResult(
                task_id=task.id,
                status=TaskStatus.CANCELLED,
                summary="Execução cancelada pelo orquestrador",
                execution_time_seconds=round(elapsed, 2)
            )
        except Exception as e:
            elapsed = time.time() - start_time
            return AgentResult(
                task_id=task.id,
                status=TaskStatus.FAILED,
                summary="Exceção na chamada ao 9Router",
                error_message=str(e),
                execution_time_seconds=round(elapsed, 2)
            )

    async def submit(self, task: AgentTask) -> RunHandle:
        """Inicia a chamada assíncrona ao 9Router."""
        handle = RunHandle(
            task_id=task.id,
            start_time=time.time(),
            metadata={"model": task.assigned_model or self.default_model}
        )

        exec_task = asyncio.create_task(self._execute_chat(task, handle))
        self._active_runs[task.id] = exec_task
        return handle

    async def collect(self, handle: RunHandle) -> AgentResult:
        """Aguarda a resposta e retorna o AgentResult."""
        exec_task = self._active_runs.get(handle.task_id)
        if not exec_task:
            if handle.task_id in self._results:
                return self._results[handle.task_id]
            return AgentResult(
                task_id=handle.task_id,
                status=TaskStatus.FAILED,
                summary="Tarefa não encontrada no pool do 9Router",
                error_message="Handle inválido"
            )

        try:
            result = await exec_task
            self._results[handle.task_id] = result
            return result
        finally:
            self._active_runs.pop(handle.task_id, None)

    async def cancel(self, handle: RunHandle) -> bool:
        """Cancela a execução em andamento."""
        exec_task = self._active_runs.get(handle.task_id)
        if exec_task and not exec_task.done():
            exec_task.cancel()
            return True
        return False
