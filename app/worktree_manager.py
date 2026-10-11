import os
import re
import shutil
import asyncio
from pathlib import Path
from typing import Optional, Dict, Tuple, List

class WorktreeError(Exception):
    """Exceção levantada em operações de git worktree."""
    pass

class WorktreeManager:
    """Gerenciador seguro de Git Worktrees com validação estrita de paths e contenção.
    
    Garante isolamento entre tarefas concorrentes, impedindo que branches
    ou diretórios fora do escopo gerenciado sejam alterados ou apagados.
    """

    TASK_ID_REGEX = re.compile(r'^[a-zA-Z0-9_\-]+$')

    def __init__(self, repo_root: str, worktrees_dir: Optional[str] = None):
        self.repo_root = os.path.abspath(repo_root)
        self.worktrees_dir = os.path.abspath(worktrees_dir or os.path.join(self.repo_root, ".worktrees"))
        os.makedirs(self.worktrees_dir, exist_ok=True)
        self._active_worktrees: Dict[str, Dict[str, str]] = {}

    def _validate_task_id(self, task_id: str) -> Path:
        """Valida que o task_id é seguro e resolve o caminho estritamente dentro da raiz de worktrees."""
        if not self.TASK_ID_REGEX.match(task_id):
            raise WorktreeError(f"Segurança: task_id inválido ou contém caracteres inseguros: '{task_id}'")

        base = Path(self.worktrees_dir).resolve()
        target = (base / f"task_{task_id}").resolve()

        if not target.is_relative_to(base):
            raise WorktreeError(f"Segurança: tentativa de path traversal detectada para task_id '{task_id}'")

        return target

    async def _run_git(self, args: list, cwd: Optional[str] = None, timeout: float = 30.0) -> Tuple[int, str, str]:
        working_dir = cwd or self.repo_root
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            return (
                proc.returncode if proc.returncode is not None else -1,
                stdout.decode("utf-8", errors="replace").strip(),
                stderr.decode("utf-8", errors="replace").strip()
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return (-1, "", f"Comando git excedeu timeout de {timeout}s")

    async def create_worktree(self, task_id: str, branch_name: Optional[str] = None) -> str:
        """Cria um novo git worktree com branch isolado para a tarefa."""
        target_path = self._validate_task_id(task_id)
        worktree_path_str = str(target_path)
        branch = branch_name or f"task/{task_id}"

        # Se o worktree já existe fisicamente, tentar limpar antes
        if target_path.exists():
            await self.remove_worktree(task_id)

        code, out, err = await self._run_git(["worktree", "add", "-b", branch, worktree_path_str])
        if code != 0:
            # Caso o branch já exista, tentar conectar sem -b
            code2, out2, err2 = await self._run_git(["worktree", "add", worktree_path_str, branch])
            if code2 != 0:
                raise WorktreeError(f"Falha ao criar git worktree para '{task_id}': {err or err2}")

        self._active_worktrees[task_id] = {
            "path": worktree_path_str,
            "branch": branch
        }
        return worktree_path_str

    async def get_diff(self, task_id: str) -> str:
        """Obtém o diff de alterações produzidas no worktree da tarefa."""
        target_path = self._validate_task_id(task_id)
        if not target_path.exists():
            return ""

        code, out, _ = await self._run_git(["diff", "HEAD"], cwd=str(target_path))
        return out

    async def remove_worktree(self, task_id: str, delete_branch: bool = True) -> bool:
        """Remove o worktree comprovadamente gerenciado e limpa o branch temporário."""
        target_path = self._validate_task_id(task_id)
        info = self._active_worktrees.pop(task_id, None)
        path_str = str(target_path)
        branch = (info.get("branch") if info else None) or f"task/{task_id}"

        # 1. git worktree remove
        code, out, err = await self._run_git(["worktree", "remove", "--force", path_str])

        # Se git remove foi bem-sucedido e a pasta ainda existir, remover resquícios
        if code == 0 and target_path.exists():
            try:
                shutil.rmtree(target_path, ignore_errors=True)
            except Exception:
                pass

        # 2. Prune worktrees
        await self._run_git(["worktree", "prune"])

        # 3. Remover branch apenas se for estritamente um branch task/* do orquestrador
        if delete_branch and branch.startswith("task/"):
            await self._run_git(["branch", "-D", branch])

        return True

    async def cleanup_all_managed(self):
        """Reconcilia e limpa todos os worktrees gerenciados pelo orquestrador."""
        code, out, _ = await self._run_git(["worktree", "list", "--porcelain"])
        if code == 0 and out:
            base = Path(self.worktrees_dir).resolve()
            for line in out.splitlines():
                if line.startswith("worktree "):
                    wt_path = Path(line.split(" ", 1)[1].strip()).resolve()
                    if wt_path.is_relative_to(base) and wt_path != base:
                        await self._run_git(["worktree", "remove", "--force", str(wt_path)])
            await self._run_git(["worktree", "prune"])
