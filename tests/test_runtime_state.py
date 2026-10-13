"""Banco de tarefas ancorado na pasta do projeto (nao no cwd), importacao do historico perdido e log em arquivo."""
import logging
import sqlite3
import time
from pathlib import Path

import pytest

import app.server as server
from app.runtime_state import merge_legacy_db, resolve_db_path, setup_file_logging
from app.state_store import StateStore
from models.task import AgentTask


def test_caminho_relativo_vale_a_partir_do_projeto_e_nao_do_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                                         # o usuario abriu o hera de outra pasta
    root = tmp_path / "projeto"
    assert resolve_db_path("data/orchestrator.db", root, {}) == root / "data" / "orchestrator.db"
    assert resolve_db_path(None, root, {}) == root / "data" / "orchestrator.db"
    assert resolve_db_path("  ", root, {}) == root / "data" / "orchestrator.db"


def test_variavel_de_ambiente_e_caminho_absoluto_vencem(tmp_path):
    root = tmp_path / "projeto"
    fixed = tmp_path / "outro" / "meu.db"
    assert resolve_db_path("data/x.db", root, {"HERA_DB_PATH": str(fixed)}) == fixed
    assert resolve_db_path(str(fixed), root, {}) == fixed


def make_db(path: Path, ids):
    store = StateStore(db_path=str(path))
    for tid in ids:
        store.create_task(AgentTask(id=tid, title=f"t {tid}", instruction="faz"))
    return store


def test_historico_perdido_na_pasta_antiga_e_importado_uma_vez(tmp_path):
    legacy, target = tmp_path / "antigo.db", tmp_path / "projeto.db"
    make_db(target, ["task-a"])
    make_db(legacy, ["task-a", "run-x-s1", "run-x-s2"])
    assert merge_legacy_db(legacy, target) == 2                           # a ja existente nao entra de novo
    with sqlite3.connect(target) as db:
        ids = {r[0] for r in db.execute("SELECT id FROM tasks")}
        events = db.execute("SELECT COUNT(*) FROM events WHERE task_id LIKE 'run-x-%'").fetchone()[0]
    assert ids == {"task-a", "run-x-s1", "run-x-s2"} and events >= 2     # eventos das tarefas importadas vieram junto
    assert merge_legacy_db(legacy, target) == 0                           # idempotente
    assert merge_legacy_db(target, target) == 0 and merge_legacy_db(tmp_path / "nao-existe.db", target) == 0


def test_importacao_ignora_arquivo_que_nao_e_um_banco(tmp_path):
    target = tmp_path / "projeto.db"
    make_db(target, ["task-a"])
    junk = tmp_path / "lixo.db"
    junk.write_text("isto nao e sqlite", encoding="utf-8")
    assert merge_legacy_db(junk, target) == 0


def test_log_em_arquivo_rotativo_mascara_segredos_e_nao_duplica(tmp_path):
    secret = "sk-" + "abcdef0123456789" * 2
    from app.privacy_guard import PrivacyGuard
    path = setup_file_logging(tmp_path / "logs", sanitize=PrivacyGuard.sanitize_content)
    assert setup_file_logging(tmp_path / "logs", sanitize=PrivacyGuard.sanitize_content) == path     # idempotente
    try:
        log = logging.getLogger("hera.teste")
        log.info("tarefa x terminou com %s", "ok")
        log.error("falhou usando a chave %s", secret)
        for handler in logging.getLogger("hera").handlers:
            handler.flush()
        text = path.read_text(encoding="utf-8")
        assert text.count("tarefa x terminou com ok") == 1
        assert "falhou usando a chave" in text and secret not in text and "abcdef0123456789" not in text
        assert "INFO" in text and "ERROR" in text and "hera.teste" in text
    finally:
        for handler in list(logging.getLogger("hera").handlers):
            if getattr(handler, "_hera_file", None) == str(path):
                logging.getLogger("hera").removeHandler(handler)
                handler.close()


def test_servidor_usa_o_banco_de_teste_e_nao_o_do_usuario():
    assert server.DB_PATH.name == "orchestrator-test.db"
    assert Path(server.store.db_path) == server.DB_PATH


@pytest.mark.asyncio
async def test_broadcast_log_vai_para_o_arquivo_de_log():
    server.broadcast_log("mensagem de teste do painel XYZ-123", level="warning")
    for handler in logging.getLogger("hera").handlers:
        handler.flush()
    text = server.LOG_FILE.read_text(encoding="utf-8")
    assert "mensagem de teste do painel XYZ-123" in text and "WARNING" in text
