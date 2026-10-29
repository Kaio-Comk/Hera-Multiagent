"""Testes de orçamento diário e aderência ao modelo de fallback do TierRouter."""
import sqlite3
import time
import httpx
import pytest

from app.gateway.providers import GatewayProviderError
from app.gateway.schemas import ChatCompletionRequest, ChatMessage
from app.gateway.tier_router import TierRouter
from app.gateway.usage import UsageLedger, UsageStore
from app.gateway.factory import build_tier_router, parse_daily_budget, parse_sticky_minutes

try:
    from test_router_embutido import Fake
except ImportError:
    class Fake:
        def __init__(self, name, tier="cloud_cheap", local=False, models=("m",), error=None):
            self.name = name
            self.tier = tier
            self.is_local = local
            self.served_models = list(models)
            self.error = error
            self.seen = []
            self.default_model = models[0] if models else "m"

        def supports(self, kind: str) -> bool:
            return True

        async def chat_complete(self, request, client=None):
            self.seen.append(request.model)
            if self.error:
                raise self.error
            from app.gateway.schemas import ChatCompletionResponse, ChatChoice, ChatMessage, ChatUsage
            return ChatCompletionResponse(
                id=f"fake-{self.name}",
                object="chat.completion",
                created=int(time.time()),
                model=request.model or self.default_model,
                choices=[ChatChoice(index=0, message=ChatMessage(role="assistant", content=f"de {self.name}"), finish_reason="stop")],
                usage=ChatUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20)
            )


CLIENT = httpx.AsyncClient()


def req(model, text="oi", stream=False, user=None):
    return ChatCompletionRequest(model=model, stream=stream, user=user, messages=[ChatMessage(role="user", content=text)])


# --------------------------------------------------------------------------- Orçamento Diário
@pytest.mark.asyncio
async def test_sem_teto_nao_muda_nada(tmp_path):
    ledger = UsageLedger(tmp_path / "u.db")
    ledger.record("pago", "m", True, 200, cost=100.0)
    a = Fake("pago", tier="cloud_cheap", models=("m",))
    r = TierRouter(adapters=[a], daily_budget_usd=None)
    r.usage = ledger
    resp = await r.route_and_execute(req("m"), CLIENT)
    assert resp.choices[0].message.content == "de pago"


@pytest.mark.asyncio
async def test_teto_zero_barra_cloud_cheap_e_permite_free_e_local(tmp_path):
    ledger = UsageLedger(tmp_path / "u.db")
    cheap = Fake("cheap", tier="cloud_cheap", models=("m",))
    free = Fake("free", tier="cloud_free", models=("m",))
    local = Fake("local", tier="local", local=True, models=("m",))

    # Teto 0: cheap é barrado, escolhe free
    r1 = TierRouter(adapters=[cheap, free], daily_budget_usd=0.0)
    r1.usage = ledger
    resp1 = await r1.route_and_execute(req("m"), CLIENT)
    assert resp1.choices[0].message.content == "de free"

    # Teto 0: cheap é barrado, escolhe local
    r2 = TierRouter(adapters=[cheap, local], daily_budget_usd=0.0)
    r2.usage = ledger
    resp2 = await r2.route_and_execute(req("m"), CLIENT)
    assert resp2.choices[0].message.content == "de local"


@pytest.mark.asyncio
async def test_teto_1_com_gasto_05_permite_e_12_barra(tmp_path):
    ledger = UsageLedger(tmp_path / "u.db")
    ledger.record("cheap", "m", True, 200, cost=0.5)

    cheap = Fake("cheap", tier="cloud_cheap", models=("m",))
    free = Fake("free", tier="cloud_free", models=("m",))
    r = TierRouter(adapters=[cheap, free], daily_budget_usd=1.0)
    r.usage = ledger

    # Gasto de 0.5 < 1.0 -> cheap atende
    resp1 = await r.route_and_execute(req("m"), CLIENT)
    assert resp1.choices[0].message.content == "de cheap"

    # Adiciona gasto de 0.7 -> total de 1.2 >= 1.0 -> cheap é barrado, cai para free
    ledger.record("cheap", "m", True, 200, cost=0.7)
    resp2 = await r.route_and_execute(req("m"), CLIENT)
    assert resp2.choices[0].message.content == "de free"


