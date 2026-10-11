"""Testes unitários para o módulo de Repo-Map e otimização de contexto AST (Fase 4)."""

import pytest
from pathlib import Path

from app.repo_map.indexer import RepoMapIndexer
from app.repo_map.cache import RepoMapCache
from app.repo_map.formatter import RepoMapFormatter

def test_indexer_extracts_classes_and_methods(tmp_path: Path):
    """Garante que classes, heranças e métodos sejam identificados corretamente pelo AST."""
    code = """
class BaseService:
    def execute(self, task_id: str):
        pass

class UserService(BaseService):
    def __init__(self, db):
        self.db = db

    async def get_user(self, user_id):
        return {"id": user_id}
"""
    file_path = tmp_path / "service.py"
    file_path.write_text(code, encoding="utf-8")

    indexer = RepoMapIndexer(workspace_root=str(tmp_path))
    data = indexer.parse_python_file(file_path)

    assert len(data["classes"]) == 2
    class_names = [c["name"] for c in data["classes"]]
    assert "BaseService" in class_names
    assert "UserService" in class_names

    user_svc = next(c for c in data["classes"] if c["name"] == "UserService")
    method_names = [m["name"] for m in user_svc["methods"]]
    assert "__init__" in method_names
    assert "get_user" in method_names

def test_indexer_extracts_standalone_functions(tmp_path: Path):
    """Garante que funções top-level síncronas e assíncronas sejam extraídas."""
    code = """
def calculate_hash(data: bytes) -> str:
    return "hash"

async def fetch_remote_data(url: str, timeout: float = 10.0):
    pass
"""
    file_path = tmp_path / "helpers.py"
    file_path.write_text(code, encoding="utf-8")

    indexer = RepoMapIndexer(workspace_root=str(tmp_path))
    data = indexer.parse_python_file(file_path)

    assert len(data["functions"]) == 2
    fn_names = [f["name"] for f in data["functions"]]
    assert "calculate_hash" in fn_names
    assert "fetch_remote_data" in fn_names

    async_fn = next(f for f in data["functions"] if f["name"] == "fetch_remote_data")
    assert async_fn["is_async"] is True
    assert "url" in async_fn["args"]

def test_indexer_cache_avoids_reparsing(tmp_path: Path):
    """Valida que o cache por SHA-256 reutilize dados já calculados."""
    file_path = tmp_path / "cached_module.py"
    file_path.write_text("def stable_function(): pass\n", encoding="utf-8")

    cache = RepoMapCache()
    indexer = RepoMapIndexer(workspace_root=str(tmp_path), cache=cache)

    # 1ª leitura: Popula cache
    data1 = indexer.parse_python_file(file_path)
    assert len(data1["functions"]) == 1

    # 2ª leitura: Deve vir do cache
    data2 = indexer.parse_python_file(file_path)
    assert data1 is data2

    # Modificar arquivo: Invalida cache e re-parseia
    file_path.write_text("def updated_function(): pass\ndef second_func(): pass\n", encoding="utf-8")
    data3 = indexer.parse_python_file(file_path)
    assert len(data3["functions"]) == 2

def test_indexer_ignores_blacklisted_directories(tmp_path: Path):
    """Valida que diretórios como .git, .venv e __pycache__ sejam completamente ignorados."""
    # Arquivo legítimo
    (tmp_path / "app.py").write_text("def main(): pass\n", encoding="utf-8")

    # Arquivos em pastas ignoradas
    venv_dir = tmp_path / ".venv" / "lib"
    venv_dir.mkdir(parents=True)
    (venv_dir / "ignored_lib.py").write_text("def ignored(): pass\n", encoding="utf-8")

    git_dir = tmp_path / ".git" / "hooks"
    git_dir.mkdir(parents=True)
    (git_dir / "hook.py").write_text("def hook(): pass\n", encoding="utf-8")

    indexer = RepoMapIndexer(workspace_root=str(tmp_path))
    scanned = indexer.scan_workspace()

    assert "app.py" in scanned
    assert not any(".venv" in k for k in scanned.keys())
    assert not any(".git" in k for k in scanned.keys())

def test_indexer_protects_against_path_traversal(tmp_path: Path):
    """Garante que caminhos fora do workspace sejam bloqueados com ValueError."""
    indexer = RepoMapIndexer(workspace_root=str(tmp_path))
    outside_file = tmp_path.parent / "outside_leak.py"
    outside_file.write_text("def leak(): pass\n", encoding="utf-8")

    try:
        with pytest.raises(ValueError, match="path traversal"):
            indexer.parse_python_file(outside_file)
    finally:
        if outside_file.exists():
            outside_file.unlink()

def test_formatter_respects_token_budget():
    """Valida que o formatador respeite o orçamento máximo de tokens e trunque com aviso."""
    mock_scanned = {}
    for i in range(50):
        mock_scanned[f"modules/service_{i}.py"] = {
            "classes": [{"name": f"Service{i}", "methods": [{"name": "run"}]}],
            "functions": [{"name": f"helper_{i}", "args": ["a", "b"]}]
        }

    formatter = RepoMapFormatter(max_token_budget=100)  # Orçamento pequeno para forçar corte
    result = formatter.format_map(mock_scanned)

    assert len(result) <= (100 * 4 + 200)  # Margem pequena para cabeçalho/rodapé
    assert "MAPA ESTRUTURAL DO REPOSITÓRIO" in result
    assert "arquivos adicionais omitidos para respeitar o limite de contexto" in result
