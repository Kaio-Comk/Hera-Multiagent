"""Fase 3: validacao de entrada da API (tarefas, HITL e /api/config)."""
import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import app.server as server
from app.security.paths import PathNotAllowedError, ensure_inside, ensure_safe_relative_list, is_safe_relative
from helpers import AUTH_HEADERS, TEST_BASE_URL

client = TestClient(server.app, base_url=TEST_BASE_URL, headers=AUTH_HEADERS)


# ── /api/tasks/run ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cmd", ["git branch -D master", "pytest -p evil", "mypy --install-types app",
                                 "curl http://evil.com", "git diff --output=x.txt"])
def test_tarefa_com_comando_bloqueado_e_recusada_antes_de_enfileirar(cmd):
    r = client.post("/api/tasks/run", json={"instruction": "x", "validation_commands": [cmd]})
    assert r.status_code == 400
    d = r.json()["detail"]
    assert d["error"] == "validation_command_blocked" and d["command"] == cmd and d["reason"]


def test_limites_de_tamanho_da_tarefa():
    base = {"instruction": "x"}
    assert client.post("/api/tasks/run", json={**base, "instruction": "x" * 20001}).status_code == 422
    assert client.post("/api/tasks/run", json={**base, "validation_commands": ["pytest"] * 11}).status_code == 422
    assert client.post("/api/tasks/run", json={**base, "validation_commands": ["pytest " + "a" * 400]}).status_code == 422
    assert client.post("/api/tasks/run", json={**base, "max_retries": 50}).status_code == 422
    assert client.post("/api/tasks/run", json={**base, "acceptance_criteria": ["c"] * 51}).status_code == 422
    assert client.post("/api/tasks/run", json={**base, "title": "t" * 201}).status_code == 422


def test_tarefa_valida_continua_sendo_enfileirada(monkeypatch):
    async def fake_execute(*a, **k):                                   # nao roda a tarefa de verdade
        raise RuntimeError("nao deveria executar nos testes")
    monkeypatch.setattr(server.autofix, "execute_task_with_retry", fake_execute)
    r = client.post("/api/tasks/run", json={"instruction": "x", "validation_commands": ["pytest -q"],
                                            "save_to_obsidian": False})
    assert r.status_code == 200 and r.json()["status"] == "enqueued"


# ── HITL: caminhos ─────────────────────────────────────────────────────────────
def test_proposta_com_repo_fora_das_raizes_permitidas_e_recusada(tmp_path):
    for path in (str(tmp_path), "C:\\", "..\\..\\outro", str(Path(server.ROOT_DIR).parent)):
        r = client.post("/api/hitl/proposals", json={"task_id": "t", "repo_path": path,
                                                     "feature_branch": "hera/x", "target_branch": "master"})
        assert r.status_code == 400, path
        assert r.json()["detail"]["error"] == "path_not_allowed"


