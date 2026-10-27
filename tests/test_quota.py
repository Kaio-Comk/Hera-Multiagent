"""Motor por assinatura sem limite de uso: detectar, lembrar e refazer o passo em outro motor."""
import asyncio
from datetime import datetime

import pytest

from app.coordinator import Coordinator
from app.dag_scheduler import DAGScheduler, TaskDAG
from app.quota import QuotaTracker, is_quota_error, seconds_until_reset
from app.router import TaskRouter
from app.state_store import StateStore
from models.agent import RunHandle
from models.task import AgentResult, AgentTask, TaskStatus


def test_reconhece_mensagens_de_limite_de_uso():
    for msg in ("Claude AI usage limit reached|1760000000", "You've hit your limit · resets 3pm", "5-hour limit reached ∙ resets 11pm",
                "RESOURCE_EXHAUSTED: quota exceeded", "HTTP 429 Too Many Requests", "Your credit balance is too low"):
        assert is_quota_error(msg), msg
    for msg in ("SyntaxError em app.py", "teste falhou: assert 1 == 2", "", None):
        assert not is_quota_error(msg or ""), msg


def test_horario_de_volta_vem_da_mensagem():
    now = datetime(2026, 10, 13, 14, 0, 0)
    assert seconds_until_reset("limit reached, resets 3pm", now) == 3600
    assert seconds_until_reset("limit reached, resets 1pm", now) == 6 * 3600
    assert seconds_until_reset("try again in 2 hours", now) == 7200
    assert seconds_until_reset("limite atingido", now) == 30 * 60                       # sem horario: 30 min


def test_motor_esgotado_some_e_volta():
    q = QuotaTracker()
    q.mark("claude_code", 600, "limite")
    assert q.blocked("claude_code") and "claude_code" in q.snapshot() and not q.blocked("antigravity")
    q._until["claude_code"] = 0.0                                                         # o prazo passou
    assert not q.blocked("claude_code") and q.snapshot() == {}


class Fake:
    def __init__(self, name, fail_text=None):
        self._name, self.fail_text, self.calls, self.default_model = name, fail_text, 0, "m"

    @property
    def name(self):
        return self._name

    async def submit(self, task):
        return RunHandle(task_id=task.id)

    async def collect(self, handle):
        self.calls += 1
        if self.fail_text:
            return AgentResult(task_id=handle.task_id, status=TaskStatus.FAILED, summary="falhou", error_message=self.fail_text)
        return AgentResult(task_id=handle.task_id, status=TaskStatus.SUCCEEDED, summary="ok", output_text="feito por " + self._name)

    async def cancel(self, handle):
        return True


@pytest.mark.asyncio
async def test_passo_e_refeito_em_outro_motor_quando_o_primeiro_esgota(tmp_path):
    claude = Fake("claude_code", "Claude AI usage limit reached|1760000000")
    agy = Fake("antigravity")
    router = TaskRouter(adapters={"claude_code": claude, "antigravity": agy})
    sched = DAGScheduler(router=router, coordinator=Coordinator(store=StateStore(db_path=str(tmp_path / "s.db"))), max_concurrency=1)
    dag = TaskDAG()
    dag.add_task(AgentTask(id="t1", title="T", instruction="faz", assigned_agent="claude_code", task_type="coding"))
    results = await sched.execute_dag(dag)
    assert results["t1"].status == TaskStatus.SUCCEEDED and "antigravity" in results["t1"].output_text
    assert claude.calls == 1 and agy.calls == 1 and router.quota.blocked("claude_code")
    assert dag.tasks["t1"].assigned_agent == "antigravity"
    # o proximo passo ja nem tenta o Claude: o roteador o ignora enquanto estiver esgotado
    dag2 = TaskDAG()
    dag2.add_task(AgentTask(id="t2", title="T", instruction="faz", assigned_agent="claude_code", task_type="coding"))
    res2 = await sched.execute_dag(dag2)
    assert res2["t2"].status == TaskStatus.SUCCEEDED and claude.calls == 1


