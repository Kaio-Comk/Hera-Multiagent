"""Iniciar/parar o Ollama pelo painel: comandos fixos, ambiente enxuto, so para o que o Hera iniciou."""
import socket
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from app import services_control as sc
from helpers import AUTH_HEADERS, TEST_BASE_URL

FAKE = str(Path(__file__).parent / "fixtures" / "fake_service.py")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def svc(tmp_path, monkeypatch):
    port = free_port()
    spec = {"label": "Falso", "port": port, "url": f"http://127.0.0.1:{port}", "candidates": [sys.executable],
            "args": [FAKE, str(port)], "env": {}, "install_hint": "instale o falso"}
    monkeypatch.setitem(sc.SERVICES, "ollama", spec)
    mgr = sc.ServiceManager(tmp_path / "logs")
    yield mgr, port
    try:
        mgr.stop("ollama")
    except PermissionError:
        pass


def test_iniciar_e_parar_so_o_que_o_hera_iniciou(svc):
    mgr, port = svc
    assert mgr.status("ollama")["running"] is False
    info = mgr.start("ollama")
    assert mgr.wait_until_up("ollama", 15) is True
    st = mgr.status("ollama")
    assert st["running"] and st["managed"] and st["pid"] == info["pid"]
    mgr.stop("ollama")
    assert sc.port_open(port) is False and mgr.status("ollama")["managed"] is False


def test_processo_filho_nao_herda_chaves_nem_variaveis_do_hera(svc, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_" + "x" * 30)
    monkeypatch.setenv("HERA_API_KEY", "token-do-hera")
    mgr, _ = svc
    mgr.start("ollama")
    assert mgr.wait_until_up("ollama", 15)
    log = (mgr.log_dir / "ollama.log").read_text(encoding="utf-8")
    assert "SECRET_ENV=" in log and "GROQ_API_KEY" not in log and "HERA_API_KEY" not in log


def test_servico_aberto_por_fora_nao_e_encerrado(svc):
    mgr, port = svc
    outside = socket.socket()
    outside.bind(("127.0.0.1", port))
    outside.listen(50)
    try:
        assert mgr.status("ollama")["running"] and not mgr.status("ollama")["managed"]
        with pytest.raises(PermissionError):
            mgr.stop("ollama")
        assert mgr.start("ollama")["message"].endswith("já está rodando.")
    finally:
        outside.close()


def test_nao_instalado_da_erro_claro(svc, monkeypatch):
    mgr, _ = svc
    monkeypatch.setitem(sc.SERVICES["ollama"], "candidates", ["programa-que-nao-existe-xyz"])
    assert mgr.status("ollama")["installed"] is False
    with pytest.raises(FileNotFoundError, match="instale o falso"):
        mgr.start("ollama")


def test_servico_que_morre_na_partida_reporta_o_log(svc, monkeypatch):
    mgr, _ = svc
    monkeypatch.setitem(sc.SERVICES["ollama"], "args", ["-c", "print('explodiu feio'); raise SystemExit(3)"])
    mgr.start("ollama")
    assert mgr.wait_until_up("ollama", 10) is False
    st = mgr.status("ollama")
    assert st["exit_code"] == 3 and "explodiu feio" in st["last_log"]


@pytest.mark.asyncio
async def test_api_servicos(svc, monkeypatch):
    mgr, port = svc
    monkeypatch.setattr(server, "service_manager", mgr)
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS) as c:
        listed = (await c.get("/api/services")).json()["services"]
        assert {s["name"] for s in listed} == {"ollama"}
        assert (await c.post("/api/services/qualquer-coisa/start")).status_code == 404
        started = (await c.post("/api/services/ollama/start")).json()
        assert started["running"] is True and started["managed"] is True
        stopped = (await c.post("/api/services/ollama/stop")).json()
        assert stopped["running"] is False
        assert sc.port_open(port) is False
