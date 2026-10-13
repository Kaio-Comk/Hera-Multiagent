"""O planejador escolhe motor E modelo de cada passo, a partir de um catalogo (o que cada um faz bem, forca do modelo)."""
import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from app import orchestration
from app.config_manager import ConfigManager
from app.engine_profiles import FORTE, EQUILIBRADO, LEVE, build_catalog, catalog_text, classify_model, pick_model
from models.agent import HealthStatus
from helpers import AUTH_HEADERS, TEST_BASE_URL
from test_orchestration import FakeAdapter


@pytest.mark.parametrize("engine,model,expected", [
    ("claude_code", "haiku", LEVE), ("claude_code", "sonnet", EQUILIBRADO), ("claude_code", "opus", FORTE),
    ("claude_code", "fable", FORTE), ("claude_code", "claude-haiku-5-5", LEVE), ("claude_code", "claude-opus-5-5", FORTE),
    ("antigravity", "gemini-3.8-flash-low", LEVE), ("antigravity", "gemini-3.8-flash-high", EQUILIBRADO),
    ("antigravity", "claude-opus-5-5-high", FORTE), ("antigravity", "claude-sonnet-5-5-medium", EQUILIBRADO),
    ("antigravity", "gpt-oss-120b-medium", EQUILIBRADO),
    ("ollama", "qwen2.5-coder:1.5b", LEVE), ("ollama", "qwen3.5:4b", LEVE), ("ollama", "llama3.1:8b", EQUILIBRADO),
    ("ollama", "llama3.3:70b", FORTE), ("ollama", "modelo-sem-tamanho", LEVE),
    ("hera_router", "llama-3.1-8b-instant", LEVE), ("hera_router", "deepseek-reasoner", FORTE),
    ("hera_router", "deepseek-chat", EQUILIBRADO), ("hera_router", "default", EQUILIBRADO), ("hera_router", "", EQUILIBRADO),
])
def test_classificacao_de_modelos(engine, model, expected):
    assert classify_model(engine, model) == expected


def sample_catalog():
    return build_catalog({
        "ollama": {"models": ["qwen3.5:4b"], "default": "qwen3.5:4b"},
        "claude_code": {"models": ["haiku", "sonnet", "opus"], "default": "sonnet"},
        "hera_router": {"models": ["default", "deepseek-chat"], "default": "default"},
    })


def test_catalogo_descreve_motores_e_modelos():
    cat = {c["engine"]: c for c in sample_catalog()}
    assert cat["claude_code"]["files"] is True and cat["ollama"]["files"] is False and cat["ollama"]["local"] is True
    assert [m["id"] for m in cat["hera_router"]["models"]] == ["deepseek-chat"]            # "default" nao e um modelo de verdade
    text = catalog_text(sample_catalog())
    assert 'MOTOR "claude_code"' in text and "opus (forte)" in text and "haiku (leve)" in text
    assert "acesso a arquivos/terminal: SIM" in text and "acesso a arquivos/terminal: NAO" in text and "custo: gratis" in text


def test_pick_model_pela_forca():
    cat = sample_catalog()
    assert pick_model(cat, "claude_code", "leve") == "haiku" and pick_model(cat, "claude_code", "forte") == "opus"
    assert pick_model(cat, "inexistente", "forte") is None


def test_prompt_do_planejador_traz_o_catalogo_e_as_regras():
    prompt = orchestration.build_planning_prompt("analisar logs", sample_catalog(), 8)
    for needle in ("MOTORES E MODELOS DISPONIVEIS", 'MOTOR "ollama"', "haiku (leve)", "COMO ESCOLHER", "NAO coloque tudo no mesmo motor",
                   "Nao gaste modelo forte em tarefa simples", "OBJETIVO:\nanalisar logs", '"model"', '"reason"'):
        assert needle in prompt, needle


# --------------------------------------------------------------------------- validacao dos passos
MODELS = {"claude_code": ["haiku", "sonnet", "opus"], "ollama": ["qwen3.5:4b"]}


