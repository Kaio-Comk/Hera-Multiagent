from app.gateway.provider_policy import prefer_ipv4_loopback
import json
import time
import asyncio
from typing import Awaitable, Callable, Dict, Any, Optional, List
import httpx

from models.task import AgentTask, AgentResult, TaskStatus
from models.agent import HealthStatus, AgentCapabilities, RunHandle
from adapters import images as images_mod
from adapters.base import AgentAdapter
from adapters.progress import STATUS, TEXT, say

class OllamaAdapter:
    """Adaptador para o motor Ollama local (GPU/CPU).
    
    Comunicação 100% offline via API REST local (padrão porta 11434).
    Excelente para geração rápida de código, resumos e tarefas locais com privacidade.
    """

    def __init__(self, base_url: str = "http://localhost:11434", default_model: str = "qwen2.5-coder:1.5b", timeout: float = 120.0):
        self.base_url = prefer_ipv4_loopback(base_url).rstrip("/")
        self.default_model = default_model
        self.timeout = timeout
        self._active_runs: Dict[str, asyncio.Task] = {}
        self._results: Dict[str, AgentResult] = {}
        # Trava opcional: `await fit_check(modelo)` devolve o motivo da recusa (modelo grande demais para a GPU) ou None.
        self.fit_check: Optional[Callable[[str], Awaitable[Optional[str]]]] = None

    @property
    def name(self) -> str:
        return "ollama"

    async def health_check(self) -> HealthStatus:
        """Verifica se o daemon do Ollama está respondendo e lista os modelos instalados."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                # 1. Checar versão
                version = "desconhecida"
                try:
                    ver_resp = await client.get(f"{self.base_url}/api/version")
                    if ver_resp.status_code == 200:
                        version = ver_resp.json().get("version", version)
                except httpx.TransportError:
                    raise          # daemon parado: nao adianta tentar a lista de modelos (cada tentativa leva ~2 s no Windows)
                except Exception:
                    pass

                # 2. Listar modelos locais
                tags_resp = await client.get(f"{self.base_url}/api/tags")
                if tags_resp.status_code == 200:
                    models_data = tags_resp.json().get("models", [])
                    available_models = [m.get("name") for m in models_data if "name" in m]
                    return HealthStatus(
                        healthy=True,
                        version=version,
                        available_models=available_models
                    )
                else:
                    return HealthStatus(
                        healthy=False,
                        error_message=f"Ollama respondeu com HTTP {tags_resp.status_code}"
                    )
        except Exception as e:
            return HealthStatus(
                healthy=False,
                error_message=f"Falha de conexão com Ollama em {self.base_url}: {str(e)}"
            )

    async def capabilities(self) -> AgentCapabilities:
        """Retorna as capacidades do motor Ollama."""
        return AgentCapabilities(
            supports_code_edit=False,
            supports_terminal=False,
            supports_streaming=True,
            supports_json_schema=True,
            supports_vision=False,
            is_local_offline=True
        )

    def _build_prompt(self, task: AgentTask) -> str:
        """Constrói o prompt estruturado com contexto, instrução e critérios de aceite."""
        criteria_text = ""
        if task.acceptance_criteria:
            criteria_text = "\nCritérios de Aceite:\n" + "\n".join(f"- {c}" for c in task.acceptance_criteria)

        prompt = f"""[TAREFA: {task.title}]
Tipo: {task.task_type}
Instrução:
{task.instruction}
{criteria_text}

