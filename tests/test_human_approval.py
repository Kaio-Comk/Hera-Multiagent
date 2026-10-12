"""Fase 2: aprovação com prova humana.

Antes, quem tivesse o token da API aprovava merges sozinho: o servidor emitia o nonce, assinava o
token e aceitava o `user_id` do cliente; no MCP, `confirmed_hitl` era um booleano enviado pelo
próprio cliente. Agora é preciso um código mostrado só no console do servidor."""
import time

import pytest
from fastapi.testclient import TestClient

import app.server as server
from app.hitl.approval_engine import ApprovalEngine, ApprovalSecurityError, ReplayAttackError
from app.hitl.schemas import DiffProposal
from app.mcp.schemas import McpServerConfig, McpToolDefinition
from app.security.human_code import HumanApprovalCode, HumanCodeLockedError, load_or_create_secret
from helpers import AUTH_HEADERS, TEST_BASE_URL

client = TestClient(server.app, base_url=TEST_BASE_URL, headers=AUTH_HEADERS)
SECRET = b"s" * 32


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture(autouse=True)
def reset_human_code_state():
    """O HUMAN_CODE do servidor é global: zera tentativas e o limitador de anúncio entre testes."""
    server.HUMAN_CODE._failures = 0
    server.HUMAN_CODE._locked_until = 0.0
    server._last_code_announce = 0.0
    yield
    server.HUMAN_CODE._failures = 0
    server.HUMAN_CODE._locked_until = 0.0


# ── HumanApprovalCode ──────────────────────────────────────────────────────────
def test_codigo_tem_8_digitos_e_muda_a_cada_janela():
    clk = Clock()
    hc = HumanApprovalCode(SECRET, clock=clk)
    c1 = hc.current()
    assert len(c1) == 8 and c1.isdigit()
    clk.t += 100
    assert hc.current() == c1                       # mesma janela de 300 s
    clk.t += 400
    assert hc.current() != c1                       # janela seguinte
    assert HumanApprovalCode(b"outro-segredo-qualquer-123", clock=clk).current() != hc.current()


def test_verifica_janela_atual_e_anterior_mas_nao_a_antiga():
    clk = Clock()
    hc = HumanApprovalCode(SECRET, clock=clk)
    old = hc.current()
    clk.t += 300
    assert hc.verify(old) is True                   # virou a janela: ainda vale o código anterior
    clk.t += 300
    assert hc.verify(old) is False                  # duas janelas depois: expirou


def test_aceita_espacos_e_hifens_e_recusa_vazio_e_errado():
    hc = HumanApprovalCode(SECRET, clock=Clock())
    code = hc.current()
    assert hc.verify(f"{code[:4]} {code[4:]}") and hc.verify(f"{code[:4]}-{code[4:]}")
    for bad in (None, "", "   ", "abc", "00000000", code[:-1]):
        assert HumanApprovalCode(SECRET, clock=Clock()).verify(bad) is False     # instancia nova: sem bloqueio


def test_cinco_erros_bloqueiam_e_o_codigo_certo_tambem_fica_bloqueado():
    clk = Clock()
    hc = HumanApprovalCode(SECRET, clock=clk)
    for _ in range(5):
        assert hc.verify("11111111") is False
    with pytest.raises(HumanCodeLockedError) as exc:
        hc.verify(hc.current())                     # nem o certo passa durante o bloqueio
    assert 1 <= exc.value.retry_after <= 300
    clk.t += 301
    assert hc.verify(hc.current()) is True          # depois do bloqueio volta ao normal


def test_acerto_zera_o_contador_de_erros():
    hc = HumanApprovalCode(SECRET, clock=Clock())
    for _ in range(4):
        hc.verify("11111111")
    assert hc.verify(hc.current()) is True
    for _ in range(4):
        hc.verify("11111111")                       # mais 4 erros NÃO bloqueiam (contador zerou)
    assert hc.verify(hc.current()) is True