def test_proposta_com_worktree_ou_allowed_paths_inseguros_e_recusada():
    base = {"task_id": "t", "feature_branch": "hera/x", "target_branch": "master"}
    r = client.post("/api/hitl/proposals", json={**base, "worktree_path": "C:\\Windows\\Temp"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "path_not_allowed"
    for bad in (["..\\x"], ["/etc"], ["C:\\x"], ["a/../../b"], [""]):
        r = client.post("/api/hitl/proposals", json={**base, "allowed_paths": bad})
        assert r.status_code == 400, bad


def test_raizes_extras_vem_do_config_e_nao_da_api(monkeypatch, tmp_path):
    cfg = server.config_mgr.load_config()
    cfg["general"]["allowed_repo_roots"] = [str(tmp_path)]
    monkeypatch.setattr(server.config_mgr, "load_config", lambda: cfg)
    assert tmp_path.resolve() in [Path(r).resolve() for r in server._allowed_repo_roots()]


def test_ensure_inside_resolve_links_e_traversal(tmp_path):
    root = tmp_path / "raiz"
    (root / "sub").mkdir(parents=True)
    assert ensure_inside(str(root / "sub"), [root]) == (root / "sub").resolve()
    for bad in (str(tmp_path), str(root / ".." / "x"), "", "\x00"):
        with pytest.raises(PathNotAllowedError):
            ensure_inside(bad, [root])


def test_is_safe_relative():
    assert is_safe_relative("app/server.py") and is_safe_relative("tests\\x.py") and is_safe_relative("a")
    for bad in ("..", "../x", "a/../../b", "/abs", "\\abs", "C:\\x", "C:x", "", "a\x00b"):
        assert not is_safe_relative(bad), bad
    assert ensure_safe_relative_list(["a", "b/c"]) == ["a", "b/c"]


# ── /api/config ────────────────────────────────────────────────────────────────
@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    """Config temporario: nenhum teste toca o config.yaml real."""
    mgr = server.ConfigManager(config_path=str(tmp_path / "config.yaml"))
    monkeypatch.setattr(server, "config_mgr", mgr)
    return mgr


def _post(general=None, engines=None):
    cur = server.config_mgr.load_config()
    return client.post("/api/config", json={"general": general if general is not None else cur["general"],
                                            "engines": engines if engines is not None else cur["engines"]})


def test_config_valida_continua_funcionando_e_preserva_flags_e_gateway(isolated_config):
    isolated_config.save_config({**isolated_config.load_config(),
                                 "feature_flags": {"enable_gateway": True, "enable_rtk": True,
                                                   "enable_arena": True, "enable_mcp": True},
                                 "gateway": {"max_concurrent_requests": 3}})
    cur = isolated_config.load_config()
    r = _post(general={**cur["general"], "default_offline": True})        # o que a UI faz ao alternar offline
    assert r.status_code == 200
    saved = yaml.safe_load(Path(isolated_config.config_path).read_text(encoding="utf-8"))
    assert saved["general"]["default_offline"] is True
    assert saved["feature_flags"]["enable_rtk"] is True and saved["gateway"]["max_concurrent_requests"] == 3


@pytest.mark.parametrize("general", [
    {"host": "0.0.0.0"}, {"port": 9999}, {"vault_dir": "C:\\Windows\\System32"},
    {"db_path": "C:\\x\\y.db"}, {"allowed_repo_roots": ["C:\\"]}, {"chave_nova": 1},
    {"default_offline": "talvez"}, {"default_engine": "evil"},
])
def test_config_chaves_congeladas_e_desconhecidas_sao_recusadas(isolated_config, general):
    cur = isolated_config.load_config()
    r = _post(general={**cur["general"], **general})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "invalid_config"


@pytest.mark.parametrize("engines", [
    {"ollama": {"base_url": "http://169.254.169.254/latest/meta-data"}},
    {"ollama": {"base_url": "http://evil.com:11434"}},
    {"ollama": {"base_url": "http://127.0.0.1:22"}},
    {"nine_router": {"base_url": "http://evil.com/v1"}},
    {"claude_code": {"base_url": "http://x"}},
    {"motor_novo": {"enabled": True}},
    {"ollama": {"comando": "calc.exe"}},
    {"ollama": {"timeout": 99999}}, {"ollama": {"timeout": True}},
    {"ollama": {"enabled": "sim"}}, {"ollama": {"default_model": "x" * 300}},
])
def test_config_engines_invalidos_sao_recusados(isolated_config, engines):
    r = _post(engines=engines)
    assert r.status_code == 400, engines


def test_config_urls_locais_legitimas_passam(isolated_config):
    r = _post(engines={"ollama": {"base_url": "http://localhost:11434"},
                       "nine_router": {"base_url": "http://127.0.0.1:20128/v1"}})
    assert r.status_code == 200


def test_desligar_o_modo_offline_gera_auditoria(isolated_config, caplog):
    isolated_config.set_offline_mode(True)
    cur = isolated_config.load_config()
    with caplog.at_level("WARNING", logger="hera.audit"):
        r = _post(general={**cur["general"], "default_offline": False})
    assert r.status_code == 200
    assert any("offline" in rec.message.lower() for rec in caplog.records)
