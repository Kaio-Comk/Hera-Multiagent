import os
import sys
import shlex
import shutil
import asyncio
import subprocess
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
from pydantic import BaseModel, Field

from models.task import AgentTask, AgentResult, TaskStatus
from app.policy_engine import PolicyEngine

class ValidationResult(BaseModel):
    """Resultado da validação objetiva de uma tarefa."""
    passed: bool
    commands_executed: Dict[str, Dict[str, Any]] = Field(default_factory=dict)
    artifacts_verified: Dict[str, bool] = Field(default_factory=dict)
    criteria_status: Dict[str, str] = Field(default_factory=dict)  # "VERIFIED", "FAILED", "UNVERIFIED"
    failure_summary: Optional[str] = None
    suggested_fix_prompt: Optional[str] = None

class TaskValidator:
    """Validador determinístico de critérios de aceite e comandos de teste sem shell."""

    SAFE_ENV_VARS = {
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
        "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA",
        "PYTHONPATH", "LANG", "LC_ALL",
        # Nome do usuario e pastas do perfil: sem USERNAME o pytest cai em `pytest-of-unknown` (pasta compartilhada que
        # outro processo pode ter criado com permissao diferente => PermissionError em todo `tmp_path`). Nao sao segredos.
        "USERNAME", "USERDOMAIN", "HOMEDRIVE", "HOMEPATH", "PROGRAMDATA", "PROGRAMFILES", "SYSTEMDRIVE",
    }

    def __init__(self, command_timeout: float = 300.0, policy_engine: Optional[PolicyEngine] = None):
        self.command_timeout = command_timeout
        self.policy_engine = policy_engine or PolicyEngine()

    def _build_clean_env(self, working_dir: str) -> Dict[str, str]:
        """Cria um ambiente mínimo de execução sem vazar chaves de API ou segredos do usuário."""
        clean = {}
        for k, v in os.environ.items():
            if k.upper() in self.SAFE_ENV_VARS:
                clean[k] = v

        current_pp = clean.get("PYTHONPATH", "")
        clean["PYTHONPATH"] = f"{working_dir}{os.pathsep}{current_pp}" if current_pp else working_dir
        return clean

    @staticmethod
    def _project_python(working_dir: str) -> Optional[str]:
        """Python do ambiente virtual DO PROJETO (`.venv`), se existir: o projeto validado tem as proprias dependencias
        (aiogram, telethon...) que o Python do Hera nao tem."""
        for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
            candidate = os.path.join(working_dir, *rel.split("/"))
            if os.path.isfile(candidate):
                return candidate
        return None

    async def _terminate_process_tree(self, proc: asyncio.subprocess.Process):
        """Encerra com segurança a árvore completa de processos no Windows."""
        try:
            if os.name == 'nt' and proc.pid:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5
                )
            else:
                proc.kill()
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except Exception:
            pass

    async def _run_command(self, cmd: str, cwd: Optional[str] = None) -> Tuple[int, str, str]:
        """Executa um comando via create_subprocess_exec (sem shell) e ambiente estrito."""
        # 1. Validação prévia de política
        is_allowed, reason = self.policy_engine.evaluate_command(cmd, workspace=cwd or os.getcwd())
        if not is_allowed:
            return (-403, "", f"Violação de política de segurança: {reason}")

        working_dir = cwd or os.getcwd()
        if not os.path.isdir(working_dir):
            return (-400, "", f"Diretório de trabalho (cwd) inválido ou inexistente: {working_dir}")

        # 2. Parsing estruturado em argv
        try:
            argv = shlex.split(cmd, posix=False if os.name == 'nt' else True)
        except Exception as e:
            return (-400, "", f"Erro ao decompor comando em argumentos: {e}")

        if not argv:
            return (-400, "", "Comando vazio")

        # Se for o binário python: o .venv do projeto (se houver) ou o interpretador ativo do Hera
        if os.path.basename(argv[0]).lower() in ("python", "python.exe"):
            argv[0] = self._project_python(working_dir) or sys.executable

        env = self._build_clean_env(working_dir)
        # Pasta temporaria PROPRIA deste comando (criada por este processo, apagada no fim): nao herda diretorios de
        # outros usuarios/processos (ex.: `pytest-of-<user>` com permissao diferente) nem deixa lixo.
        tmp_root = tempfile.mkdtemp(prefix="hera-val-")
        env["TEMP"] = env["TMP"] = tmp_root

        # 3. Execução estruturada com shell=False
        try:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv,
                    cwd=working_dir,
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
            except Exception as e:
                return (-500, "", f"Falha ao iniciar processo de validação: {e}")

            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.command_timeout)
                return (
                    proc.returncode if proc.returncode is not None else -1,
                    stdout.decode("utf-8", errors="replace").strip(),
                    stderr.decode("utf-8", errors="replace").strip()
                )
            except asyncio.TimeoutError:
                await self._terminate_process_tree(proc)
                return (-1, "", f"Comando excedeu o timeout de segurança de {self.command_timeout}s")
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    async def validate(self, task: AgentTask, result: AgentResult, workspace: Optional[str] = None) -> ValidationResult:
        """Executa todas as validações declaradas para a tarefa."""
        # Validação explícita de workspace
        if task.workspace and not os.path.exists(task.workspace):
            return ValidationResult(
                passed=False,
                failure_summary=f"Workspace especificado na tarefa não existe: {task.workspace}"
            )

        cwd = workspace or (task.workspace if task.workspace and os.path.exists(task.workspace) else None)

        commands_report = {}
        artifacts_report = {}
        criteria_status = {}
        failure_messages = []

        # 1. Rejeição imediata de tarefas que já falharam ou foram canceladas
        if result.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            return ValidationResult(
                passed=False,
                failure_summary=f"Execução não aprovada (Status: {result.status.value}): {result.error_message or result.summary}",
                suggested_fix_prompt=(
                    f"A execução prévia encerrou com status {result.status.value}:\n"
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
            # Garantir contenção de caminho para artefatos relativos
            if cwd:
                try:
                    resolved_art = Path(path_to_check).resolve()
                    resolved_cwd = Path(cwd).resolve()
                    if not resolved_art.is_relative_to(resolved_cwd):
                        failure_messages.append(f"Artefato '{art}' viola contenção e aponta para fora do workspace.")
                        continue
                except Exception:
                    pass

            exists_and_not_empty = os.path.exists(path_to_check) and os.path.getsize(path_to_check) > 0
            artifacts_report[art] = exists_and_not_empty
            if not exists_and_not_empty:
                failure_messages.append(f"Artefato esperado '{art}' não foi gerado ou está vazio.")

        # 4. Status de critérios de aceite
        for crit in task.acceptance_criteria:
            if len(failure_messages) > 0:
                criteria_status[crit] = "FAILED"
            elif len(task.validation_commands) > 0 or len(result.artifacts) > 0:
                criteria_status[crit] = "VERIFIED"
            else:
                criteria_status[crit] = "UNVERIFIED"

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
            criteria_status=criteria_status,
            failure_summary=failure_summary,
            suggested_fix_prompt=suggested_prompt
        )
