"""Motor `hera_router`: o Roteador embutido do Hera.

Não abre porta nem processo externo: chama o gateway do próprio Hera (provedores de nuvem com rodízio de chaves,
combos com fallback, camadas, DLP e modo offline) diretamente, em memória.

O modelo da tarefa pode ser: `default` (o roteador escolhe), o nome de um COMBO, ou o nome de um modelo de algum
provedor ligado.
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Dict, List, Optional

from adapters.base import AgentAdapter  # noqa: F401 - documenta o protocolo implementado
from app.gateway.providers import GatewayProviderError
from app.gateway.schemas import ChatCompletionRequest, ChatCompletionResponse, ChatMessage
from app.privacy_guard import PrivacyGuard
from models.agent import AgentCapabilities, HealthStatus, RunHandle
from models.task import AgentResult, AgentTask, TaskStatus


class HeraRouterAdapter:
    def __init__(
        self,
        call: Callable[[ChatCompletionRequest], Awaitable[ChatCompletionResponse]],
        models: Callable[[], List[str]],
        has_cloud: Callable[[], bool],
        default_model: str = "default",
    ):
        self._call = call
        self._models = models
        self._has_cloud = has_cloud
        self.default_model = default_model
        self._active_runs: Dict[str, asyncio.Task] = {}

    @property
    def name(self) -> str:
        return "hera_router"

    def has_cloud(self) -> bool:
        """Há ao menos um provedor de nuvem ligado e com chave? (o roteador do Hera só prefere este motor se sim)"""
        return bool(self._has_cloud())

    async def health_check(self) -> HealthStatus:
        models = ["default", *self._models()]
        return HealthStatus(healthy=True, version="embutido", available_models=models)

    async def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(supports_code_edit=False, supports_terminal=False, supports_streaming=True,
                                 supports_json_schema=True, supports_vision=False, is_local_offline=False)

    def _request(self, task: AgentTask) -> ChatCompletionRequest:
        user = f"Tarefa: {task.title}\nTipo: {task.task_type}\nInstrução:\n{task.instruction}"
        if task.acceptance_criteria:
            user += "\n\nCritérios de Aceite:\n" + "\n".join(f"- {c}" for c in task.acceptance_criteria)
        return ChatCompletionRequest(
            model=task.assigned_model or self.default_model,
            messages=[
                ChatMessage(role="system", content="Você é um agente técnico e direto integrado ao Orquestrador "
                                                   "Multiagente. Responda estritamente à tarefa, de forma concisa."),
                ChatMessage(role="user", content=user),
            ],
            temperature=0.2,
        )

    async def _run(self, task: AgentTask, handle: RunHandle) -> AgentResult:
        started = handle.start_time
        try:
            resp = await asyncio.wait_for(self._call(self._request(task)), timeout=task.timeout_seconds or 300)
            choice = resp.choices[0] if resp.choices else None
            content = (choice.message.content if choice and choice.message else "") or ""
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
            elapsed = time.time() - started
            return AgentResult(task_id=task.id, status=TaskStatus.SUCCEEDED,
                               summary=f"Concluído pelo Roteador Hera ({resp.model}) em {elapsed:.2f}s",
                               output_text=str(content).strip(), execution_time_seconds=round(elapsed, 2))
        except asyncio.CancelledError:
            return AgentResult(task_id=task.id, status=TaskStatus.CANCELLED, summary="Execução cancelada",
                               execution_time_seconds=round(time.time() - started, 2))
        except GatewayProviderError as exc:
            return AgentResult(task_id=task.id, status=TaskStatus.FAILED,
                               summary=f"Roteador Hera recusou/falhou (HTTP {exc.status_code})",
                               error_message=PrivacyGuard.sanitize_content(str(exc))[:300],
                               execution_time_seconds=round(time.time() - started, 2))
        except Exception as exc:                # noqa: BLE001 - nunca derruba o orquestrador
            return AgentResult(task_id=task.id, status=TaskStatus.FAILED, summary="Exceção no Roteador Hera",
                               error_message=PrivacyGuard.sanitize_content(f"{type(exc).__name__}: {exc}")[:300],
                               execution_time_seconds=round(time.time() - started, 2))

    async def submit(self, task: AgentTask) -> RunHandle:
        handle = RunHandle(task_id=task.id, start_time=time.time(),
                           metadata={"model": task.assigned_model or self.default_model})
        self._active_runs[task.id] = asyncio.create_task(self._run(task, handle))
        return handle

    async def collect(self, handle: RunHandle) -> AgentResult:
        run: Optional[asyncio.Task] = self._active_runs.get(handle.task_id)
        if run is None:
            return AgentResult(task_id=handle.task_id, status=TaskStatus.FAILED,
                               summary="Tarefa não encontrada no Roteador Hera", error_message="Handle inválido")
        try:
            return await run
        finally:
            self._active_runs.pop(handle.task_id, None)

    async def cancel(self, handle: RunHandle) -> bool:
        run = self._active_runs.get(handle.task_id)
        if run and not run.done():
            run.cancel()
            return True
        return False
