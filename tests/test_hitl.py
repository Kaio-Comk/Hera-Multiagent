import os
import time
import subprocess
import pytest
from pathlib import Path

from app.hitl.schemas import (
    ApprovalRequest,
    RejectRequest,
    RequestChangesRequest,
    DiffProposal,
    DiffStats,
)
from app.hitl.git_manager import (
    GitManager,
    WorktreeRegistry,
    WorktreeEntry,
    GitSecurityError,
    DirtyWorkingTreeError,
    MergeConflictError,
    validate_branch_name,
)
from app.hitl.diff_analyzer import DiffAnalyzer
from app.hitl.approval_engine import (
    ApprovalEngine,
    ApprovalSecurityError,
    TokenExpiredError,
    ReplayAttackError,
    UnauthorizedError,
    StaleCommitError,
    DiffMutatedError,
)
from app.hitl.service import HitlService
from fastapi.testclient import TestClient
from app.server import app
from helpers import AUTH_HEADERS, TEST_BASE_URL


@pytest.fixture
def test_git_repo(tmp_path: Path):
    """Cria um repositório Git local temporário inicializado na branch 'master'."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def run_git(*args):
        subprocess.run(["git"] + list(args), cwd=str(repo), check=True, capture_output=True, text=True)

    run_git("init")
    run_git("checkout", "-B", "master")
    run_git("config", "user.name", "HeraTest")
    run_git("config", "user.email", "test@hera.local")
    run_git("config", "commit.gpgsign", "false")

    # Commit inicial
    f = repo / "main.py"
    f.write_text("def hello():\n    return 'hello world'\n", encoding="utf-8")
    run_git("add", "main.py")
    run_git("commit", "-m", "feat: commit inicial")

    return repo


def test_validate_branch_name_security():
    """Garante que injeção de flags e path traversal em nomes de branch sejam barrados."""
    assert validate_branch_name("feature/nova-tarefa") == "feature/nova-tarefa"
    assert validate_branch_name("hera/task-123") == "hera/task-123"

    with pytest.raises(GitSecurityError):
        validate_branch_name("--help")

    with pytest.raises(GitSecurityError):
        validate_branch_name("-b")

    with pytest.raises(GitSecurityError):
        validate_branch_name("../master")

    with pytest.raises(GitSecurityError):
        validate_branch_name("branch; rm -rf /")


def test_worktree_registry_access_control(tmp_path: Path):
    """Garante que worktrees estranhas não possam ser manipuladas ou deletadas."""
    registry = WorktreeRegistry()
    gm = GitManager(registry=registry)

    # Tentativa de remover worktree estranha
    with pytest.raises(GitSecurityError, match="Tentativa de remoção de worktree não autorizada"):
        gm.remove_worktree(repo_path=str(tmp_path), worktree_id="wt-fake-alien")


def test_diff_analyzer_secrets_and_sensitive_files():
    """Valida detecção de credenciais, arquivos sensíveis e estatísticas de diff."""
    diff_sample = """diff --git a/app/config.py b/app/config.py
