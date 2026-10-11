import os
import shutil
import time
import asyncio
from typing import Dict, Any, Optional, List

from models.task import AgentTask, AgentResult, TaskStatus
from models.agent import HealthStatus, AgentCapabilities, RunHandle
from adapters.base import AgentAdapter

class AntigravityAdapter:
    """Adaptador para Antigravity CLI (agy) no Windows.
    
    Executa tarefas em modo headless/não-interativo usando as flags oficiais (-p / --print).
    Especializado em desenvolvimento assistido, refatoração e testes automatizados.
    """

    def __init__(self, binary_path: Optional[str] = None, timeout: float = 300.0):
        self.binary_path = binary_path or self._discover_binary()
        self.timeout = timeout
        self._active_processes: Dict[str, asyncio.subprocess.Process] = {}
        self._active_tasks: Dict[str, asyncio.Task] = {}
        self._results: Dict[str, AgentResult] = {}

    @property
    def name(self) -> str:
        return "antigravity"

    def _discover_binary(self) -> str:
        found = shutil.which("agy")
        if found:
            return found
        default_agy = os.path.expandvars(r"%LOCALAPPDATA%\agy\bin\agy.EXE")
        if os.path.exists(default_agy):
            return default_agy
        fallback_agy = r"C:\Users\Kaio Lima\AppData\Local\agy\bin\agy.EXE"
        if os.path.exists(fallback_agy):
            return fallback_agy
        return "agy"

    async def health_check(self) -> HealthStatus:
        """Verifica se o binário do Antigravity está acessível e obtém sua versão."""
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary_path,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            if proc.returncode == 0:
                ver_text = stdout.decode("utf-8", errors="replace").strip()
                return HealthStatus(
                    healthy=True,
                    version=ver_text,
                    available_models=["gemini-2.5-pro", "gemini-2.5-flash"]
                )
            else:
                return HealthStatus(
                    healthy=False,
                    error_message=f"Antigravity retornou código {proc.returncode}: {stderr.decode('utf-8', errors='replace').strip()}"
                )
        except Exception as e:
            return HealthStatus(
                healthy=False,
                error_message=f"Falha ao executar Antigravity ({self.binary_path}): {str(e)}"
            )

    async def capabilities(self) -> AgentCapabilities:
        """Retorna as capacidades do Antigravity CLI."""
        return AgentCapabilities(
            supports_code_edit=True,
            supports_terminal=True,
            supports_streaming=True,
            supports_json_schema=True,
            supports_vision=True,
            is_local_offline=False
        )

    def _build_prompt(self, task: AgentTask) -> str:
        prompt = f"""[TAREFA: {task.title}]
Instrução:
{task.instruction}
"""
        if task.acceptance_criteria:
            prompt += "\nCritérios de Aceite:\n" + "\n".join(f"- {c}" for c in task.acceptance_criteria)
        return prompt

    async def _execute_cli(self, task: AgentTask, handle: RunHandle) -> AgentResult:
        start_time = handle.start_time
        prompt = self._build_prompt(task)
        timeout = task.timeout_seconds or self.timeout

        cmd = [self.binary_path, "-p", prompt, "--output-format", "text"]

        # Validação estrita de workspace
        if task.workspace and not os.path.isdir(task.workspace):
            return AgentResult(
                task_id=task.id,
                status=TaskStatus.FAILED,
                summary="Falha de validação de workspace",
                error_message=f"Workspace especificado '{task.workspace}' não existe ou é inválido. Execução abortada por segurança.",
                execution_time_seconds=0.0
            )

        cwd = task.workspace if task.workspace else None

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            self._active_processes[task.id] = proc
            handle.process_id = proc.pid

            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            elapsed = time.time() - start_time

            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode == 0:
                return AgentResult(
                    task_id=task.id,
                    status=TaskStatus.SUCCEEDED,
                    summary=f"Antigravity concluiu a tarefa com sucesso em {elapsed:.2f}s",
                    output_text=stdout_str,
                    execution_time_seconds=round(elapsed, 2)
                )
            else:
                return AgentResult(
                    task_id=task.id,
                    status=TaskStatus.FAILED,
                    summary=f"Antigravity finalizou com código {proc.returncode}",
                    output_text=stdout_str,
                    error_message=stderr_str,
                    execution_time_seconds=round(elapsed, 2)
                )

        except asyncio.TimeoutError:
            elapsed = time.time() - start_time
            if task.id in self._active_processes:
                p = self._active_processes[task.id]
                try:
                    if os.name == 'nt' and p.pid:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                    else:
                        p.kill()
                except Exception:
                    pass
            return AgentResult(
                task_id=task.id,
                status=TaskStatus.FAILED,
                summary=f"Timeout atingido ({timeout}s) no Antigravity",
                error_message=f"Execução excedeu limite de {timeout} segundos",
                execution_time_seconds=round(elapsed, 2)
            )
        except asyncio.CancelledError:
            elapsed = time.time() - start_time
            if task.id in self._active_processes:
                try:
                    self._active_processes[task.id].kill()
                except Exception:
                    pass
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
                summary="Exceção na execução do Antigravity",
                error_message=str(e),
                execution_time_seconds=round(elapsed, 2)
            )
        finally:
            self._active_processes.pop(task.id, None)

    async def submit(self, task: AgentTask) -> RunHandle:
        """Inicia a execução do Antigravity em segundo plano."""
        handle = RunHandle(task_id=task.id, start_time=time.time())
        exec_task = asyncio.create_task(self._execute_cli(task, handle))
        self._active_tasks[task.id] = exec_task
        return handle

    async def collect(self, handle: RunHandle) -> AgentResult:
        """Aguarda a conclusão do Antigravity e retorna o AgentResult."""
        exec_task = self._active_tasks.get(handle.task_id)
        if not exec_task:
            if handle.task_id in self._results:
                return self._results[handle.task_id]
            return AgentResult(
                task_id=handle.task_id,
                status=TaskStatus.FAILED,
                summary="Tarefa não encontrada no pool de execução",
                error_message="Handle inválido"
            )

        try:
            result = await exec_task
            self._results[handle.task_id] = result
            return result
        finally:
            self._active_tasks.pop(handle.task_id, None)

    async def cancel(self, handle: RunHandle) -> bool:
        """Encerra o processo do Antigravity e a tarefa assíncrona."""
        proc = self._active_processes.get(handle.task_id)
        if proc:
            try:
                proc.kill()
            except Exception:
                pass
        exec_task = self._active_tasks.get(handle.task_id)
        if exec_task and not exec_task.done():
            exec_task.cancel()
            return True
        return False