def test_segredo_persistente_e_codigo_igual_entre_processos(tmp_path):
    f = tmp_path / "data" / ".hera_approval_secret"
    s1 = load_or_create_secret(f)
    assert len(s1) >= 32 and load_or_create_secret(f) == s1
    clk = Clock()
    assert HumanApprovalCode(s1, clock=clk).current() == HumanApprovalCode(load_or_create_secret(f), clock=clk).current()
    with pytest.raises(ValueError):
        HumanApprovalCode(b"curto")


# ── ApprovalEngine: nonce emitido, limite e persistência ───────────────────────
def _proposal():
    return DiffProposal(id="p1", task_id="t1", repo_path=".", feature_branch="hera/x", target_branch="master",
                        base_commit="a", head_commit="b", diff_content="d", diff_hash="h")


def test_nonce_inventado_pelo_cliente_e_recusado():
    engine = ApprovalEngine()
    with pytest.raises(ApprovalSecurityError, match="não emitido"):
        engine.create_approval_token(_proposal(), user_id="u", nonce="nonce-que-eu-inventei")


def test_nonce_emitido_mas_vencido_e_recusado(monkeypatch):
    engine = ApprovalEngine(token_ttl_seconds=10.0)
    nonce = engine.generate_nonce()
    real = time.time()
    monkeypatch.setattr("app.hitl.approval_engine.time.time", lambda: real + 11)
    with pytest.raises(ApprovalSecurityError, match="expirado"):
        engine.create_approval_token(_proposal(), user_id="u", nonce=nonce)


def test_emissao_de_nonces_tem_teto_de_memoria():
    engine = ApprovalEngine()
    nonces = [engine.generate_nonce() for _ in range(engine.MAX_ISSUED_NONCES + 200)]
    assert len(engine._issued_nonces) <= engine.MAX_ISSUED_NONCES
    engine.create_approval_token(_proposal(), user_id="u", nonce=nonces[-1])        # o mais novo continua válido
    with pytest.raises(ApprovalSecurityError):
        engine.create_approval_token(_proposal(), user_id="u", nonce=nonces[0])      # o mais antigo foi descartado


def test_nonce_consumido_sobrevive_a_reinicio(tmp_path):
    state = tmp_path / "nonces.json"
    e1 = ApprovalEngine(state_path=state)
    nonce = e1.generate_nonce()
    token = e1.create_approval_token(_proposal(), user_id="u", nonce=nonce)
    assert e1.validate_and_consume_token(token, current_head_commit="b", current_diff_hash="h")
    e2 = ApprovalEngine(state_path=state)           # "reinício" do servidor
    with pytest.raises(ReplayAttackError):
        e2.create_approval_token(_proposal(), user_id="u", nonce=nonce)


def test_estado_corrompido_ou_expirado_nao_derruba_o_motor(tmp_path):
    state = tmp_path / "nonces.json"
    state.write_text("{isso nao e json", encoding="utf-8")
    assert ApprovalEngine(state_path=state)._consumed_nonces == set()
    state.write_text('{"consumed": {"velho": 1}}', encoding="utf-8")                 # expirou em 1970
    assert ApprovalEngine(state_path=state)._consumed_nonces == set()


# ── API: aprovar exige o código ────────────────────────────────────────────────
def _approve(code=None, user_id="evil-admin"):
    body = {"proposal_id": "prop-inexistente", "user_id": user_id, "session_id": "s", "nonce": "n",
            "expected_diff_hash": "h"}
    if code is not None:
        body["human_code"] = code
    return client.post("/api/hitl/approve", json=body)


def test_aprovar_sem_codigo_e_403_e_o_codigo_aparece_no_console(capsys):
    r = _approve()
    assert r.status_code == 403 and r.json()["detail"]["error"] == "human_code_required"
    out = capsys.readouterr().out
    assert server.HUMAN_CODE.current() in out and "prop-inexistente" in out
    assert server.HUMAN_CODE.current() not in r.text                 # o código NUNCA volta na resposta da API


def test_codigo_errado_e_403_e_nao_vaza_o_certo():
    r = _approve("00000000")
    assert r.status_code == 403 and r.json()["detail"]["error"] == "human_code_invalid"
    assert server.HUMAN_CODE.current() not in r.text


