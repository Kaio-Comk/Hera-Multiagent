"""Fase 3: perfis por ARGUMENTO na politica de comandos.

Antes, `git`, `pytest`, `mypy`, `ruff`... eram liberados pelo NOME do programa e qualquer argumento
passava: `git branch -D`, `git diff --output=arq` (escreve arquivo), `pytest -p modulo` (executa
codigo), `mypy --install-types` (instala pacote), `ruff check --fix` (reescreve fontes)."""
import asyncio
from pathlib import Path

import pytest

from app.policy_engine import PolicyEngine
from app.validator import TaskValidator

ROOT = str(Path(__file__).resolve().parent.parent)
engine = PolicyEngine()


def ok(cmd, workspace=ROOT):
    allowed, reason = engine.evaluate_command(cmd, workspace=workspace)
    return allowed


# ── git: allowlist estrita ─────────────────────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    "git status", "git status --short", "git diff", "git diff --stat HEAD~1", "git log --oneline -5",
    "git log -p --stat", "git diff -c", "git branch", "git branch --list", "git branch -a",
    "git branch --show-current", "git branch --list feature/*",
])
def test_git_leitura_continua_permitida(cmd):
    assert ok(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "git branch -D master", "git branch -d x", "git branch -m velho novo", "git branch -M x",
    "git branch --delete x", "git branch --move a b", "git branch -c a b", "git branch nova-branch",
    "git branch -f x HEAD~3", "git branch --set-upstream-to=origin/x",
    "git diff --output=C:\\temp\\x.txt", "git log --output=x.txt", "git diff --output x",
    "git diff --no-index a b", "git diff --ext-diff", "git diff --textconv", "git diff -Oordem.txt",
    "git log --exec-path", "git diff --git-dir=C:\\outro\\.git",
    "git -c core.pager=calc.exe diff", "git -c alias.x=!calc status",
    "git config --global core.editor calc", "git push", "git checkout master", "git reset --hard",
    "git clean -fd", "git remote add x http://evil", "git fetch", "git stash",
])
def test_git_perigoso_bloqueado(cmd):
    assert not ok(cmd), cmd


# ── pytest: sem plugin, config externa, relatorio nem caminho de fora ──────────
@pytest.mark.parametrize("cmd", [
    "pytest", "pytest -q -x", "pytest tests", "pytest tests/test_auth.py::test_token_e_criado_uma_vez_e_reaproveitado",
    "python -m pytest tests -q", "python -m pytest -k \"policy and not slow\"", "pytest -p no:cacheprovider",
    "pytest --tb=short -vv", "pytest -n 2 --maxfail=3",
])
def test_pytest_normal_continua_permitido(cmd):
    assert ok(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "pytest -p plugin_malicioso", "pytest -pplugin_malicioso", "python -m pytest -p evil",
    "pytest -c C:\\evil\\pytest.ini", "pytest --config-file=evil.ini", "pytest -o addopts=-pevil",
    "pytest --override-ini=python_files=*", "pytest --rootdir=C:\\", "pytest --confcutdir=C:\\",
    "pytest --pyargs os", "pytest --basetemp=C:\\x", "pytest --junitxml=C:\\x\\r.xml",
    "pytest --log-file=C:\\x\\log.txt", "pytest --html=r.html", "pytest --pdb", "pytest --trace",
    "pytest --pdbcls=evil:Pdb",
])
def test_pytest_perigoso_bloqueado(cmd):
    assert not ok(cmd), cmd


def test_pytest_caminho_fora_do_workspace_bloqueado(tmp_path):
    fora = tmp_path / "outro"
    fora.mkdir()
    assert not ok(f'pytest "{fora}"')
    assert not ok(f'python -m pytest "{fora}"')
    assert not ok(f'python -m py_compile "{fora / "x.py"}"')
    assert ok(f'pytest "{fora}"', workspace=str(tmp_path))                 # dentro do SEU workspace: ok
    assert not ok("pytest ..\\fora")                                       # traversal ja era barrado


# ── linters ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cmd", [
    "ruff check .", "ruff check app --select E", "ruff --version", "flake8 app", "flake8 --max-line-length=120 app",
    "mypy app", "mypy --strict app", "black --check app", "black --check --diff .",
])
def test_linter_normal_continua_permitido(cmd):
    assert ok(cmd), cmd


@pytest.mark.parametrize("cmd", [
    "ruff check --fix .", "ruff check --fix-only .", "ruff check --unsafe-fixes .", "ruff check --add-noqa .",
    "ruff format .", "ruff check --config C:\\evil\\ruff.toml .", "ruff check --output-file=x.txt .",
    "flake8 --output-file=x.txt app", "flake8 --append-config=evil.cfg app", "flake8 --install-hook git",
    "mypy --install-types app", "mypy --non-interactive app", "mypy --python-executable C:\\evil\\py.exe app",
    "mypy --config-file=evil.ini app", "mypy --html-report out app", "mypy --txt-report=out app",
    "mypy --junit-xml x.xml app", "mypy --cache-dir=C:\\x app",
    "black app", "black --check --config C:\\evil\\pyproject.toml .",
])
def test_linter_perigoso_bloqueado(cmd):
    assert not ok(cmd), cmd


# ── comportamento antigo preservado ────────────────────────────────────────────
def test_sem_workspace_a_checagem_de_caminho_nao_se_aplica_mas_as_opcoes_continuam_bloqueadas():
    assert engine.evaluate_command("pytest tests")[0]
    assert not engine.evaluate_command("pytest -p evil")[0]
    assert not engine.evaluate_command("git branch -D x")[0]


# ── o validador usa a politica antes de executar ───────────────────────────────
def test_validador_nao_executa_comando_bloqueado():
    v = TaskValidator()
    code, out, err = asyncio.run(v._run_command("git branch -D master", cwd=ROOT))
    assert code == -403 and "bloquead" in err.lower()
    code, _, err = asyncio.run(v._run_command("pytest -p evil", cwd=ROOT))
    assert code == -403
