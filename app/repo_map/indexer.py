"""Indexador AST nativo para arquivos Python no workspace.

Analisa a árvore sintática abstrata (ast) com zero dependências externas,
extraindo classes, métodos, assinaturas de funções e type hints de forma segura.
"""

import ast
from pathlib import Path
from typing import Dict, Any, List, Optional, Set

from app.repo_map.cache import RepoMapCache, compute_file_hash

DEFAULT_IGNORE_DIRS: Set[str] = {
    ".git", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".gemini", "dist", "build", ".egg-info"
}

MAX_FILE_SIZE_BYTES = 500 * 1024  # 500 KB

class RepoMapIndexer:
    """Indexador estrutural de código baseado na biblioteca padrão ast."""

    def __init__(
        self,
        workspace_root: str,
        cache: Optional[RepoMapCache] = None,
        ignore_dirs: Optional[Set[str]] = None
    ):
        self.workspace_root = Path(workspace_root).resolve()
        if not self.workspace_root.exists() or not self.workspace_root.is_dir():
            raise ValueError(f"Raiz do workspace inválida ou inexistente: {workspace_root}")
        self.cache = cache or RepoMapCache()
        self.ignore_dirs = ignore_dirs or DEFAULT_IGNORE_DIRS

    def should_ignore_dir(self, dir_name: str) -> bool:
        """Verifica se o diretório deve ser ignorado."""
        return dir_name in self.ignore_dirs or dir_name.startswith(".")

    def parse_python_file(self, file_path: Path) -> Dict[str, Any]:
        """Extrai símbolos estruturais de um arquivo Python via ast."""
        # Proteção contra path traversal
        resolved = file_path.resolve()
        if not resolved.is_relative_to(self.workspace_root):
            raise ValueError(f"Tentativa de path traversal detectada: {file_path}")

        # Proteção contra arquivos gigantes
        if resolved.stat().st_size > MAX_FILE_SIZE_BYTES:
            return {"classes": [], "functions": [], "imports": []}

        # Checar cache por SHA-256
        file_hash = compute_file_hash(resolved)
        cached_data = self.cache.get(str(resolved), file_hash)
        if cached_data is not None:
            return cached_data

        try:
            with open(resolved, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
            tree = ast.parse(source, filename=str(resolved))
        except (SyntaxError, Exception):
            # Se o arquivo tiver erro de sintaxe, retorna vazio com segurança
            data = {"classes": [], "functions": [], "imports": []}
            self.cache.put(str(resolved), file_hash, data)
            return data

        classes: List[Dict[str, Any]] = []
        functions: List[Dict[str, Any]] = []
        imports: List[str] = []

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                methods = []
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods.append({
                            "name": item.name,
                            "args": [arg.arg for arg in item.args.args],
                            "is_async": isinstance(item, ast.AsyncFunctionDef)
                        })
                classes.append({
                    "name": node.name,
                    "bases": [ast.unparse(b) for b in node.bases] if hasattr(ast, "unparse") else [],
                    "methods": methods
                })
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append({
                    "name": node.name,
                    "args": [arg.arg for arg in node.args.args],
                    "is_async": isinstance(node, ast.AsyncFunctionDef)
                })
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)

        data = {
            "classes": classes,
            "functions": functions,
            "imports": list(set(imports))
        }
        self.cache.put(str(resolved), file_hash, data)
        return data

    def scan_workspace(self, max_files: int = 200) -> Dict[str, Dict[str, Any]]:
        """Varre o workspace e indexa todos os arquivos .py relevantes."""
        results: Dict[str, Dict[str, Any]] = {}
        count = 0

        for path in self.workspace_root.rglob("*.py"):
            # Verificar se algum diretório pai está na lista de ignore
            parts = path.relative_to(self.workspace_root).parts
            if any(self.should_ignore_dir(p) for p in parts[:-1]):
                continue

            rel_path = str(path.relative_to(self.workspace_root)).replace("\\", "/")
            results[rel_path] = self.parse_python_file(path)
            count += 1
            if count >= max_files:
                break

        return results