Por favor, execute e forneça uma resposta direta, técnica e precisa."""
        return prompt

    async def _sees_images(self, model_name: str) -> bool:
        """O modelo aceita imagem? Pergunta ao Ollama (`/api/show` -> capabilities) e, sem resposta, usa o nome do modelo."""
        try:
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
                resp = await client.post(f"{self.base_url}/api/show", json={"model": model_name})
            caps = resp.json().get("capabilities") if resp.status_code == 200 else None
            if isinstance(caps, list):
                return "vision" in caps
        except (httpx.HTTPError, ValueError):
            pass
        return images_mod.looks_like_vision_model(model_name)

    async def _execute_generate(self, task: AgentTask, handle: RunHandle) -> AgentResult:
        """Executa a requisição HTTP POST para o endpoint /api/generate do Ollama."""
        model_name = task.assigned_model or self.default_model
        prompt = self._build_prompt(task)
        start_time = handle.start_time

        if self.fit_check is not None:
            try:
                refusal = await self.fit_check(model_name)
            except Exception:       # noqa: BLE001 - a trava nunca pode impedir a execucao por defeito proprio
                refusal = None
            if refusal:
                return AgentResult(task_id=task.id, status=TaskStatus.FAILED, summary="Modelo do Ollama nao cabe na GPU",
                                   error_message=refusal, execution_time_seconds=0.0)

        images_b64: List[str] = []
        if images_mod.usable_images(task.images):
            if await self._sees_images(model_name):
                images_b64 = images_mod.encode_base64(task.images)
            else:
                prompt += images_mod.not_supported_note(f"ollama/{model_name}", task.images)
        payload = {
            "model": model_name,
            "prompt": prompt,
            "stream": True,
            "options": {
                "temperature": 0.2
            }
        }

        if images_b64:
            payload["images"] = images_b64
        try:
            timeout_cfg = httpx.Timeout(task.timeout_seconds or self.timeout)
            say(STATUS, f"Executando no Ollama ({model_name})...")
            async with httpx.AsyncClient(timeout=timeout_cfg) as client:
                # stream: o texto chega aos poucos e o console ao vivo mostra enquanto o modelo escreve
                async with client.stream("POST", f"{self.base_url}/api/generate", json=payload) as response:
                    if response.status_code != 200:
                        body = (await response.aread()).decode("utf-8", errors="replace")
                        low = body.lower()
                        oom = "unable to allocate" in low or "out of memory" in low
                        return AgentResult(
                            task_id=task.id,
                            status=TaskStatus.FAILED,
                            summary="Modelo do Ollama sem memoria para carregar (GPU/RAM): use um modelo menor" if oom
                            else f"Erro HTTP {response.status_code} do Ollama",
                            error_message=body,
                            execution_time_seconds=round(time.time() - start_time, 2)
                        )
                    parts: List[str] = []
                    pending = ""
                    last_flush = time.monotonic()
                    final: Dict[str, Any] = {}
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        data = json.loads(line)
                        if data.get("error"):
                            raise RuntimeError(str(data["error"]))
                        piece = data.get("response", "")
                        if piece:
                            parts.append(piece)
                            pending += piece
                            if "\n" in pending or len(pending) >= 240 or time.monotonic() - last_flush >= 0.6:
                                say(TEXT, pending)
                                pending, last_flush = "", time.monotonic()
                        if data.get("done"):
                            final = data
                    if pending:
                        say(TEXT, pending)

                elapsed = time.time() - start_time
                response_text = "".join(parts).strip()

                # Estatísticas de telemetria da GPU/CPU
                eval_count = final.get("eval_count", 0)
                eval_duration = final.get("eval_duration", 0)
                tps = 0.0
                if eval_duration > 0 and eval_count > 0:
                    tps = eval_count / (eval_duration / 1e9)

                summary = f"Concluído via Ollama ({model_name}) em {elapsed:.2f}s"
                if tps > 0:
                    summary += f" (~{tps:.1f} tokens/s)"

                return AgentResult(
                    task_id=task.id,
                    status=TaskStatus.SUCCEEDED,
                    summary=summary,
                    output_text=response_text,
                    execution_time_seconds=round(elapsed, 2),
                    metadata={"output_tokens": eval_count or None, "input_tokens": final.get("prompt_eval_count"),
                              "tokens_per_second": round(tps, 1) or None}
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
                summary="Exceção na chamada ao Ollama",
                error_message=str(e),
                execution_time_seconds=round(elapsed, 2)
            )

    async def submit(self, task: AgentTask) -> RunHandle:
        """Inicia o processamento assíncrono da tarefa."""
        handle = RunHandle(
            task_id=task.id,
            start_time=time.time(),
            metadata={"model": task.assigned_model or self.default_model}
        )

        exec_task = asyncio.create_task(self._execute_generate(task, handle))
        self._active_runs[task.id] = exec_task
        return handle

    async def collect(self, handle: RunHandle) -> AgentResult:
        """Aguarda a finalização da tarefa assíncrona e retorna o AgentResult."""
        exec_task = self._active_runs.get(handle.task_id)
        if not exec_task:
            if handle.task_id in self._results:
                return self._results[handle.task_id]
            return AgentResult(
                task_id=handle.task_id,
                status=TaskStatus.FAILED,
                summary="Tarefa não encontrada no pool de execução",
                error_message="Handle inválido ou tarefa não submetida"
            )

        try:
            result = await exec_task
            self._results[handle.task_id] = result
            return result
        finally:
            self._active_runs.pop(handle.task_id, None)

    async def cancel(self, handle: RunHandle) -> bool:
        """Interrompe a requisição assíncrona."""
        exec_task = self._active_runs.get(handle.task_id)
        if exec_task and not exec_task.done():
            exec_task.cancel()
            return True
        return False
