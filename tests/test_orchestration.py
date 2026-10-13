"""Painel de motores (ligar/desligar, modelo) e plano multiagente (planejador -> demais motores)."""
import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from app import orchestration
from app.config_manager import ConfigManager
from app.router import TaskRouter
from models.agent import HealthStatus, RunHandle
from models.task import AgentResult, AgentTask, TaskStatus
from helpers import AUTH_HEADERS, TEST_BASE_URL


class FakeAdapter:
    """Motor de mentira: guarda o que recebeu e devolve um texto fixo."""

    def __init__(self, name, output="ok", models=("m1", "m2")):
        self.name = name
        self.output = output
        self.default_model = models[0]
        self.models = list(models)
        self.received = []

    async def health_check(self):
        return HealthStatus(healthy=True, version="fake", available_models=self.models)

    async def submit(self, task):
        self.received.append(task)
        return RunHandle(task_id=task.id)

    async def collect(self, handle):
        return AgentResult(task_id=handle.task_id, status=TaskStatus.SUCCEEDED, summary="feito", output_text=self.output)

    async def cancel(self, handle):
        return True


PLAN_JSON = json.dumps({"steps": [
    {"id": "s1", "title": "Pesquisar", "instruction": "Levante os requisitos", "type": "research", "engine": "worker", "depends_on": []},
    {"id": "s2", "title": "Escrever", "instruction": "Escreva o codigo", "type": "coding", "engine": "worker", "depends_on": ["s1"]},
]})


# --------------------------------------------------------------------------- parse_plan
def test_parse_plan_aceita_json_com_cerca_e_texto_em_volta():
    text = "Claro! Aqui esta:\n```json\n" + PLAN_JSON + "\n```\nBoa sorte."
    steps, warnings = orchestration.parse_plan(text, ["worker"], 8)
    assert [s["id"] for s in steps] == ["s1", "s2"]
    assert steps[1]["depends_on"] == ["s1"]
    assert warnings == []


def test_parse_plan_motor_desligado_vira_automatico_com_aviso():
    steps, warnings = orchestration.parse_plan(PLAN_JSON, ["outro"], 8)
    assert all(s["engine"] == "" for s in steps)
    assert any("nao esta ligado" in w for w in warnings)


def test_parse_plan_limita_numero_de_passos():
    many = json.dumps({"steps": [{"id": f"s{i}", "instruction": f"faca {i}"} for i in range(1, 30)]})
    steps, warnings = orchestration.parse_plan(many, ["worker"], 5)
    assert len(steps) == 5 and any("so os 5 primeiros" in w for w in warnings)


def test_parse_plan_ciclo_vira_sequencia():
    cyc = json.dumps({"steps": [
        {"id": "a", "instruction": "um", "depends_on": ["b"]},
        {"id": "b", "instruction": "dois", "depends_on": ["a"]},
    ]})
    steps, warnings = orchestration.parse_plan(cyc, ["worker"], 8)
    assert steps[0]["depends_on"] == [] and steps[1]["depends_on"] == ["a"]
    assert any("ciclo" in w for w in warnings)


def test_parse_plan_limpa_texto_e_ignora_dependencia_inexistente():
    raw = json.dumps({"steps": [{"id": "s1", "title": "T\x00\x1b", "instruction": "faz\x00 isso", "depends_on": ["fantasma"]}]})
    steps, warnings = orchestration.parse_plan(raw, ["worker"], 8)
    assert "\x00" not in steps[0]["instruction"] and "\x1b" not in steps[0]["title"]
    assert steps[0]["depends_on"] == [] and any("dependencia invalida" in w for w in warnings)


def test_parse_plan_recusa_lixo_e_sem_motor():
    with pytest.raises(orchestration.PlanError):
        orchestration.parse_plan("nao sou json", ["worker"], 8)
    with pytest.raises(orchestration.PlanError):
        orchestration.parse_plan(PLAN_JSON, [], 8)
    with pytest.raises(orchestration.PlanError):
        orchestration.parse_plan('{"steps": []}', ["worker"], 8)