def steps(*items):
    return json.dumps({"steps": [{"id": f"s{i}", "instruction": "faz", **it} for i, it in enumerate(items, 1)]})


def test_modelo_valido_e_motivo_sao_mantidos():
    out, warnings = orchestration.parse_plan(steps({"engine": "claude_code", "model": "opus", "reason": "bug sutil\x00"}),
                                             ["claude_code", "ollama"], 8, MODELS)
    assert out[0]["model"] == "opus" and out[0]["reason"] == "bug sutil" and warnings == []


def test_modelo_que_o_motor_nao_tem_vira_padrao_com_aviso():
    out, warnings = orchestration.parse_plan(steps({"engine": "claude_code", "model": "gpt-9000"}), ["claude_code"], 8, MODELS)
    assert out[0]["model"] == "" and any("gpt-9000" in w for w in warnings)


def test_modelo_sem_motor_ou_perigoso_e_descartado():
    out, _ = orchestration.parse_plan(steps({"engine": "", "model": "opus"}, {"engine": "claude_code", "model": "--dangerously-skip-permissions"},
                                            {"engine": "claude_code", "model": "a; calc"}), ["claude_code"], 8, MODELS)
    assert [s["model"] for s in out] == ["", "", ""]


def test_sem_lista_de_modelos_do_motor_aceita_nome_seguro():
    out, _ = orchestration.parse_plan(steps({"engine": "ollama", "model": "qualquer:7b"}), ["ollama"], 8, {"ollama": []})
    assert out[0]["model"] == "qualquer:7b"


def test_dag_leva_o_modelo_para_a_tarefa():
    out, _ = orchestration.parse_plan(steps({"engine": "claude_code", "model": "haiku"}), ["claude_code"], 8, MODELS)
    dag = orchestration.build_dag(out, "run-x", "")
    assert next(iter(dag.tasks.values())).assigned_model == "haiku"


# --------------------------------------------------------------------------- API
class Engine(FakeAdapter):
    def __init__(self, name, models, healthy=True, output="ok"):
        super().__init__(name, output=output, models=tuple(models))
        self.healthy = healthy

    async def health_check(self):
        return HealthStatus(healthy=self.healthy, version="x", available_models=self.models if self.healthy else [],
                            error_message=None if self.healthy else "fora do ar")


@pytest.fixture
def api(tmp_path, monkeypatch):
    planner = Engine("claude_code", ["haiku", "sonnet", "opus"])
    local = Engine("ollama", ["qwen3.5:4b"], output="resposta local")
    down = Engine("antigravity", ["gemini-3.8-flash-low"], healthy=False)
    fakes = {"claude_code": planner, "ollama": local, "antigravity": down}
    mgr = ConfigManager(config_path=str(tmp_path / "config.yaml"))
    monkeypatch.setattr(server, "config_mgr", mgr)
    monkeypatch.setattr(server, "adapters", fakes)
    monkeypatch.setattr(server.router, "adapters", fakes)
    monkeypatch.setattr(server.router, "disabled", set())
    monkeypatch.setattr(server.router, "default_models", {})
    monkeypatch.setattr(server.router, "default_offline", False)
    mgr.save_config({**mgr.load_config(), "orchestration": {"planner": {"engine": "claude_code", "model": "sonnet"}, "max_steps": 8}})
    return planner, local, down


def client():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS)


PLAN = steps(
    {"title": "Mapear", "engine": "claude_code", "model": "haiku", "reason": "ler arquivos, tarefa simples", "type": "research"},
    {"title": "Resumir", "engine": "ollama", "model": "qwen3.5:4b", "reason": "so texto, local e gratis", "type": "summary", "depends_on": ["s1"]},
    {"title": "Revisar", "engine": "claude_code", "model": "opus", "reason": "revisao critica", "type": "review", "depends_on": ["s2"]},
)