@pytest.mark.asyncio
async def test_todos_barrados_pelo_teto_erro_402_com_mensagem(tmp_path):
    ledger = UsageLedger(tmp_path / "u.db")
    ledger.record("p1", "m", True, 200, cost=2.0)

    p1 = Fake("p1", tier="cloud_cheap", models=("m",))
    p2 = Fake("p2", tier="cloud_premium", models=("m",))
    r = TierRouter(adapters=[p1, p2], daily_budget_usd=1.0)
    r.usage = ledger

    with pytest.raises(GatewayProviderError) as exc_info:
        await r.route_and_execute(req("m"), CLIENT)

    assert exc_info.value.status_code == 402
    assert "Orcamento diario atingido e nenhum provedor gratuito ou local disponivel." in str(exc_info.value)


def test_today_cost_soma_so_hoje(tmp_path):
    ledger = UsageLedger(tmp_path / "u.db")
    now = time.time()

    # Registro de 2 dias atrás inserido direto no banco
    with ledger._connect() as db:
        db.execute(
            "INSERT INTO usage (ts, provider, model, ok, cost) VALUES (?, ?, ?, ?, ?)",
            (now - 86400 * 2, "antigo", "m", 1, 10.0),
        )

    # Registros de hoje
    ledger.record("p1", "m", True, 200, cost=1.5)
    ledger.record("p2", "m", True, 200, cost=2.5)

    # Soma de hoje: 1.5 + 2.5 = 4.0 (ignora os 10.0 de 2 dias atrás)
    assert ledger.today_cost() == 4.0


def test_factory_le_daily_budget_e_ignora_invalido():
    r1 = build_tier_router({"gateway": {"daily_budget_usd": 15.5}}, adapters=[])
    assert r1.daily_budget_usd == 15.5

    r2 = build_tier_router({"gateway": {"daily_budget_usd": 0}}, adapters=[])
    assert r2.daily_budget_usd == 0.0

    r_none = build_tier_router({"gateway": {"daily_budget_usd": None}}, adapters=[])
    assert r_none.daily_budget_usd is None

    # Negativo vira None
    r_neg = build_tier_router({"gateway": {"daily_budget_usd": -5.0}}, adapters=[])
    assert r_neg.daily_budget_usd is None

    # Inválido vira None
    r_inv = build_tier_router({"gateway": {"daily_budget_usd": "abc"}}, adapters=[])
    assert r_inv.daily_budget_usd is None

    r_bool = build_tier_router({"gateway": {"daily_budget_usd": True}}, adapters=[])
    assert r_bool.daily_budget_usd is None


# --------------------------------------------------------------------------- Aderência (Sticky Fallback)
@pytest.mark.asyncio
async def test_aderencia_falha_em_a_sucesso_em_b_proxima_vai_direto_a_b():
    clock = [1000.0]
    a = Fake("a", models=("m",), error=GatewayProviderError("erro em a", status_code=503, retryable=True))
    b = Fake("b", models=("m",))
    r = TierRouter(adapters=[a, b], sticky_minutes=15, clock=lambda: clock[0])

    # Primeira requisição: a falha (503 retentável), fallback para b tem sucesso
    resp1 = await r.route_and_execute(req("m"), CLIENT)
    assert resp1.choices[0].message.content == "de b"
    assert a.seen == ["m"]
    assert b.seen == ["m"]

    # Estado de aderência registrado
    status = r.sticky_status()
    assert "default" in status
    assert status["default"]["provedor"] == "b"
    assert status["default"]["segundos_restantes"] == 15 * 60

    # Próxima requisição com mesma chave: vai direto a B sem tentar A
    a.seen.clear()
    b.seen.clear()
    resp2 = await r.route_and_execute(req("m"), CLIENT)
    assert resp2.choices[0].message.content == "de b"
    assert a.seen == []
    assert b.seen == ["m"]


@pytest.mark.asyncio
async def test_aderencia_expira_com_relogio_falso():
    clock = [1000.0]
    a = Fake("a", models=("m",), error=GatewayProviderError("503", status_code=503, retryable=True))
    b = Fake("b", models=("m",))
    r = TierRouter(adapters=[a, b], sticky_minutes=10, clock=lambda: clock[0])

    await r.route_and_execute(req("m"), CLIENT)
    assert r.sticky_status()["default"]["provedor"] == "b"

    # Avança o relógio além de 10 min (600s)
    clock[0] += 601.0

    # Aderência expirou
    assert r.sticky_status() == {}

    # Próxima chamada volta a tentar A primeiro (que falha) e depois B
    a.seen.clear()
    b.seen.clear()
    await r.route_and_execute(req("m"), CLIENT)
    assert a.seen == ["m"]
    assert b.seen == ["m"]


