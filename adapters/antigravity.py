import os
from adapters.model_args import clean_model, model_args
from adapters.cli_stream import AntigravityStream, run_streaming
from adapters.progress import STATUS, say
from app.locations import find_agy_binary
import shutil
import time
import asyncio
from typing import Dict, Any, Optional, List

from models.task import AgentTask, AgentResult, TaskStatus
from models.agent import HealthStatus, AgentCapabilities, RunHandle
from adapters.base import AgentAdapter

FALLBACK_MODELS = ["gemini-3.8-flash-high", "gemini-3.8-flash-medium", "gemini-3.8-flash-low"]
MODELS_TTL = 300.0


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
        # $HERA_AGY_PATH > PATH > %LOCALAPPDATA%\agy\bin (sem caminho de usuario fixo no codigo)
        return find_agy_binary()

    async def _discover_models(self):
        """Lista REAL de modelos (`agy models`), em cache por 5 min. Devolve (lista, veio_do_cli)."""
        import time as _time
        cached = getattr(self, "_models_cache", None)
        if cached and _time.monotonic() - cached[0] < MODELS_TTL:
            return cached[1], cached[2]
        models, real = list(FALLBACK_MODELS), False
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary_path, "models", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            if proc.returncode == 0:
                found = []
                for line in stdout.decode("utf-8", errors="replace").splitlines():
                    ident = line.split("\t", 1)[0].strip()
                    if ident and clean_model(ident) and " " not in ident:
                        found.append(ident)
                if found:
                    models, real = list(dict.fromkeys(found)), True
        except Exception:                                   # noqa: BLE001 - sem lista, cai nos modelos conhecidos
            pass
        self._models_cache = (_time.monotonic(), models, real)
        return models, real

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
                models, _ = await self._discover_models()
                return HealthStatus(
                    healthy=True,
                    version=ver_text,
                    available_models=models
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

        models, real = await self._discover_models()
        # o modelo escolhido agora vale; com a lista real do CLI, so passa o que ele conhece (senao usa o padrao dele)
        cmd = [self.binary_path, *model_args(task.assigned_model, models if real else None), "-p", prompt, "--output-format", "stream-json"]

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
            parser = AntigravityStream()
            say(STATUS, f"Executando no Antigravity (modelo {task.assigned_model or 'padrao'})...")

            def remember(proc):
                self._active_processes[task.id] = proc
                handle.process_id = proc.pid

            code, raw_out, stderr_str = await run_streaming(cmd, cwd, timeout, parser, on_proc=remember)
            elapsed = time.time() - start_time
            output = parser.text() if (parser.final is not None or parser.ok is not None) else raw_out.strip()

            if code == 0 and parser.ok is not False:
                return AgentResult(
                    task_id=task.id,
                    status=TaskStatus.SUCCEEDED,
                    summary=f"Antigravity concluiu a tarefa com sucesso em {elapsed:.2f}s",
                    output_text=output,
                    execution_time_seconds=round(elapsed, 2),
                    metadata=dict(parser.meta)
                )
            return AgentResult(
                task_id=task.id,
                status=TaskStatus.FAILED,
                summary=f"Antigravity finalizou com código {code}",
                output_text=output,
                error_message=stderr_str or parser.error or output[:500] or "O Antigravity terminou com erro.",
                execution_time_seconds=round(elapsed, 2),
                metadata=dict(parser.meta)
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
