import os
import shutil
import asyncio
import subprocess
from typing import Optional, Dict, Tuple

class WorktreeError(Exception):
    """Exceção levantada em operações de git worktree."""
    pass

class WorktreeManager:
    """Gerenciador de Git Worktrees para execução de tarefas em branches isolados.
    
    Garante que agentes concorrentes nunca escrevam no mesmo diretório de trabalho,
    permitindo merge controlado por diff e testes.
    """

    def __init__(self, repo_root: str, worktrees_dir: Optional[str] = None):
        self.repo_root = os.path.abspath(repo_root)
        self.worktrees_dir = os.path.abspath(worktrees_dir or os.path.join(self.repo_root, ".worktrees"))
        os.makedirs(self.worktrees_dir, exist_ok=True)
        self._active_worktrees: Dict[str, Dict[str, str]] = {}

    async def _run_git(self, args: list, cwd: Optional[str] = None) -> Tuple[int, str, str]:
        working_dir = cwd or self.repo_root
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=working_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        return (
            proc.returncode,
            stdout.decode("utf-8", errors="replace").strip(),
            stderr.decode("utf-8", errors="replace").strip()
        )

    async def create_worktree(self, task_id: str, branch_name: Optional[str] = None) -> str:
        """Cria um novo git worktree com branch isolado para a tarefa."""
        branch = branch_name or f"task/{task_id}"
        worktree_path = os.path.join(self.worktrees_dir, task_id)

        # Se o worktree já existe, remover primeiro para idempotência
        if os.path.exists(worktree_path):
            await self.remove_worktree(task_id)

        code, out, err = await self._run_git(["worktree", "add", "-b", branch, worktree_path])
        if code != 0:
            # Caso o branch já exista, tentar conectar sem -b
            code2, out2, err2 = await self._run_git(["worktree", "add", worktree_path, branch])
            if code2 != 0:
                raise WorktreeError(f"Falha ao criar git worktree para '{task_id}': {err or err2}")

        self._active_worktrees[task_id] = {
            "path": worktree_path,
            "branch": branch
        }
        return worktree_path

    async def get_diff(self, task_id: str) -> str:
        """Obtém o diff de alterações produzidas no worktree da tarefa."""
        info = self._active_worktrees.get(task_id)
        if not info or not os.path.exists(info["path"]):
            return ""

        code, out, _ = await self._run_git(["diff", "HEAD"], cwd=info["path"])
        return out

    async def remove_worktree(self, task_id: str, delete_branch: bool = True) -> bool:
        """Remove o worktree e limpa o branch temporário."""
        info = self._active_worktrees.pop(task_id, None)
        path = info["path"] if info else os.path.join(self.worktrees_dir, task_id)
        branch = info["branch"] if info else f"task/{task_id}"

        # 1. git worktree remove
        await self._run_git(["worktree", "remove", "--force", path])

        # Se o diretório ainda existir, limpar
        if os.path.exists(path):
            try:
                shutil.rmtree(path, ignore_errors=True)
            except Exception:
                pass

        # 2. Prune worktrees
        await self._run_git(["worktree", "prune"])

        # 3. Remover branch se solicitado
        if delete_branch:
            await self._run_git(["branch", "-D", branch])

        return True