@pytest.mark.asyncio
async def test_api_plano_traz_catalogo_modelos_e_so_motores_no_ar(api):
    planner, local, down = api
    planner.output = PLAN
    async with client() as c:
        data = (await c.post("/api/orchestration/plan", json={"goal": "analisar o projeto"})).json()
    assert data["executors"] == ["claude_code", "ollama"]                              # antigravity esta fora do ar
    assert any("antigravity" in w and "fora do ar" in w for w in data["warnings"])
    assert [(s["engine"], s["model"]) for s in data["steps"]] == [("claude_code", "haiku"), ("ollama", "qwen3.5:4b"), ("claude_code", "opus")]
    assert data["steps"][0]["reason"].startswith("ler arquivos")
    cat = {x["engine"]: x for x in data["catalog"]}
    assert {m["id"]: m["strength"] for m in cat["claude_code"]["models"]} == {"haiku": "leve", "sonnet": "equilibrado", "opus": "forte"}
    sent = planner.received[0].instruction                                              # o que o planejador recebeu
    assert 'MOTOR "ollama"' in sent and "opus (forte)" in sent and "antigravity" not in sent


@pytest.mark.asyncio
async def test_api_avisa_quando_o_planejador_poe_tudo_no_mesmo_motor(api):
    planner, _, _ = api
    planner.output = steps(*[{"engine": "claude_code", "model": "sonnet"} for _ in range(3)])
    async with client() as c:
        data = (await c.post("/api/orchestration/plan", json={"goal": "fazer algo simples"})).json()
    assert any("Todos os passos ficaram" in w for w in data["warnings"])


@pytest.mark.asyncio
async def test_api_modelo_inventado_pelo_planejador_e_descartado(api):
    planner, _, _ = api
    planner.output = steps({"engine": "claude_code", "model": "super-modelo-9"})
    async with client() as c:
        data = (await c.post("/api/orchestration/plan", json={"goal": "fazer algo simples"})).json()
    assert data["steps"][0]["model"] == "" and any("super-modelo-9" in w for w in data["warnings"])


@pytest.mark.asyncio
async def test_api_sem_nenhum_motor_no_ar_recusa(api):
    planner, local, _ = api
    planner.healthy = local.healthy = False
    async with client() as c:
        r = await c.post("/api/orchestration/plan", json={"goal": "fazer algo simples"})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "no_executor"


@pytest.mark.asyncio
async def test_api_executar_envia_o_modelo_escolhido_para_cada_motor(api):
    planner, local, _ = api
    planner.default_model = "sonnet"
    body = {"goal": "x", "steps": [
        {"id": "s1", "title": "Mapear", "instruction": "mapeia", "engine": "claude_code", "model": "haiku"},
        {"id": "s2", "title": "Resumir", "instruction": "resume", "engine": "ollama", "model": "qwen3.5:4b", "depends_on": ["s1"]},
        {"id": "s3", "title": "Inventado", "instruction": "revisa", "engine": "claude_code", "model": "modelo-que-nao-existe", "depends_on": ["s2"]}]}
    async with client() as c:
        r = await c.post("/api/orchestration/execute", json=body)
    assert r.status_code == 200 and any("modelo-que-nao-existe" in w for w in r.json()["warnings"])
    for _ in range(60):
        if len(planner.received) >= 2 and len(local.received) >= 1:
            break
        await asyncio.sleep(0.1)
    assert [t.assigned_model for t in planner.received] == ["haiku", "sonnet"]           # o inventado virou o padrao do motor
    assert local.received[0].assigned_model == "qwen3.5:4b"


# --------------------------------------------------------------------------- modelos que nao cabem na GPU / falhas visiveis
from app.engine_profiles import filter_ollama_models  # noqa: E402
from models.agent import RunHandle  # noqa: E402
from models.task import AgentResult, TaskStatus  # noqa: E402


def test_filtra_modelos_do_ollama_que_nao_cabem_na_gpu():
    sizes = {"qwen3.5:4b": 3_324_173_934, "qwen2.5-coder:1.5b": 986_000_000, "sem-tamanho": 0}
    fits, skipped = filter_ollama_models(list(sizes), sizes, 4096)               # GPU de 4 GB: cabem ate ~3,0 GB
    assert fits == ["qwen2.5-coder:1.5b", "sem-tamanho"] and len(skipped) == 1 and "qwen3.5:4b" in skipped[0] and "4 GB" in skipped[0]
    assert filter_ollama_models(list(sizes), sizes, None) == (list(sizes), [])  # sem GPU conhecida: nao filtra nada
    assert filter_ollama_models(list(sizes), sizes, 24576)[1] == []             # GPU grande: tudo cabe


