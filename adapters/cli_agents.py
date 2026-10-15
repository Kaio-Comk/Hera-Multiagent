"""Motores por CLI OFICIAL em modo não interativo: Codex (OpenAI), Cursor Agent, Qoder, CodeBuddy e Kimi Code.

Como o Claude Code e o Antigravity, o Hera NÃO fala com o serviço: ele chama o programa oficial que VOCÊ instalou e em que você
fez login (`codex login`, `agent login`, `qodercli login`, `kimi login`, login dentro do `codebuddy`). Quem se identifica
ao serviço é o programa, com o próprio cabeçalho e o próprio token; o Hera nunca vê a senha nem o token.

Windows: os `.cmd`/`.ps1` que o npm cria passariam o prompt por um interpretador de comandos (metacaracteres `& | ^` viram
comandos). Por isso o Hera resolve o ponto de entrada real (`node <script>.js`) e chama sem shell, com argumentos em lista.
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from pathlib import Path
from typing import Dict, List, Optional

from adapters import images as images_mod
from adapters.cli_stream import _kill_tree, run_streaming
from adapters.model_args import clean_model
from adapters.progress import STATUS, TEXT, say
from models.agent import AgentCapabilities, HealthStatus, RunHandle
from models.task import AgentResult, AgentTask, TaskStatus

MAX_ARGV_PROMPT = 24000            # limite de linha de comando do Windows é ~32 mil caracteres
NOT_LOGGED = re.compile(r"not logged in|not authenticated|unauthenticated|please (log ?in|sign ?in)|未登录|nao esta logado", re.I)


def fit_prompt(prompt: str, limit: int = MAX_ARGV_PROMPT) -> str:
    """Prompt que cabe na linha de comando: se passar do limite, corta o MEIO (início e fim ficam) e avisa."""
    if len(prompt) <= limit:
        return prompt
    note = "\n\n[... trecho do meio omitido: instrução maior que o limite da linha de comando deste motor ...]\n\n"
    keep = limit - len(note)
    head = int(keep * 0.6)
    return prompt[:head] + note + prompt[len(prompt) - (keep - head):]


def npm_global_dir() -> Path:
    appdata = os.environ.get("APPDATA")
    return Path(appdata) / "npm" / "node_modules" if appdata else Path.home() / ".npm-global" / "lib" / "node_modules"


class HeadlessCliAdapter:
    """Base: roda `<cli> ... <prompt>` sem shell, lê o texto final da saída padrão."""
    name = "cli"
    label = "CLI"
    env_var = ""                                      # $HERA_<X>_PATH: caminho fixo do executável
    exe_names: List[str] = []                         # nomes procurados no PATH
    npm_entry: Optional[tuple] = None                 # (pacote, arquivo de entrada) quando veio do npm
    status_args: Optional[List[str]] = None           # comando que diz se há login (opcional)
    login_hint = ""
    install_hint = ""
    prompt_by_stdin = False

    def __init__(self, binary_path: Optional[str] = None, timeout: float = 300.0, auto_approve: bool = False):
        self.binary_path = binary_path or ""
        self.timeout = timeout
        self.auto_approve = bool(auto_approve)       # libera escrita/comandos sem perguntar (escolha explícita do usuário)
        self._active_processes: Dict[str, asyncio.subprocess.Process] = {}
        self._active_tasks: Dict[str, asyncio.Task] = {}
        self._results: Dict[str, AgentResult] = {}

    # ── descoberta do programa ────────────────────────────────────────────────
    def launcher(self) -> Optional[List[str]]:
        """Comando-base (lista) para executar o CLI sem shell; None se não estiver instalado."""
        fixed = (self.binary_path or os.environ.get(self.env_var, "")).strip()
        if fixed and os.path.exists(fixed):
            return self._wrap(fixed)
        if self.npm_entry:
            script = npm_global_dir() / self.npm_entry[0] / self.npm_entry[1]
            node = shutil.which("node")
            if node and script.exists():
                return [node, str(script)]
        for exe in self.exe_names:
            found = shutil.which(exe)
            if found:
                return self._wrap(found)
        return self._extra_launcher()

    def _extra_launcher(self) -> Optional[List[str]]:
        return None

    @staticmethod
    def _wrap(path: str) -> List[str]:
        return [path]

    @property
    def default_model(self) -> str:
        return "default"

    # ── saúde ────────────────────────────────────────────────────────────────
    async def _run_quick(self, args: List[str], timeout: float = 20.0):
        base = self.launcher()
        proc = await asyncio.create_subprocess_exec(*base, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode, (out + b"\n" + err).decode("utf-8", errors="replace").strip()

    async def health_check(self) -> HealthStatus:
        if not self.launcher():
            return HealthStatus(healthy=False, error_message=f"{self.label} não está instalado. {self.install_hint}")
        try:
            code, text = await self._run_quick(["--version"])
        except Exception as exc:                     # noqa: BLE001
            return HealthStatus(healthy=False, error_message=f"Falha ao executar {self.label}: {type(exc).__name__}")
        if code != 0:
            return HealthStatus(healthy=False, error_message=f"{self.label} retornou código {code}: {text[:200]}")
        version = text.splitlines()[-1] if text else ""
        if self.status_args:
            try:
                _, status = await self._run_quick(self.status_args)
                if NOT_LOGGED.search(status):
                    return HealthStatus(healthy=False, version=version,
                                        error_message=f"{self.label} sem login. {self.login_hint}")
            except Exception:                        # noqa: BLE001 - sem o status, confia na versão
                pass
        return HealthStatus(healthy=True, version=version, available_models=[])

    async def capabilities(self) -> AgentCapabilities:
        return AgentCapabilities(supports_code_edit=True, supports_terminal=True, supports_streaming=False,
                                 supports_json_schema=False, supports_vision=False, is_local_offline=False)

    # ── execução ─────────────────────────────────────────────────────────────
    def _build_prompt(self, task: AgentTask) -> str:
        prompt = f"[TAREFA: {task.title}]\nInstrução:\n{task.instruction}\n"
        if task.acceptance_criteria:
            prompt += "\nCritérios de Aceite:\n" + "\n".join(f"- {c}" for c in task.acceptance_criteria)
        return prompt + images_mod.prompt_note(task.images)

    def build_args(self, task: AgentTask, prompt: str, model: Optional[str], workspace: Optional[str]) -> List[str]:
        raise NotImplementedError

    async def _execute_cli(self, task: AgentTask, handle: RunHandle) -> AgentResult:
        start = handle.start_time
        base = self.launcher()
        if not base:
            return AgentResult(task_id=task.id, status=TaskStatus.FAILED, summary=f"{self.label} não instalado",
                               error_message=f"{self.label} não está instalado. {self.install_hint}")
        if task.workspace and not os.path.isdir(task.workspace):
            return AgentResult(task_id=task.id, status=TaskStatus.FAILED, summary="Falha de validação de workspace",
                               error_message=f"Workspace '{task.workspace}' não existe ou é inválido. Execução abortada por segurança.")
        timeout = task.timeout_seconds or self.timeout
        prompt = self._build_prompt(task)
        prompt = prompt if self.prompt_by_stdin else fit_prompt(prompt)
        cmd = [*base, *self.build_args(task, prompt, clean_model(task.assigned_model), task.workspace or None)]

        class _Text:                                  # saída é texto puro: cada linha vira progresso e vai para o resultado
            def __init__(self) -> None:
                self.lines: List[str] = []

            def feed(self, line: str) -> None:
                self.lines.append(line)
                if len(self.lines) <= 400:
                    say(TEXT, line[:300])

        parser = _Text()
        try:
            say(STATUS, f"Executando no {self.label} (modelo {task.assigned_model or 'padrao'})...")

            def remember(proc):
                self._active_processes[task.id] = proc
                handle.process_id = proc.pid

            code, raw, err = await run_streaming(cmd, task.workspace or None, timeout, parser, on_proc=remember,
                                                 stdin_text=prompt if self.prompt_by_stdin else None)
            elapsed = time.time() - start
            output = raw.strip()
            if code == 0 and output:
                return AgentResult(task_id=task.id, status=TaskStatus.SUCCEEDED,
                                   summary=f"{self.label} concluiu a tarefa em {elapsed:.2f}s", output_text=output,
                                   execution_time_seconds=round(elapsed, 2))
            reason = err or output[:500] or f"{self.label} terminou sem resposta."
            if NOT_LOGGED.search(reason):
                reason = f"{self.label} sem login. {self.login_hint}"
            return AgentResult(task_id=task.id, status=TaskStatus.FAILED, summary=f"{self.label} finalizou com código {code}",
                               output_text=output, error_message=reason[:800], execution_time_seconds=round(elapsed, 2))
        except asyncio.TimeoutError as exc:
            if task.id in self._active_processes:
                _kill_tree(self._active_processes[task.id])
            idle = type(exc).__name__ == "IdleTimeout"
            return AgentResult(task_id=task.id, status=TaskStatus.FAILED,
                               summary=f"{self.label} parado: {exc}" if idle else f"Timeout atingido ({timeout}s) no {self.label}",
                               error_message=f"Sem saída do agente: {exc}." if idle else f"Execução excedeu limite de {timeout} segundos",
                               execution_time_seconds=round(time.time() - start, 2))
        except asyncio.CancelledError:
            if task.id in self._active_processes:
                _kill_tree(self._active_processes[task.id])
            return AgentResult(task_id=task.id, status=TaskStatus.CANCELLED, summary="Execução cancelada pelo orquestrador",
                               execution_time_seconds=round(time.time() - start, 2))
        except Exception as exc:                     # noqa: BLE001
            return AgentResult(task_id=task.id, status=TaskStatus.FAILED, summary=f"Exceção na execução do {self.label}",
                               error_message=f"{type(exc).__name__}: {exc}", execution_time_seconds=round(time.time() - start, 2))
        finally:
            self._active_processes.pop(task.id, None)

    async def submit(self, task: AgentTask) -> RunHandle:
        handle = RunHandle(task_id=task.id, start_time=time.time())
        self._active_tasks[task.id] = asyncio.create_task(self._execute_cli(task, handle))
        return handle

    async def collect(self, handle: RunHandle) -> AgentResult:
        exec_task = self._active_tasks.get(handle.task_id)
        if not exec_task:
            return self._results.get(handle.task_id) or AgentResult(
                task_id=handle.task_id, status=TaskStatus.FAILED, summary="Tarefa não encontrada no pool de execução",
                error_message="Handle inválido")
        try:
            result = await exec_task
            self._results[handle.task_id] = result
            return result
        finally:
            self._active_tasks.pop(handle.task_id, None)

    async def cancel(self, handle: RunHandle) -> bool:
        proc = self._active_processes.get(handle.task_id)
        if proc:
            _kill_tree(proc)
        exec_task = self._active_tasks.get(handle.task_id)
        if exec_task and not exec_task.done():
            exec_task.cancel()
            return True
        return False


def _dirs(task: AgentTask, workspace: Optional[str]) -> List[str]:
    return [d for d in dict.fromkeys([*task.extra_dirs, *images_mod.image_dirs(task.images)]) if os.path.isdir(d) and d != workspace]


class CodexAdapter(HeadlessCliAdapter):
    """OpenAI Codex CLI (`codex exec`). Login: `codex login` (conta ChatGPT ou chave da OpenAI)."""
    name, label, env_var = "codex", "Codex (OpenAI)", "HERA_CODEX_PATH"
    exe_names = ["codex"]
    npm_entry = ("@openai", "codex/bin/codex.js")
    status_args = ["login", "status"]
    login_hint = "Rode `codex login` no terminal."
    install_hint = "Instale com `npm install -g @openai/codex`."
    prompt_by_stdin = True                           # `codex exec` lê o prompt da entrada padrão: sem limite de tamanho de linha

    def build_args(self, task, prompt, model, workspace):
        args = ["exec", "--skip-git-repo-check", "--color", "never", "-s", "workspace-write" if self.auto_approve else "read-only"]
        if workspace:
            args += ["-C", workspace]
        for d in _dirs(task, workspace):
            args += ["--add-dir", d]
        if model:
            args += ["-m", model]
        return args + ["-"]


class CursorAdapter(HeadlessCliAdapter):
    """Cursor Agent (`agent -p`). Login: `agent login` ou a variável CURSOR_API_KEY."""
    name, label, env_var = "cursor", "Cursor Agent", "HERA_CURSOR_PATH"
    exe_names = ["agent", "cursor-agent"]
    status_args = ["status"]
    login_hint = "Rode `agent login` no terminal."
    install_hint = "Instale pelo instalador oficial (cursor.com/install)."

    def _extra_launcher(self):
        root = Path(os.environ.get("LOCALAPPDATA", "")) / "cursor-agent" / "versions"
        if root.is_dir():
            for version in sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True):
                node, index = version / "node.exe", version / "index.js"
                if node.exists() and index.exists():
                    return [str(node), str(index)]
        return None

    def build_args(self, task, prompt, model, workspace):
        args = ["-p", "--output-format", "text", "--trust"]
        if workspace:
            args += ["--workspace", workspace]
        if self.auto_approve:
            args += ["--force"]
        if model:
            args += ["--model", model]
        return args + [prompt]


class QoderAdapter(HeadlessCliAdapter):
    """Qoder CLI (`qodercli -p`). Login: `qodercli login` ou QODER_PERSONAL_ACCESS_TOKEN."""
    name, label, env_var = "qoder", "Qoder", "HERA_QODER_PATH"
    exe_names = ["qodercli"]
    npm_entry = ("@qoder-ai", "qodercli/bundle/qodercli.js")
    status_args = ["status"]
    login_hint = "Rode `qodercli login` no terminal."
    install_hint = "Instale com `npm install -g @qoder-ai/qodercli`."

    def build_args(self, task, prompt, model, workspace):
        args = ["-p", "-o", "text"]
        if workspace:
            args += ["-w", workspace]
        if self.auto_approve:
            args += ["--permission-mode", "accept_edits"]
        if model:
            args += ["-m", model]
        return args + [prompt]


class CodeBuddyAdapter(HeadlessCliAdapter):
    """CodeBuddy Code (Tencent, `codebuddy -p`). Login: dentro do próprio `codebuddy` (ou variável de chave do serviço)."""
    name, label, env_var = "codebuddy", "CodeBuddy", "HERA_CODEBUDDY_PATH"
    exe_names = ["codebuddy", "cbc"]
    npm_entry = ("@tencent-ai", "codebuddy-code/bin/codebuddy")
    login_hint = "Abra `codebuddy` no terminal e faça o login."
    install_hint = "Instale com `npm install -g @tencent-ai/codebuddy-code`."

    def build_args(self, task, prompt, model, workspace):
        args = ["-p", "--output-format", "text"]
        for d in _dirs(task, workspace):
            args += ["--add-dir", d]
        if self.auto_approve:
            args += ["--permission-mode", "acceptEdits"]
        if model:
            args += ["--model", model]
        return args + [prompt]


class KimiAdapter(HeadlessCliAdapter):
    """Kimi Code (Moonshot, `kimi -p`). Login: `kimi login`."""
    name, label, env_var = "kimi", "Kimi Code", "HERA_KIMI_PATH"
    exe_names = ["kimi"]
    npm_entry = ("@moonshot-ai", "kimi-code/dist/main.mjs")
    login_hint = "Rode `kimi login` no terminal."
    install_hint = "Instale com `npm install -g @moonshot-ai/kimi-code`."

    def build_args(self, task, prompt, model, workspace):
        args = ["--output-format", "text"]
        if self.auto_approve:
            args += ["--yolo"]
        if model:
            args += ["-m", model]
        return args + ["-p", prompt]


CLI_ENGINES = {c.name: c for c in (CodexAdapter, CursorAdapter, QoderAdapter, CodeBuddyAdapter, KimiAdapter)}
