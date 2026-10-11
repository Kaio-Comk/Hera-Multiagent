import os
import re
import uuid
import shutil
import hashlib
import subprocess
from pathlib import Path
from typing import Dict, List, Optional, Tuple


class GitSecurityError(RuntimeError):
    """Erro lançado quando uma operação Git viola invariantes de segurança."""
    pass


class DirtyWorkingTreeError(RuntimeError):
    """Erro lançado quando o repositório de destino possui alterações locais não salvas."""
    pass


class MergeConflictError(RuntimeError):
    """Erro lançado quando ocorre conflito no merge."""
    pass


SAFE_BRANCH_PATTERN = re.compile(r"^[a-zA-Z0-9_\-\./]+$")
PROTECTED_BRANCHES = frozenset({"main", "master", "develop", "production", "release"})


def validate_branch_name(branch_name: str) -> str:
    """Valida sintaxe de nome de branch para evitar flag injection e traversal."""
    if not branch_name or not isinstance(branch_name, str):
        raise GitSecurityError("Nome de branch inválido ou vazio.")
    cleaned = branch_name.strip()
    if cleaned.startswith("-"):
        raise GitSecurityError(f"Nome de branch não pode iniciar com '-': {cleaned}")
    if ".." in cleaned or "/." in cleaned or "./" in cleaned:
        raise GitSecurityError(f"Nome de branch não pode conter navegação de caminho: {cleaned}")
    if not SAFE_BRANCH_PATTERN.match(cleaned):
        raise GitSecurityError(f"Nome de branch contém caracteres proibidos: {cleaned}")
    return cleaned


class WorktreeEntry:
    def __init__(
        self,
        worktree_id: str,
        worktree_path: str,
        task_id: str,
        branch_name: str,
        created_at: float,
    ):
        self.worktree_id = worktree_id
        self.worktree_path = worktree_path
        self.task_id = task_id
        self.branch_name = branch_name
        self.created_at = created_at

    def to_dict(self) -> Dict[str, any]:
        return {
            "worktree_id": self.worktree_id,
            "worktree_path": self.worktree_path,
            "task_id": self.task_id,
            "branch_name": self.branch_name,
            "created_at": self.created_at,
        }


class WorktreeRegistry:
    """Registro determinístico em memória de worktrees isoladas criadas pelo HERA."""

    def __init__(self):
        self._entries: Dict[str, WorktreeEntry] = {}

    def register(self, entry: WorktreeEntry):
        self._entries[entry.worktree_id] = entry

    def get(self, worktree_id: str) -> Optional[WorktreeEntry]:
        return self._entries.get(worktree_id)

    def is_registered(self, worktree_id: str) -> bool:
        return worktree_id in self._entries

    def is_path_registered(self, path: str) -> bool:
        norm = os.path.abspath(path)
        return any(os.path.abspath(e.worktree_path) == norm for e in self._entries.values())

    def unregister(self, worktree_id: str) -> Optional[WorktreeEntry]:
        return self._entries.pop(worktree_id, None)

    def list_all(self) -> List[WorktreeEntry]:
        return list(self._entries.values())


