import os
import asyncio
from typing import Dict, List, Optional, Tuple, Any
from pydantic import BaseModel, Field

from models.task import AgentTask, AgentResult, TaskStatus
from app.policy_engine import PolicyEngine

class ValidationResult(BaseModel):
    """Resultado da validação objetiva de uma tarefa."""
    passed: bool
    commands_executed: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    artifacts_verified: Dict[str, bool] = Field(default_factory=dict)
    failure_summary: Optional[str] = None
    suggested_fix_prompt: Optional[str] = None

class TaskValidator:
    """Validador determinístico de critérios de aceite e comandos de teste."""

    def __init__(self, command_timeout: float = 60.0, policy_engine: Optional[PolicyEngine] = None):
        self.command_timeout = command_timeout
        self.policy_engine = policy_engine or PolicyEngine()

    async def _run_command(self, cmd: str, cwd: Optional[str] = None) -> Tuple[int, str, str]:
        """Executa um comando de teste/validação em subprocesso com ambiente PYTHONPATH configurado."""
        is_allowed, reason = self.policy_engine.evaluate_command(cmd)
        if not is_allowed:
            return (-403, "", f"Violação de política de segurança: {reason}")

        working_dir = cwd or os.getcwd()
        env = os.environ.copy()
        current_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{working_dir}{os.pathsep}{current_pp}" if current_pp else working_dir

        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=working_dir,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.command_timeout)
            return (
                proc.returncode,
                stdout.decode("utf-8", errors="replace").strip(),
                stderr.decode("utf-8", errors="replace").strip()
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return (-1, "", f"Comando excedeu o timeout de {self.command_timeout}s")

    async def validate(self, task: AgentTask, result: AgentResult, workspace: Optional[str] = None) -> ValidationResult:
        """Executa todas as validações declaradas para a tarefa."""
        cwd = workspace or (task.workspace if task.workspace and os.path.exists(task.workspace) else None)

        commands_report = {}
        artifacts_report = {}
        failure_messages = []

        # 1. Se o resultado do agente já veio como FAILED
        if result.status == TaskStatus.FAILED:
            return ValidationResult(
                passed=False,
                failure_summary=f"Execução do agente falhou: {result.error_message or result.summary}",
                suggested_fix_prompt=(
                    f"A execução prévia falhou com o seguinte erro:\n"
                    f"{result.error_message or result.summary}\n\n"
                    f"Por favor, revise o código e corrija a causa do erro."
                )
            )

        # 2. Executar comandos de validação automatizados
        for cmd in task.validation_commands:
            code, stdout, stderr = await self._run_command(cmd, cwd=cwd)
            passed = (code == 0)
            commands_report[cmd] = {
                "exit_code": code,
                "passed": passed,
                "stdout": stdout,
                "stderr": stderr
            }

            if not passed:
                err_detail = stderr if stderr else stdout
                failure_messages.append(f"Comando '{cmd}' falhou (exit {code}):\n{err_detail}")

        # 3. Validar arquivos e artefatos gerados
        for art in result.artifacts:
            path_to_check = art if os.path.isabs(art) else (os.path.join(cwd, art) if cwd else art)
            exists_and_not_empty = os.path.exists(path_to_check) and os.path.getsize(path_to_check) > 0
            artifacts_report[art] = exists_and_not_empty
            if not exists_and_not_empty:
                failure_messages.append(f"Artefato esperado '{art}' não foi gerado ou está vazio.")

        all_passed = (len(failure_messages) == 0)

        failure_summary = None
        suggested_prompt = None

        if not all_passed:
            failure_summary = "\n---\n".join(failure_messages)
            suggested_prompt = (
                f"[FALHA NA VALIDAÇÃO DA TAREFA: {task.title}]\n"
                f"A tentativa de execução gerou as seguintes falhas nos testes/critérios de aceite:\n\n"
                f"{failure_summary}\n\n"
                f"INSTRUÇÃO DE CORREÇÃO:\n"
                f"Analise o traceback ou erro acima e produza uma solução corrigida que satisfaça todos os critérios."
            )

        return ValidationResult(
            passed=all_passed,
            commands_executed=commands_report,
            artifacts_verified=artifacts_report,
            failure_summary=failure_summary,
            suggested_fix_prompt=suggested_prompt
        )