# --------------------------------------------------------------------------- roteador
@pytest.mark.asyncio
async def test_roteador_nao_usa_motor_desligado():
    router = TaskRouter(adapters={"ollama": FakeAdapter("ollama"), "claude_code": FakeAdapter("claude_code")})
    router.disabled = {"claude_code"}
    task = AgentTask(id="t", title="x", instruction="faz", task_type="coding", assigned_agent="claude_code")
    adapter, _, _ = await router.route(task)
    assert adapter.name == "ollama"


@pytest.mark.asyncio
async def test_roteador_usa_modelo_escolhido_no_painel():
    router = TaskRouter(adapters={"ollama": FakeAdapter("ollama")})
    router.default_models = {"ollama": "llama3.2:3b"}
    _, model, _ = await router.route(AgentTask(id="t", title="x", instruction="faz", task_type="coding"))
    assert model == "llama3.2:3b"


@pytest.mark.asyncio
async def test_roteador_sem_motor_ligado_falha_fechado():
    router = TaskRouter(adapters={"ollama": FakeAdapter("ollama")})
    router.disabled = {"ollama"}
    with pytest.raises(PermissionError):
        await router.route(AgentTask(id="t", title="x", instruction="faz"))


@pytest.mark.asyncio
async def test_ollama_desligado_nao_deixa_segredo_ir_para_outro_motor():
    router = TaskRouter(adapters={"ollama": FakeAdapter("ollama"), "claude_code": FakeAdapter("claude_code")})
    router.disabled = {"ollama"}
    secret = "sk-" + "abcdef0123456789" * 2
    with pytest.raises(PermissionError):
        await router.route(AgentTask(id="t", title="x", instruction=f"use a chave {secret}", assigned_agent="claude_code"))


# --------------------------------------------------------------------------- API
@pytest.fixture
def api(tmp_path, monkeypatch):
    planner = FakeAdapter("planner", output=PLAN_JSON)
    worker = FakeAdapter("worker", output="resultado do worker")
    fakes = {"planner": planner, "worker": worker}
    mgr = ConfigManager(config_path=str(tmp_path / "config.yaml"))
    monkeypatch.setattr(server, "config_mgr", mgr)
    monkeypatch.setattr(server, "adapters", fakes)
    monkeypatch.setattr(server.router, "adapters", fakes)
    monkeypatch.setattr(server.router, "disabled", set())
    monkeypatch.setattr(server.router, "default_models", {})
    monkeypatch.setattr(server.router, "default_offline", False)
    monkeypatch.setattr(server, "ENGINE_ADAPTER_NAMES", {"planner": "planner", "worker": "worker"})
    mgr.save_config({**mgr.load_config(), "orchestration": {"planner": {"engine": "planner", "model": ""}, "max_steps": 8}})
    return planner, worker


def client():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS)


@pytest.mark.asyncio
async def test_api_ligar_desligar_motor_e_nao_mexe_no_modo_offline(api, monkeypatch):
    monkeypatch.setattr(server.router, "default_offline", True)
    cfg = server.config_mgr.load_config()
    cfg["general"]["default_offline"] = True            # o que esta no disco e o que vale
    server.config_mgr.save_config(cfg)
    async with client() as c:
        r = await c.post("/api/config", json={"general": {}, "engines": {"antigravity": {"enabled": False}}})
        assert r.status_code == 200
        assert server.router.default_offline is True        # `general` vazio nao pode desligar o modo offline
        health = (await c.get("/api/health")).json()["engines"]
        assert health["worker"]["enabled"] is True and health["worker"]["available_models"] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_api_motor_desligado_some_dos_executores(api):
    _, worker = api
    server.router.disabled = {"worker"}
    async with client() as c:
        info = (await c.get("/api/orchestration")).json()
        assert info["active_engines"] == ["planner"]
        r = await c.post("/api/orchestration/plan", json={"goal": "fazer um site", "executors": ["worker"]})
        assert r.status_code == 400 and r.json()["detail"]["error"] == "no_executor"