class GitManager:
    """Executor seguro de comandos Git estruturados com isolamento de worktree."""

    def __init__(self, registry: Optional[WorktreeRegistry] = None):
        self.registry = registry or WorktreeRegistry()

    def _run_git(
        self,
        args: List[str],
        cwd: str,
        check: bool = True,
        env: Optional[Dict[str, str]] = None
    ) -> subprocess.CompletedProcess:
        """Executa comando Git com argv estruturado (shell=False) e cwd obrigatório."""
        cwd_path = Path(cwd).resolve()
        if not cwd_path.exists() or not cwd_path.is_dir():
            raise GitSecurityError(f"Diretório de trabalho Git inválido: {cwd}")

        full_cmd = ["git"] + args
        custom_env = os.environ.copy()
        if env:
            custom_env.update(env)

        try:
            res = subprocess.run(
                full_cmd,
                cwd=str(cwd_path),
                capture_output=True,
                text=True,
                check=check,
                shell=False,
                env=custom_env
            )
            return res
        except subprocess.CalledProcessError as e:
            cmd_str = " ".join(args)
            err_msg = (e.stderr or e.stdout or "").strip()
            raise RuntimeError(f"Comando Git falhou: git {cmd_str} -> {err_msg}") from e

    def compute_diff_hash(self, diff_content: str) -> str:
        """Calcula hash SHA-256 canônico do conteúdo de um diff serializado."""
        return hashlib.sha256(diff_content.encode("utf-8")).hexdigest()

    def get_current_commit(self, repo_path: str, ref: str = "HEAD") -> str:
        """Obtém o hash SHA de commit para uma referência Git."""
        validate_branch_name(ref)
        res = self._run_git(["rev-parse", ref], cwd=repo_path)
        return res.stdout.strip()

    def is_working_tree_clean(self, repo_path: str) -> bool:
        """Verifica se o repositório não possui modificações unstaged ou staged."""
        res = self._run_git(["status", "--porcelain"], cwd=repo_path)
        return len(res.stdout.strip()) == 0

    def get_diff(self, repo_path: str, base_ref: str, head_ref: str) -> str:
        """Extrai o diff unificado entre dois commits/branches."""
        validate_branch_name(base_ref)
        validate_branch_name(head_ref)
        res = self._run_git(["diff", f"{base_ref}..{head_ref}"], cwd=repo_path)
        return res.stdout

    def create_worktree(
        self,
        repo_path: str,
        task_id: str,
        branch_name: str,
        base_ref: str = "HEAD",
        worktree_root: Optional[str] = None
    ) -> WorktreeEntry:
        """Cria uma worktree isolada para o agente trabalhar de forma segura."""
        clean_branch = validate_branch_name(branch_name)
        validate_branch_name(base_ref)

        worktree_id = f"wt-{uuid.uuid4().hex[:12]}"
        repo_p = Path(repo_path).resolve()
        
        target_root = Path(worktree_root).resolve() if worktree_root else repo_p.parent / ".hera_worktrees"
        target_root.mkdir(parents=True, exist_ok=True)
        worktree_path = target_root / worktree_id

        # Cria worktree com nova branch a partir de base_ref
        self._run_git(
            ["worktree", "add", "-b", clean_branch, str(worktree_path), base_ref],
            cwd=str(repo_p)
        )

        import time
        entry = WorktreeEntry(
            worktree_id=worktree_id,
            worktree_path=str(worktree_path),
            task_id=task_id,
            branch_name=clean_branch,
            created_at=time.time()
        )
        self.registry.register(entry)
        return entry

    def remove_worktree(self, repo_path: str, worktree_id: str, force: bool = False) -> bool:
        """Descarta uma worktree, exigindo que tenha sido registrada pelo HERA."""
        entry = self.registry.get(worktree_id)
        if not entry:
            raise GitSecurityError(
                f"Tentativa de remoção de worktree não autorizada/estranha ao HERA: {worktree_id}"
            )

        repo_p = Path(repo_path).resolve()
        wt_path = Path(entry.worktree_path)

        args = ["worktree", "remove", str(wt_path)]
        if force:
            args.append("--force")

        try:
            self._run_git(args, cwd=str(repo_p), check=False)
        except Exception:
            pass

        # Fallback de limpeza caso o diretório persista
        if wt_path.exists():
            try:
                shutil.rmtree(wt_path, ignore_errors=True)
            except Exception:
                pass

        # Prune worktrees inativas
        self._run_git(["worktree", "prune"], cwd=str(repo_p), check=False)

        # Deletar branch exclusiva associada caso seja prefixada com hera
        if entry.branch_name.startswith("hera/") or entry.branch_name.startswith("agent/"):
            try:
                self._run_git(["branch", "-D", entry.branch_name], cwd=str(repo_p), check=False)
            except Exception:
                pass

        self.registry.unregister(worktree_id)
        return True

    def perform_merge(
        self,
        repo_path: str,
        target_branch: str,
        feature_branch: str,
        commit_message: Optional[str] = None
    ) -> str:
        """Executa merge seguro entre feature_branch e target_branch.
        
        Garante que o repositório esteja limpo antes do merge.
        Se ocorrer conflito, aborta imediatamente para evitar corrupção.
        """
        clean_target = validate_branch_name(target_branch)
        clean_feature = validate_branch_name(feature_branch)

        # 1. Verificar se repositório está limpo
        if not self.is_working_tree_clean(repo_path):
            raise DirtyWorkingTreeError(
                "Merge cancelado: o repositório principal possui alterações locais não comitadas."
            )

        # 2. Checkout na branch alvo
        self._run_git(["checkout", clean_target], cwd=repo_path)

        # 3. Executar merge com commit estruturado
        msg = commit_message or f"merge(hera): merge branch {clean_feature} into {clean_target}"
        merge_cmd = ["merge", "--no-ff", "-m", msg, clean_feature]

        res = self._run_git(merge_cmd, cwd=repo_path, check=False)
        if res.returncode != 0:
            # Conflito detectado -> abortar imediatamente
            self._run_git(["merge", "--abort"], cwd=repo_path, check=False)
            err_output = (res.stderr or res.stdout or "").strip()
            raise MergeConflictError(
                f"Conflito de merge detectado entre {clean_feature} e {clean_target}. Operação abortada com segurança. {err_output}"
            )

        # 4. Retornar novo commit hash
        new_head = self.get_current_commit(repo_path, ref="HEAD")
        return new_head
