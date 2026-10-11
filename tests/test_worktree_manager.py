import os
import subprocess
import pytest
from app.worktree_manager import WorktreeManager

@pytest.fixture
def temp_git_repo(tmp_path):
    repo_dir = str(tmp_path / "repo")
    os.makedirs(repo_dir, exist_ok=True)

    # Inicializar git repo com commit inicial
    subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=repo_dir, check=True)

    dummy_file = os.path.join(repo_dir, "init.txt")
    with open(dummy_file, "w") as f:
        f.write("initial commit")

    subprocess.run(["git", "add", "."], cwd=repo_dir, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo_dir, check=True)

    return repo_dir

@pytest.mark.asyncio
async def test_worktree_lifecycle(temp_git_repo):
    wm = WorktreeManager(repo_root=temp_git_repo)

    # 1. Criar worktree
    wt_path = await wm.create_worktree("task-wt-01")
    assert os.path.exists(wt_path)
    assert os.path.exists(os.path.join(wt_path, "init.txt"))

    # 2. Modificar arquivo dentro do worktree
    mod_file = os.path.join(wt_path, "feature.txt")
    with open(mod_file, "w") as f:
        f.write("nova feature isolada")

    subprocess.run(["git", "add", "feature.txt"], cwd=wt_path, check=True)
    diff = await wm.get_diff("task-wt-01")
    assert "nova feature" in diff or "feature.txt" in diff

    # 3. Remover worktree
    removed = await wm.remove_worktree("task-wt-01")
    assert removed is True
    assert not os.path.exists(wt_path)