--- a/app/config.py
+++ b/app/config.py
@@ -1,2 +1,3 @@
+# Nova chave adicionada
+OPENAI_API_KEY = "sk-1234567890abcdef1234567890abcdef"
-OLD_VAR = "deprecated"
diff --git a/.env b/.env
new file mode 100644
--- /dev/null
+++ b/.env
@@ -0,0 +1,1 @@
+SECRET=supersecret
"""
    stats = DiffAnalyzer.calculate_stats(diff_sample)
    assert stats.files_changed == 2
    assert stats.insertions >= 3
    assert stats.deletions >= 1

    alerts = DiffAnalyzer.analyze_alerts(diff_sample)
    alert_categories = [a.category for a in alerts]
    assert "secret" in alert_categories
    assert "sensitive_file" in alert_categories

    # Alerta de segredo não deve vazar a chave completa
    secret_alert = next(a for a in alerts if a.category == "secret")
    assert secret_alert.level == "danger"
    assert "sk-1234567890abcdef" not in secret_alert.message


def test_approval_engine_hmac_and_replay_protection():
    """Testa geração de tokens, verificação HMAC e bloqueio estrito contra Replay Attack."""
    engine = ApprovalEngine(token_ttl_seconds=10.0)
    proposal = DiffProposal(
        id="prop-1",
        task_id="task-1",
        repo_path=".",
        feature_branch="hera/test",
        target_branch="master",
        base_commit="abc1",
        head_commit="def2",
        diff_content="sample diff",
        diff_hash="hash123",
    )

    nonce = engine.generate_nonce()
    token = engine.create_approval_token(proposal, user_id="alice", nonce=nonce)

    # 1. Validação válida
    valid = engine.validate_and_consume_token(
        token=token,
        current_head_commit="def2",
        current_diff_hash="hash123",
        expected_user_id="alice"
    )
    assert valid is True

    # 2. Replay Attack: mesmo nonce deve falhar imediatamente
    with pytest.raises(ReplayAttackError, match="Replay Attack"):
        engine.validate_and_consume_token(
            token=token,
            current_head_commit="def2",
            current_diff_hash="hash123",
            expected_user_id="alice"
        )


def test_approval_engine_stale_commit_and_diff_mutation():
    """Garante que alterações no HEAD ou no diff após aprovação invalidem o merge."""
    engine = ApprovalEngine(token_ttl_seconds=10.0)
    proposal = DiffProposal(
        id="prop-2",
        task_id="task-2",
        repo_path=".",
        feature_branch="hera/test",
        target_branch="master",
        base_commit="abc1",
        head_commit="def2",
        diff_content="sample diff",
        diff_hash="hash123",
    )

    nonce1 = engine.generate_nonce()
    token1 = engine.create_approval_token(proposal, user_id="alice", nonce=nonce1)

    # HEAD mudou (novo commit adicionado)
    with pytest.raises(StaleCommitError, match="divergiu após a aprovação"):
        engine.validate_and_consume_token(
            token=token1,
            current_head_commit="new_commit_999",
            current_diff_hash="hash123"
        )

    # Diff mudou (código alterado sem novo commit ou squash)
    nonce2 = engine.generate_nonce()
    token2 = engine.create_approval_token(proposal, user_id="alice", nonce=nonce2)
    with pytest.raises(DiffMutatedError, match="sofreu mutação"):
        engine.validate_and_consume_token(
            token=token2,
            current_head_commit="def2",
            current_diff_hash="tampered_hash_456"
        )


def test_approval_engine_expiration_and_unauthorized():
    """Valida expiração por tempo (TTL) e divergência de usuário."""
    engine = ApprovalEngine(token_ttl_seconds=0.05)  # 50ms
    proposal = DiffProposal(
        id="prop-3",
        task_id="task-3",
        repo_path=".",
        feature_branch="hera/test",
        target_branch="master",
        base_commit="abc1",
        head_commit="def2",
        diff_content="sample diff",
        diff_hash="hash123",
    )

    nonce = engine.generate_nonce()
    token = engine.create_approval_token(proposal, user_id="alice", nonce=nonce)

    # Usuário errado
    with pytest.raises(UnauthorizedError, match="Autor da requisição"):
        engine.validate_and_consume_token(
            token=token,
            current_head_commit="def2",
            current_diff_hash="hash123",
            expected_user_id="mallory"
        )

    # Aguardar expirar
    time.sleep(0.06)
    with pytest.raises(TokenExpiredError, match="expirado"):
        engine.validate_and_consume_token(
            token=token,
            current_head_commit="def2",
            current_diff_hash="hash123",
            expected_user_id="alice"
        )


def test_hitl_service_full_workflow_merge(test_git_repo: Path):
    """Testa ciclo completo: proposta -> aprovação -> merge atômico verificado."""
    service = HitlService()
    repo_path = str(test_git_repo)

    # Criar branch de feature e fazer commit
    subprocess.run(["git", "checkout", "-b", "hera/task-feat"], cwd=repo_path, check=True)
    (test_git_repo / "main.py").write_text("def hello():\n    return 'hello world modified'\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "feat: atualiza saudação"], cwd=repo_path, check=True)
    subprocess.run(["git", "checkout", "master"], cwd=repo_path, check=True)

    # 1. Criar proposta
    prop = service.create_proposal(
        task_id="task-101",
        repo_path=repo_path,
        feature_branch="hera/task-feat",
        target_branch="master"
    )
    assert prop.status == "pending_review"
    assert prop.stats.files_changed == 1
    assert prop.diff_hash is not None

    # 2. Aprovar e mesclar
    nonce = service.approval_engine.generate_nonce()
    req = ApprovalRequest(
        proposal_id=prop.id,
        user_id="operator",
        session_id="sess-1",
        nonce=nonce,
        expected_diff_hash=prop.diff_hash
    )
    res = service.approve_and_merge(req)
    assert res.success is True
    assert res.merged_commit is not None
    assert prop.status == "merged"

    # Verificar que o master contém a alteração
    content = (test_git_repo / "main.py").read_text(encoding="utf-8")
    assert "hello world modified" in content


def test_hitl_service_dirty_working_tree_aborts_safely(test_git_repo: Path):
    """Garante que o merge é cancelado se houver trabalho local não comitado no repositório."""
    service = HitlService()
    repo_path = str(test_git_repo)

    subprocess.run(["git", "checkout", "-b", "hera/task-clean"], cwd=repo_path, check=True)
    (test_git_repo / "novo.txt").write_text("novo", encoding="utf-8")
    subprocess.run(["git", "add", "novo.txt"], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-m", "add novo"], cwd=repo_path, check=True)
    subprocess.run(["git", "checkout", "master"], cwd=repo_path, check=True)

    prop = service.create_proposal(
        task_id="task-102",
        repo_path=repo_path,
        feature_branch="hera/task-clean",
        target_branch="master"
    )

    # Deixar working tree suja
    (test_git_repo / "dirty_file.txt").write_text("unsaved user edits", encoding="utf-8")

    nonce = service.approval_engine.generate_nonce()
    req = ApprovalRequest(
        proposal_id=prop.id,
        user_id="operator",
        session_id="sess-2",
        nonce=nonce,
        expected_diff_hash=prop.diff_hash
    )
    res = service.approve_and_merge(req)
    assert res.success is False
    assert res.error == "DirtyWorkingTreeError"
    # O arquivo sujo do usuário permanece intacto!
    assert (test_git_repo / "dirty_file.txt").exists()


def test_hitl_service_merge_conflict_aborts_cleanly(test_git_repo: Path):
    """Garante que em caso de conflito de merge, a operação é abortada sem corromper o repositório."""
    service = HitlService()
    repo_path = str(test_git_repo)

    # Criar branch A
    subprocess.run(["git", "checkout", "-b", "hera/branch-a"], cwd=repo_path, check=True)
    (test_git_repo / "main.py").write_text("def hello():\n    return 'from branch a'\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "branch a change"], cwd=repo_path, check=True)

    # Criar conflito no master
    subprocess.run(["git", "checkout", "master"], cwd=repo_path, check=True)
    (test_git_repo / "main.py").write_text("def hello():\n    return 'from master branch'\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-am", "master change"], cwd=repo_path, check=True)

    prop = service.create_proposal(
        task_id="task-103",
        repo_path=repo_path,
        feature_branch="hera/branch-a",
        target_branch="master"
    )

    nonce = service.approval_engine.generate_nonce()
    req = ApprovalRequest(
        proposal_id=prop.id,
        user_id="operator",
        session_id="sess-3",
        nonce=nonce,
        expected_diff_hash=prop.diff_hash
    )
    res = service.approve_and_merge(req)
    assert res.success is False
    assert res.error == "MergeConflictError"

    # Verificar que master continua limpo e funcional após abort
    status_out = subprocess.run(["git", "status", "--porcelain"], cwd=repo_path, capture_output=True, text=True).stdout
    assert status_out.strip() == ""


def test_hitl_api_endpoints_integration(test_git_repo: Path):
    """Testa endpoints HTTP FastAPI (/api/hitl/*) com TestClient."""
    client = TestClient(app, base_url=TEST_BASE_URL, headers=AUTH_HEADERS)
    repo_path = str(test_git_repo)

    # Criar branch
    subprocess.run(["git", "checkout", "-b", "hera/task-api"], cwd=repo_path, check=True)
    (test_git_repo / "api_test.py").write_text("print('test')", encoding="utf-8")
    subprocess.run(["git", "add", "api_test.py"], cwd=repo_path, check=True)
    subprocess.run(["git", "commit", "-m", "add api test"], cwd=repo_path, check=True)
    subprocess.run(["git", "checkout", "master"], cwd=repo_path, check=True)

    # 1. POST /api/hitl/proposals
    resp = client.post("/api/hitl/proposals", json={
        "task_id": "task-api-1",
        "repo_path": repo_path,
        "feature_branch": "hera/task-api",
        "target_branch": "master"
    })
    assert resp.status_code == 200
    prop_data = resp.json()
    proposal_id = prop_data["id"]
    diff_hash = prop_data["diff_hash"]

    # 2. GET /api/hitl/proposals/{proposal_id}
    resp_get = client.get(f"/api/hitl/proposals/{proposal_id}")
    assert resp_get.status_code == 200
    assert resp_get.json()["id"] == proposal_id

    # 3. POST /api/hitl/nonces
    resp_nonce = client.post("/api/hitl/nonces")
    assert resp_nonce.status_code == 200
    nonce = resp_nonce.json()["nonce"]

    # 4. POST /api/hitl/approve
    resp_approve = client.post("/api/hitl/approve", json={
        "proposal_id": proposal_id,
        "user_id": "operator",
        "session_id": "session-test",
        "nonce": nonce,
        "expected_diff_hash": diff_hash,
        "comments": "Aprovado via teste de integração"
    })
    assert resp_approve.status_code == 200
    assert resp_approve.json()["success"] is True

    # 5. GET /api/hitl/worktrees
    resp_wt = client.get("/api/hitl/worktrees")
    assert resp_wt.status_code == 200
    assert isinstance(resp_wt.json(), list)