@pytest.mark.asyncio
async def test_aderencia_chaves_user_independentes():
    clock = [1000.0]
    a = Fake("a", models=("m",), error=GatewayProviderError("503", status_code=503, retryable=True))
    b = Fake("b", models=("m",))
    r = TierRouter(adapters=[a, b], sticky_minutes=15, clock=lambda: clock[0])

    # Chamada de Alice: cai para B e fica aderente para 'alice'
    req_alice = ChatCompletionRequest(model="m", messages=[ChatMessage(role="user", content="oi")], user="alice")
    await r.route_and_execute(req_alice, CLIENT)

    assert "alice" in r.sticky_status()
    assert "bob" not in r.sticky_status()
    assert r.sticky_status()["alice"]["provedor"] == "b"

    # Chamada de Bob: não tem aderência, tenta A primeiro
    a.seen.clear()
    b.seen.clear()
    req_bob = ChatCompletionRequest(model="m", messages=[ChatMessage(role="user", content="oi")], user="bob")
    await r.route_and_execute(req_bob, CLIENT)
    assert a.seen == ["m"]
    assert b.seen == ["m"]

    # Alice vai direto a B
    a.seen.clear()
    b.seen.clear()
    await r.route_and_execute(req_alice, CLIENT)
    assert a.seen == []
    assert b.seen == ["m"]


@pytest.mark.asyncio
async def test_aderencia_b_pausado_ou_barrado_e_ignorado(tmp_path):
    clock = [1000.0]
    a = Fake("a", models=("m",))
    b = Fake("b", models=("m",))
    r = TierRouter(adapters=[a, b], sticky_minutes=15, clock=lambda: clock[0])

    r._set_sticky("default", "b")
    assert r.sticky_status()["default"]["provedor"] == "b"

    # Caso 1: B pausado pelo disjuntor
    r.guard.report_failure("b", "500")
    r.guard.report_failure("b", "500")
    r.guard.report_failure("b", "500")
    assert r.guard.is_paused("b")

    # Aderência a B é ignorada; A é tentado primeiro
    a.seen.clear()
    b.seen.clear()
    resp1 = await r.route_and_execute(req("m"), CLIENT)
    assert resp1.choices[0].message.content == "de a"
    assert a.seen == ["m"]
    assert b.seen == []

    # Reset do disjuntor de B
    r.guard.reset("b")
    assert not r.guard.is_paused("b")

    # Caso 2: B barrado por limite RPM
    r.guard.set_limits({"b": {"rpm": 1, "daily_requests": None, "daily_tokens": None}})
    r.guard._hits["b"].append(clock[0])

    a.seen.clear()
    b.seen.clear()
    resp2 = await r.route_and_execute(req("m"), CLIENT)
    assert resp2.choices[0].message.content == "de a"
    assert a.seen == ["m"]
    assert b.seen == []


@pytest.mark.asyncio
async def test_aderencia_sticky_minutes_zero_desliga():
    a = Fake("a", models=("m",), error=GatewayProviderError("503", status_code=503, retryable=True))
    b = Fake("b", models=("m",))
    r = TierRouter(adapters=[a, b], sticky_minutes=0)

    # Primeira chamada cai para B
    await r.route_and_execute(req("m"), CLIENT)
    assert r.sticky_status() == {}

    # Segunda chamada não vai direto para B; continua tentando A primeiro
    a.seen.clear()
    b.seen.clear()
    await r.route_and_execute(req("m"), CLIENT)
    assert a.seen == ["m"]
    assert b.seen == ["m"]


@pytest.mark.asyncio
async def test_dlp_continua_barrando_mesmo_com_aderencia():
    cloud_b = Fake("b", tier="cloud_cheap", models=("m",))
    local_p = Fake("local", tier="local", local=True, models=("m",))

    r = TierRouter(adapters=[cloud_b, local_p], sticky_minutes=15)
    r._set_sticky("default", "b")

    # Requisição forçando offline/local_only: DLP barra cloud_b apesar da aderência
    resp = await r.route_and_execute(req("m", text="cpf 123.456.789-00"), CLIENT, force_offline=True)
    assert resp.choices[0].message.content == "de local"