def test_cinco_codigos_errados_bloqueiam_com_429_e_retry_after():
    for _ in range(5):
        assert _approve("00000000").status_code == 403
    r = _approve(server.HUMAN_CODE.current())                        # nem o certo passa enquanto bloqueado
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    assert r.json()["detail"]["error"] == "human_code_locked"


def test_anuncio_no_console_e_limitado_a_um_a_cada_30s(capsys):
    _approve(); _approve(); _approve()
    assert capsys.readouterr().out.count("Código de aprovação humana") == 1


def test_codigo_certo_passa_o_portao_e_o_user_id_do_cliente_e_ignorado(monkeypatch):
    seen = {}

    def spy(req):
        seen["user_id"] = req.user_id
        from app.hitl.schemas import MergeResult
        return MergeResult(success=False, message="x", target_branch="master", error="SPY")
    monkeypatch.setattr(server.hitl_service, "approve_and_merge", spy)
    r = _approve(server.HUMAN_CODE.current(), user_id="evil-admin")
    assert r.status_code == 400 and r.json()["detail"]["error"] == "SPY"          # passou do portão: erro é do serviço
    assert seen["user_id"] == server.OPERATOR_ID != "evil-admin"                    # identidade decidida pelo servidor


def test_rejeitar_nao_exige_codigo():
    r = client.post("/api/hitl/reject", json={"proposal_id": "nao-existe", "user_id": "u", "reason": "x"})
    assert r.status_code in (200, 404, 422)                          # nunca 403 de código: rejeitar é o lado seguro


# ── MCP: confirmed_hitl do cliente não vale ────────────────────────────────────
@pytest.fixture
def ferramenta_destrutiva():
    server.mcp_catalog.register_server(McpServerConfig(id="srv-h", name="H", transport="stdio", command="python"))
    server.mcp_catalog.register_tool(McpToolDefinition(name="apaga_tudo", description="d", server_id="srv-h",
                                                       effect="destructive"))
    called = []
    server.mcp_client.register_tool_handler("apaga_tudo", lambda: called.append(1) or "feito")
    server.mcp_client.enabled = True
    yield called
    server.mcp_catalog._tools.pop("apaga_tudo", None)
    server.mcp_catalog._servers.pop("srv-h", None)
    server.mcp_client._handlers.pop("apaga_tudo", None)


def test_mcp_confirmed_hitl_true_do_cliente_e_ignorado(ferramenta_destrutiva):
    r = client.post("/api/mcp/execute", json={"tool_name": "apaga_tudo", "arguments": {}, "confirmed_hitl": True})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "human_code_required"
    assert ferramenta_destrutiva == []              # a ferramenta destrutiva NÃO rodou


def test_mcp_codigo_errado_nao_executa(ferramenta_destrutiva):
    r = client.post("/api/mcp/execute", json={"tool_name": "apaga_tudo", "arguments": {}, "human_code": "00000000"})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "human_code_invalid"
    assert ferramenta_destrutiva == []


def test_mcp_com_codigo_valido_executa(ferramenta_destrutiva):
    r = client.post("/api/mcp/execute", json={"tool_name": "apaga_tudo", "arguments": {},
                                              "human_code": server.HUMAN_CODE.current()})
    assert r.status_code == 200 and r.json()["success"] is True
    assert ferramenta_destrutiva == [1]


# ── CLI ────────────────────────────────────────────────────────────────────────
def test_comando_approval_code_imprime_o_mesmo_codigo_do_servidor():
    import subprocess
    import sys
    import os
    from pathlib import Path
    root = Path(server.__file__).resolve().parent.parent
    # mesmo HERA_DATA_DIR do processo de testes => mesmo segredo => mesmo código
    out = subprocess.run([sys.executable, "main.py", "approval-code"], cwd=root, capture_output=True, text=True,
                         env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=60).stdout
    assert server.HUMAN_CODE.current() in out or server.HUMAN_CODE._code_for(int(time.time() // 300) - 1) in out
