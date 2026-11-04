from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from app.telegram.facts_loader import get_support_facts, parse_markdown_base, sem_acento
from app.telegram.notifier import limpar_estilo
from app.telegram.customer_support import CustomerSupportService
from app.telegram.autofix_service import AutoFixService
from app.telegram.schemas import (
    CustomerMessageRequest,
    CustomerMessageResponse,
    SystemErrorAlert,
    AutoHealResponse,
)
from app.server import app



@pytest.fixture(autouse=True)
def mock_enviar_telegram(monkeypatch):
    mock = MagicMock(return_value=True)
    monkeypatch.setattr("app.telegram.customer_support.enviar_telegram_kaio", mock)
    monkeypatch.setattr("app.telegram.autofix_service.enviar_telegram_kaio", mock)
    monkeypatch.setattr("app.telegram.notifier.enviar_telegram_kaio", mock)
    return mock


def test_notifier_limpar_estilo():
    # Verifica remocao estrita de emojis e travessoes soltos
    texto_com_emoji = "Ola! 🚀 Segue o link do NetHub 🔥 — aproveite!"
    limpo = limpar_estilo(texto_com_emoji)
    assert "🚀" not in limpo
    assert "🔥" not in limpo
    assert "—" not in limpo
    assert "Ola! Segue o link do NetHub, aproveite!" == limpo


def test_facts_loader_markdown_parser():
    md = """## Fatos PT
- O bot oficial e @NetHub_Filme_Bot
- Plano completo por R$ 9,90

## Fatos ES
- El bot oficial es @NetHub_Peliculas_Bot

## Escalar
- pagamento, pix, estorno, reembolso

## Bug
- nao carrega, tela preta, travando, bug

## Tom
- Curto: no maximo 3 frases
- NUNCA use emojis
"""
    fatos, escalar, tom, bug = parse_markdown_base(md)
    assert len(fatos["pt"]) == 2
    assert len(fatos["es"]) == 1
    assert "pix" in escalar
    assert "tela preta" in bug
    assert any("emojis" in t for t in tom)


@pytest.mark.asyncio
async def test_customer_support_cumprimento_e_fechamento():
    support = CustomerSupportService()

    # Cumprimento
    req_oi = CustomerMessageRequest(chat_id=123, texto="Oi, bom dia!", edicao="br")
    res_oi = await support.processar_mensagem(req_oi)
    assert res_oi.status == "answered"
    assert res_oi.motivo == "cumprimento"
    assert "Oi!" in res_oi.resposta_cliente

    # Fechamento / agradecimento
    req_obg = CustomerMessageRequest(chat_id=123, texto="muito obrigado!", edicao="br")
    res_obg = await support.processar_mensagem(req_obg)
    assert res_obg.status == "answered"
    assert res_obg.motivo == "fechamento"
    assert "Por nada!" in res_obg.resposta_cliente


@pytest.mark.asyncio
async def test_customer_support_escalar_financeiro():
    support = CustomerSupportService()
    req_pag = CustomerMessageRequest(chat_id=123, texto="fiz o pix mas nao liberou meu plano", edicao="br")
    res_pag = await support.processar_mensagem(req_pag)

    assert res_pag.status == "escalated"
    assert res_pag.motivo == "pagamento"
    assert "comprovante" in res_pag.resposta_cliente.lower()


@pytest.mark.asyncio
async def test_customer_support_bug_dispara_autofix():
    mock_autofix = MagicMock()
    mock_autofix.agendar_autofix_cliente = AsyncMock(return_value="task-mock-1234")

    support = CustomerSupportService(autofix_service=mock_autofix)
    req_bug = CustomerMessageRequest(
        chat_id=456,
        nome="Teste Cliente",
        username="teste",
        texto="no player a tela fica preta e o video nao carrega",
        edicao="br",
    )
    res_bug = await support.processar_mensagem(req_bug)

    assert res_bug.status == "bug_investigating"
    assert res_bug.motivo == "bug"
    assert res_bug.task_id == "task-mock-1234"
    assert "investigação e correção automática" in res_bug.resposta_cliente
    mock_autofix.agendar_autofix_cliente.assert_awaited_once()


@pytest.mark.asyncio
async def test_autofix_deduplicacao_e_ruido_transitorio(monkeypatch):
    autofix = AutoFixService()
    monkeypatch.setattr(autofix, "_executar_autoheal_sistema_background", AsyncMock())

    # 1. Erro transitorio deve ser descartado
    alert_transitorio = SystemErrorAlert(
        servico="bot",
        mensagem="aiogram.dispatcher: Failed to fetch updates due to timeout",
    )
    status_t, msg_t = await autofix.agendar_autoheal_sistema(alert_transitorio)
    assert status_t == "ignored_transient"

    # 2. Erro real deve disparar auto-cura
    alert_real = SystemErrorAlert(
        servico="bot",
        arquivo="Bot/main.py",
        linha=100,
        mensagem="KeyError: 'user_id' in process_message",
        traceback="Traceback:\nKeyError: 'user_id'",
    )
    status_1, msg_1 = await autofix.agendar_autoheal_sistema(alert_real)
    assert status_1 == "started"

    # 3. Mesma assinatura em seguida deve ser deduplicada
    status_2, msg_2 = await autofix.agendar_autoheal_sistema(alert_real)
    assert status_2 == "deduplicated"