def test_catalogo_avisa_o_planejador_do_que_nao_usar():
    cat = build_catalog({"ollama": {"models": ["qwen2.5-coder:1.5b"], "notes": ["qwen3.5:4b (3.3 GB)"], "default": ""}})
    assert "ATENCAO: nao use qwen3.5:4b (3.3 GB)" in catalog_text(cat)


@pytest.mark.asyncio
async def test_plano_nao_oferece_modelo_que_nao_cabe_na_gpu(api, monkeypatch):
    planner, local, _ = api
    local.models = ["qwen3.5:4b", "qwen2.5-coder:1.5b"]
    async def sizes():
        return {"qwen3.5:4b": 3_324_173_934, "qwen2.5-coder:1.5b": 986_000_000}
    monkeypatch.setattr(server, "_ollama_sizes", sizes)
    monkeypatch.setattr(server, "_gpu_vram_mb", lambda: 4096)
    planner.output = steps({"engine": "ollama", "model": "qwen3.5:4b", "title": "Resumir"})
    async with client() as c:
        data = (await c.post("/api/orchestration/plan", json={"goal": "resumir o projeto"})).json()
    cat = {x["engine"]: x for x in data["catalog"]}
    assert [m["id"] for m in cat["ollama"]["models"]] == ["qwen2.5-coder:1.5b"]
    assert any("nao caber na GPU" in w and "qwen3.5:4b" in w for w in data["warnings"])
    assert data["steps"][0]["model"] == ""                                       # o planejador escolheu o pesado: descartado
    assert "ATENCAO: nao use qwen3.5:4b" in planner.received[0].instruction


class FailingEngine(Engine):
    async def collect(self, handle):
        return AgentResult(task_id=handle.task_id, status=TaskStatus.FAILED, summary="Exceção no motor",
                           error_message="unable to allocate CUDA_Host buffer; chave sk-" + "abcdef0123456789" * 2)


@pytest.mark.asyncio
async def test_passo_que_falha_mostra_o_motivo_e_fica_no_log_sem_segredo(api, monkeypatch):
    import logging
    planner, local, _ = api
    broken = FailingEngine("ollama", ["qwen2.5-coder:1.5b"])
    fakes = {"claude_code": planner, "ollama": broken}
    monkeypatch.setattr(server, "adapters", fakes)
    monkeypatch.setattr(server.router, "adapters", fakes)
    events = []
    monkeypatch.setattr(server.ws_manager, "broadcast", lambda payload: _record(events, payload))
    body = {"goal": "x", "steps": [{"id": "s1", "title": "Resumir", "instruction": "resume", "engine": "ollama", "model": "qwen2.5-coder:1.5b"}]}
    async with client() as c:
        assert (await c.post("/api/orchestration/execute", json=body)).status_code == 200
    for _ in range(80):
        if any(e.get("type") == "orchestration_finished" for e in events):
            break
        await asyncio.sleep(0.1)
    done = next(e for e in events if e.get("type") == "orchestration_finished")
    step = done["steps"][0]
    assert step["status"] == "FAILED" and "CUDA_Host" in step["error"] and "abcdef0123456789" not in step["error"]
    for handler in logging.getLogger("hera").handlers:
        handler.flush()
    log = server.LOG_FILE.read_text(encoding="utf-8")
    assert "status=FAILED" in log and "CUDA_Host" in log and "abcdef0123456789" not in log
    with server.store._get_connection() as db:
        row = db.execute("SELECT message FROM events WHERE event_type='TASK_FAILED' AND task_id LIKE 'run-%' ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None and "CUDA_Host" in row[0] and "abcdef0123456789" not in row[0]


async def _record(events, payload):
    events.append(payload)