@pytest.mark.asyncio
async def test_sem_outro_motor_o_passo_falha_com_motivo_claro(tmp_path):
    claude = Fake("claude_code", "You've hit your limit · resets 3pm")
    router = TaskRouter(adapters={"claude_code": claude})
    sched = DAGScheduler(router=router, coordinator=Coordinator(store=StateStore(db_path=str(tmp_path / "s.db"))), max_concurrency=1)
    dag = TaskDAG()
    dag.add_task(AgentTask(id="t1", title="T", instruction="faz", assigned_agent="claude_code", task_type="coding"))
    res = (await sched.execute_dag(dag))["t1"]
    assert res.status == TaskStatus.FAILED and "limite de uso" in (res.error_message or "") and "nao ha outro motor" in res.error_message


@pytest.mark.asyncio
async def test_erro_comum_nao_dispara_o_desvio(tmp_path):
    claude = Fake("claude_code", "SyntaxError: invalid syntax")
    agy = Fake("antigravity")
    router = TaskRouter(adapters={"claude_code": claude, "antigravity": agy})
    sched = DAGScheduler(router=router, coordinator=Coordinator(store=StateStore(db_path=str(tmp_path / "s.db"))), max_concurrency=1)
    dag = TaskDAG()
    dag.add_task(AgentTask(id="t1", title="T", instruction="faz", assigned_agent="claude_code", task_type="coding"))
    res = (await sched.execute_dag(dag))["t1"]
    assert res.status == TaskStatus.FAILED and agy.calls == 0 and not router.quota.blocked("claude_code")


def test_passo_de_arquivos_nao_cai_no_ollama():
    router = TaskRouter(adapters={"claude_code": Fake("claude_code"), "ollama": Fake("ollama")})
    coding = AgentTask(id="a", title="a", instruction="x", task_type="coding")
    assert router.fallback_adapter(coding, "claude_code") is None
    research = AgentTask(id="b", title="b", instruction="x", task_type="research")
    assert router.fallback_adapter(research, "claude_code")[0].name == "ollama"


def test_bloqueado_parcial_conta_como_bloqueio():
    from app.dag_scheduler import DAGScheduler
    from types import SimpleNamespace as R
    sched = DAGScheduler.__new__(DAGScheduler)
    for texto in ("BLOQUEADO: sem acesso", "BLOQUEADO (parcial): faltou limpar", "**BLOQUEADO** nao deu"):
        assert sched._is_blocked(R(output_text=texto))
    assert not sched._is_blocked(R(output_text="Tudo certo, nada bloqueado."))

def test_parse_ratelimit_headers():
    from app.quota import parse_ratelimit_headers
    headers = {
        "anthropic-ratelimit-requests-remaining": "15",
        "anthropic-ratelimit-requests-limit": "50",
        "anthropic-ratelimit-tokens-remaining": "25000",
        "anthropic-ratelimit-tokens-limit": "100000",
        "retry-after": "45"
    }
    parsed = parse_ratelimit_headers(headers)
    assert parsed["remaining_requests"] == 15
    assert parsed["limit_requests"] == 50
    assert parsed["remaining_tokens"] == 25000
    assert parsed["limit_tokens"] == 100000
    assert parsed["reset_seconds"] == 45


def test_quota_tracker_predictive_health():
    q = QuotaTracker()
    assert q.get_health_status("claude_code") == "healthy"

    # 40% remanescente -> degraded
    q.record_metrics("claude_code", {
        "x-ratelimit-remaining-tokens": "40000",
        "x-ratelimit-limit-tokens": "100000"
    })
    assert q.get_health_status("claude_code") == "degraded"

    # 20% remanescente -> survival
    q.record_metrics("claude_code", {
        "x-ratelimit-remaining-tokens": "20000",
        "x-ratelimit-limit-tokens": "100000"
    })
    assert q.get_health_status("claude_code") == "survival"