@pytest.mark.asyncio
async def test_api_escolher_planejador_valida_o_motor(api):
    async with client() as c:
        ok = await c.post("/api/orchestration/planner", json={"engine": "worker", "model": "m2"})
        assert ok.status_code == 200
        assert (await c.get("/api/orchestration")).json()["planner"] == {"engine": "worker", "model": "m2"}
        assert (await c.post("/api/orchestration/planner", json={"engine": "inexistente"})).status_code == 400
        server.router.disabled = {"planner"}
        assert (await c.post("/api/orchestration/planner", json={"engine": "planner"})).status_code == 400


@pytest.mark.asyncio
async def test_api_gerar_plano_usa_o_planejador_e_nao_executa_nada(api):
    planner, worker = api
    async with client() as c:
        r = await c.post("/api/orchestration/plan", json={"goal": "criar uma API de login"})
    data = r.json()
    assert r.status_code == 200
    assert data["planner"]["engine"] == "planner" and data["planner"]["forced_local"] is False
    assert [s["id"] for s in data["steps"]] == ["s1", "s2"]
    assert len(planner.received) == 1 and "criar uma API de login" in planner.received[0].instruction
    assert worker.received == []


@pytest.mark.asyncio
async def test_api_plano_com_json_invalido_cai_em_um_passo(api):
    planner, _ = api
    planner.output = "desculpe, nao consegui"
    async with client() as c:
        data = (await c.post("/api/orchestration/plan", json={"goal": "fazer coisa"})).json()
    assert len(data["steps"]) == 1 and data["steps"][0]["instruction"] == "fazer coisa"
    assert any("indisponivel" in w for w in data["warnings"])


@pytest.mark.asyncio
async def test_api_modo_offline_forca_planejador_local(api, monkeypatch):
    fakes = {"ollama": FakeAdapter("ollama", output=PLAN_JSON), "planner": FakeAdapter("planner", output="nao deveria ser chamado")}
    monkeypatch.setattr(server, "adapters", fakes)
    monkeypatch.setattr(server.router, "adapters", fakes)
    monkeypatch.setattr(server.router, "default_offline", True)
    async with client() as c:
        data = (await c.post("/api/orchestration/plan", json={"goal": "fazer coisa"})).json()
    assert data["planner"]["engine"] == "ollama" and data["planner"]["forced_local"] is True
    assert fakes["planner"].received == []
    assert any("nao pode ver" in w for w in data["warnings"])


@pytest.mark.asyncio
async def test_api_executar_plano_distribui_aos_motores(api):
    _, worker = api
    steps = [
        {"id": "s1", "title": "Pesquisar", "instruction": "Levante os requisitos", "type": "research", "engine": "worker", "depends_on": []},
        {"id": "s2", "title": "Escrever", "instruction": "Escreva o codigo", "type": "coding", "engine": "worker", "depends_on": ["s1"]},
    ]
    async with client() as c:
        r = await c.post("/api/orchestration/execute", json={"goal": "x", "steps": steps})
    assert r.status_code == 200 and r.json()["run_id"].startswith("run-")
    for _ in range(50):                                   # a execucao roda em segundo plano
        if len(worker.received) == 2:
            break
        await asyncio.sleep(0.1)
    assert [t.instruction for t in worker.received] == ["Levante os requisitos", "Escreva o codigo"]


@pytest.mark.asyncio
async def test_api_executar_plano_rejeita_corpo_malformado(api):
    async with client() as c:
        assert (await c.post("/api/orchestration/execute", json={"steps": []})).status_code == 422
        too_many = [{"instruction": f"x{i}"} for i in range(40)]
        assert (await c.post("/api/orchestration/execute", json={"steps": too_many})).status_code == 422
        huge = [{"instruction": "x" * 10000}]
        assert (await c.post("/api/orchestration/execute", json={"steps": huge})).status_code == 422
