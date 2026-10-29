"""Motor bloqueado por cota nao pode virar falha no Ollama desligado nem gasto do Claude.

Bug de 28/10/2026: um 429 do River (um provedor do hera_router) marcava o MOTOR inteiro como sem limite por 5 a 30 min;
os passos seguintes caiam no Ollama (desligado) e falhavam, e o substituto escolhido era o claude_code (plano do Kaio).
"""
import pytest

from app.router import TaskRouter
from models.agent import HealthStatus
from models.task import AgentTask


class Motor:
    """Motor falso: so o que o roteador usa."""

    def __init__(self, nome, saudavel=True, nuvem=False, modelo="padrao"):
        self._nome, self.saudavel, self.nuvem, self.default_model = nome, saudavel, nuvem, modelo

    @property
    def name(self):
        return self._nome

    def has_cloud(self):
        return self.nuvem

    async def health_check(self):
        return HealthStatus(healthy=self.saudavel)


def tarefa(tipo="docs", agente=None, modelo=None, texto="escreva um texto curto sobre filmes"):
    return AgentTask(id="t1", title="t", instruction=texto, task_type=tipo, assigned_agent=agente, assigned_model=modelo)


def roteador(*motores):
    return TaskRouter({m.name: m for m in motores})


async def test_substituto_nunca_e_o_claude_code_quando_ha_outro_motor():
    r = roteador(Motor("claude_code"), Motor("antigravity"), Motor("hera_router", nuvem=True))
    escolhido = r.fallback_adapter(tarefa("docs"), "hera_router")
    assert escolhido[0].name == "antigravity"


async def test_claude_code_entra_quando_e_o_unico():
    r = roteador(Motor("claude_code"))
    assert r.fallback_adapter(tarefa("docs"), "hera_router")[0].name == "claude_code"


async def test_ollama_vivo_vem_antes_do_claude_code_porque_e_gratis():
    r = roteador(Motor("claude_code"), Motor("ollama"))
    assert r.fallback_adapter(tarefa("docs"), "hera_router")[0].name == "ollama"


async def test_ollama_desligado_perde_para_o_claude_code_como_substituto():
    r = roteador(Motor("claude_code"), Motor("ollama", saudavel=False))
    await r.route(tarefa("docs"))           # a rota confere a saude do Ollama e guarda o resultado
    assert r.fallback_adapter(tarefa("docs"), "hera_router")[0].name == "claude_code"


async def test_motor_pedido_bloqueado_por_cota_vai_para_outro_motor_de_nuvem_e_nao_para_o_ollama_desligado():
    r = roteador(Motor("antigravity"), Motor("hera_router", nuvem=True), Motor("ollama", saudavel=False))
    r.quota.mark("hera_router", 1800, "rate limit do river")
    adaptador, _modelo, motivo = await r.route(tarefa("docs", agente="hera_router"))
    assert adaptador.name == "antigravity"
    assert "hera_router" in motivo


async def test_pesquisa_com_roteador_bloqueado_nao_cai_no_ollama_desligado():
    r = roteador(Motor("antigravity"), Motor("hera_router", nuvem=True), Motor("ollama", saudavel=False))
    r.quota.mark("hera_router", 1800, "429")
    adaptador, _m, _r = await r.route(tarefa("research"))
    assert adaptador.name == "antigravity"


async def test_padrao_pula_o_ollama_desligado():
    r = roteador(Motor("ollama", saudavel=False), Motor("antigravity"))
    adaptador, _m, _r = await r.route(tarefa("docs"))
    assert adaptador.name == "antigravity"


async def test_ollama_vivo_continua_sendo_o_padrao_local():
    r = roteador(Motor("ollama"), Motor("antigravity"))
    adaptador, _m, _r = await r.route(tarefa("docs"))
    assert adaptador.name == "ollama"


async def test_limite_de_um_provedor_nao_bloqueia_o_hera_router_inteiro():
    r = roteador(Motor("hera_router", nuvem=True), Motor("antigravity"))
    assert r.registrar_limite("hera_router", 1800, "River: 429 too many requests") is False
    assert not r.quota.blocked("hera_router")


async def test_limite_de_assinatura_continua_bloqueando_o_motor():
    r = roteador(Motor("claude_code"), Motor("antigravity"))
    assert r.registrar_limite("antigravity", 1800, "usage limit reached") is True
    assert r.quota.blocked("antigravity")


async def test_dados_sensiveis_continuam_exigindo_o_motor_local():
    r = roteador(Motor("ollama"), Motor("antigravity"))
    adaptador, _m, motivo = await r.route(tarefa("coding", agente="antigravity", texto="password=Abc12345678901234567890 do banco"))
    assert adaptador.name == "ollama" and "DLP" in motivo


async def test_dados_sensiveis_com_ollama_desligado_falham_fechado_com_mensagem_clara():
    r = roteador(Motor("ollama", saudavel=False), Motor("antigravity"))
    with pytest.raises(PermissionError, match="local"):
        await r.route(tarefa("coding", agente="antigravity", texto="password=Abc12345678901234567890 do banco"))