def test_api_endpoints_integration():
    client = TestClient(app, base_url="http://127.0.0.1:8000")

    # 1. Status da ponte Telegram
    resp_status = client.get("/api/telegram/status")
    assert resp_status.status_code == 200
    dados_status = resp_status.json()
    assert dados_status["status"] == "online"
    assert "active_tasks" in dados_status

    # 2. Endpoint de atendimento a clientes
    resp_cliente = client.post(
        "/api/telegram/customer-message",
        json={"chat_id": 999, "texto": "Ola, tudo bem?", "edicao": "br"},
    )
    assert resp_cliente.status_code == 200
    dados_cliente = resp_cliente.json()
    assert dados_cliente["status"] == "answered"
    assert dados_cliente["motivo"] == "cumprimento"

    # 3. Endpoint de erro de sistema
    resp_sys = client.post(
        "/api/telegram/system-error",
        json={
            "servico": "app",
            "edicao": "br",
            "nivel": "ERROR",
            "arquivo": "App/index.html",
            "linha": 42,
            "mensagem": "LineTooLong: header exceeds limit",
        },
    )
    assert resp_sys.status_code == 200
    dados_sys = resp_sys.json()
    assert dados_sys["status"] == "ignored_transient"


# ---------------------------------------------------------------- 04/11/2026: conta do cliente e "não sei" de verdade

from app.telegram.customer_support import NAO_SEI, fatos_da_conta  # noqa: E402


def test_fatos_da_conta_viram_frases_sem_dado_pessoal():
    ctx = {"plano": "pro", "ativo": True, "valido_ate": "03/12/2026", "tem_conta_site": True,
           "pedidos": [{"titulo": "Are You Sure?!", "status": "pendente"}, {"titulo": "Pecadores", "status": "adicionado"}]}
    fatos = fatos_da_conta(ctx, "pt")
    assert "Este cliente tem o plano pro ativo até 03/12/2026." in fatos
    assert any("Are You Sure?!" in f and "não há prazo" in f for f in fatos)
    assert any("Pecadores" in f and "já está no catálogo" in f for f in fatos)
    assert fatos_da_conta({}, "pt") == []


@pytest.mark.asyncio
async def test_duvida_usa_a_conta_no_prompt():
    vistos = []

    async def llm(mensagens):
        vistos.append(mensagens[0]["content"])
        return "Seu plano está ativo até 03/12/2026."

    support = CustomerSupportService(llm_caller=llm)
    req = CustomerMessageRequest(chat_id=1, texto="meu plano ainda vale para a tv?", edicao="br",
                                 contexto={"plano": "pro", "ativo": True, "valido_ate": "03/12/2026"})
    res = await support.processar_mensagem(req)
    assert res.motivo == "duvida" and "03/12/2026" in vistos[0]


@pytest.mark.asyncio
async def test_quando_a_ia_nao_sabe_o_motivo_e_nao_sei():
    async def llm(mensagens):
        return NAO_SEI["pt"]

    support = CustomerSupportService(llm_caller=llm)
    res = await support.processar_mensagem(CustomerMessageRequest(chat_id=1, texto="vocês têm app para geladeira?",
                                                                  edicao="br"))
    assert res.motivo == "nao_sei" and res.status == "escalated"


def test_contexto_e_opcional_no_pedido():
    assert CustomerMessageRequest(chat_id=1, texto="oi").contexto == {}


# ---------------------------------------------------------------- 04/11/2026: a auto-cura não edita biblioteca

from pathlib import Path as _P  # noqa: E402

from app.telegram.autofix_service import caminho_proibido_na_autocura, erro_so_em_biblioteca  # noqa: E402


def test_autocura_nao_grava_no_venv_nem_fora_do_projeto(tmp_path):
    ws = tmp_path / "NetHub BR"
    (ws / "Bot" / "gateway").mkdir(parents=True)
    assert not caminho_proibido_na_autocura(ws / "Bot" / "gateway" / "gw_server.py", ws)
    assert caminho_proibido_na_autocura(ws / ".venv" / "Lib" / "site-packages" / "telethon" / "network" / "x.py", ws)
    assert caminho_proibido_na_autocura(ws / ".git" / "config", ws)
    assert caminho_proibido_na_autocura(tmp_path / "outro" / "a.py", ws)
    assert caminho_proibido_na_autocura(_P("C:/Python312/Lib/asyncio/base_events.py"), ws)


def test_erro_so_em_biblioteca_nao_vira_autocura(tmp_path):
    ws = tmp_path / "NetHub BR"
    lib = str(ws / ".venv" / "Lib" / "site-packages" / "telethon" / "network" / "mtprotosender.py")
    tb_lib = f'Traceback (most recent call last):\n  File "{lib}", line 525, in _recv_loop\nRuntimeError: x'
    assert erro_so_em_biblioteca(lib, tb_lib, ws)
    tb_projeto = tb_lib + f'\n  File "{ws / "Bot" / "gateway" / "gw_server.py"}", line 10, in h'
    assert not erro_so_em_biblioteca(lib, tb_projeto, ws)
    assert not erro_so_em_biblioteca(str(ws / "Bot" / "main.py"), "", ws)


@pytest.mark.asyncio
async def test_ruido_da_queda_de_rede_e_ignorado():
    svc = AutoFixService()
    alerta = SystemErrorAlert(servico="gateway", edicao="br", mensagem="Unhandled error while receiving data",
                              traceback="RuntimeError: readexactly() called while another coroutine is already waiting for incoming data")
    status, _ = await svc.agendar_autoheal_sistema(alerta)
    assert status == "ignored_transient"
