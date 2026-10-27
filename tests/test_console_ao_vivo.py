"""Console ao vivo: barramento de eventos, canal de progresso dos motores, leitura do stream-json dos CLIs, escalonador e API."""
import asyncio
import json
import sys

import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from adapters import progress
from adapters.cli_stream import AntigravityStream, ClaudeStream, run_streaming
from app.console_bus import MAX_TEXT, ConsoleBus
from app.dag_scheduler import DAGScheduler, TaskDAG
from app.router import TaskRouter
from models.agent import HealthStatus, RunHandle
from models.task import AgentResult, AgentTask, TaskStatus
from helpers import AUTH_HEADERS, TEST_BASE_URL


# --------------------------------------------------------------------------- barramento
def test_eventos_em_ordem_com_segredo_mascarado_e_nul_removido(tmp_path):
    bus = ConsoleBus(tmp_path / "runs")
    secret = "sk-" + "abcdef0123456789" * 2
    bus.emit("text", f"minha chave {secret}", run="run-1", step="s1")
    bus.emit("tool_result", "P\x00o\x00w\x00e\x00r", run="run-1", step="s1")
    events = bus.events()
    assert [e["seq"] for e in events] == [1, 2]
    assert secret not in events[0]["text"] and "abcdef0123456789" not in events[0]["text"]
    assert events[1]["text"] == "Power"
    assert bus.events(after=1)[0]["seq"] == 2 and bus.events(run="outra") == []


def test_texto_enorme_e_cortado_e_o_arquivo_tem_limite(tmp_path, monkeypatch):
    import app.console_bus as cb
    bus = ConsoleBus(tmp_path / "runs")
    assert len(bus.emit("text", "x" * (MAX_TEXT * 3), run="run-2", step="s1")["text"]) == MAX_TEXT
    monkeypatch.setattr(cb, "MAX_RUN_BYTES", 2000)
    for i in range(50):
        bus.emit("text", "y" * 100, run="run-3", step="s1")
    assert (tmp_path / "runs" / "run-3.jsonl").stat().st_size <= 2000                # nao enche o disco
    assert len(bus.events(run="run-3")) == 50                                         # ao vivo continua inteiro


def test_execucao_e_gravada_e_reaberta_depois(tmp_path):
    bus = ConsoleBus(tmp_path / "runs")
    bus.emit("plan_start", "meta", run="run-4", title="Meu plano", total=2)
    bus.emit("step_start", "", run="run-4", step="s1", title="A", engine="ollama")
    bus.emit("plan_end", "1 de 2", run="run-4", status="problems", done=1, total=2)
    reopened = ConsoleBus(tmp_path / "runs")                                          # "reiniciou o Hera"
    runs = reopened.list_runs()
    assert runs[0]["id"] == "run-4" and runs[0]["title"] == "Meu plano" and runs[0]["status"] == "problems" and runs[0]["steps"] == 2
    assert [e["kind"] for e in reopened.read_run("run-4")] == ["plan_start", "step_start", "plan_end"]


@pytest.mark.parametrize("bad", ["../segredo", "..\\x", "a/b", "run 1", "x" * 200, ""])
def test_id_de_execucao_malicioso_nao_le_nem_escreve_fora(tmp_path, bad):
    bus = ConsoleBus(tmp_path / "runs")
    assert bus.read_run(bad) == []
    bus.emit("info", "oi", run=bad)
    assert not list(tmp_path.rglob("segredo*")) and not any(p for p in (tmp_path / "runs").glob("*") if p.name.startswith(".."))


def test_so_guarda_as_ultimas_execucoes(tmp_path, monkeypatch):
    import app.console_bus as cb
    monkeypatch.setattr(cb, "KEEP_RUNS", 3)
    bus = ConsoleBus(tmp_path / "runs")
    for i in range(6):
        bus.emit("plan_start", "", run=f"run-{i}", title=str(i), total=1)
    assert len(list((tmp_path / "runs").glob("*.jsonl"))) <= 4


@pytest.mark.asyncio
async def test_envia_pela_tela_via_websocket(tmp_path):
    sent = []

    async def fake_ws(payload):
        sent.append(payload)

    bus = ConsoleBus(tmp_path / "runs", broadcast=fake_ws)
    bus.emit("text", "ola", run="run-5", step="s1")
    await asyncio.sleep(0.05)
    assert sent and sent[0]["type"] == "console" and sent[0]["text"] == "ola" and sent[0]["seq"] == 1


# --------------------------------------------------------------------------- canal de progresso
def test_sem_canal_ligado_say_nao_faz_nada():
    progress.say("text", "ninguem ouve")                                              # nao levanta


@pytest.mark.asyncio
async def test_passos_concorrentes_nao_misturam_os_eventos():
    got = {"a": [], "b": []}

    async def worker(name, delay):
        token = progress.bind(lambda kind, text="", **m: got[name].append(text))
        try:
            await asyncio.sleep(delay)
            progress.say("text", f"fala de {name}")
            await asyncio.sleep(delay)
            progress.say("text", f"fim de {name}")
        finally:
            progress.unbind(token)

    await asyncio.gather(worker("a", 0.02), worker("b", 0.03))
    assert got == {"a": ["fala de a", "fim de a"], "b": ["fala de b", "fim de b"]}


def test_erro_no_canal_nao_derruba_o_motor():
    token = progress.bind(lambda *a, **k: 1 / 0)
    try:
        progress.say("text", "x")
    finally:
        progress.unbind(token)


# --------------------------------------------------------------------------- stream-json dos CLIs (formatos reais)
def collect(parser_cls, lines):
    seen = []
    token = progress.bind(lambda kind, text="", **meta: seen.append((kind, text, meta)))
    parser = parser_cls()
    try:
        for line in lines:
            parser.feed(json.dumps(line) if isinstance(line, dict) else line)
    finally:
        progress.unbind(token)
    return parser, seen


CLAUDE_LINES = [
    {"type": "system", "subtype": "init", "model": "claude-haiku-5-5"},
    {"type": "system", "subtype": "thinking_tokens"},
    {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "", "signature": "xx"}]}},
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls -la", "description": "lista"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": "a.txt\nb.txt", "is_error": False}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "content": [{"type": "text", "text": "deu ruim"}], "is_error": True}]}},
    {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "Esta pasta tem 2 arquivos."}]}},
    {"type": "result", "result": "Esta pasta tem 2 arquivos.", "is_error": False, "total_cost_usd": 0.0042, "duration_ms": 9000,
     "num_turns": 3, "usage": {"input_tokens": 8, "output_tokens": 738}},
]


def test_claude_stream_json_vira_eventos_e_resultado_final():
    parser, seen = collect(ClaudeStream, CLAUDE_LINES)
    kinds = [k for k, _, _ in seen]
    assert kinds == ["status", "thinking", "tool", "tool_result", "tool_result", "text"]
    assert seen[0][1].startswith("Claude Code iniciado") and "claude-haiku-5-5" in seen[0][1]
    assert seen[2][1] == "Bash: ls -la" and seen[3][2]["error"] is False and seen[4][1] == "deu ruim" and seen[4][2]["error"] is True
    assert parser.text() == "Esta pasta tem 2 arquivos." and parser.is_error is False
    assert parser.meta["cost_usd"] == 0.0042 and parser.meta["input_tokens"] == 8 and parser.meta["output_tokens"] == 738


def test_claude_resultado_com_erro_e_linha_nao_json():
    parser, seen = collect(ClaudeStream, ["isto nao e json", {"type": "result", "result": "falhou", "is_error": True}])
    assert seen[0][0] == "status" and parser.is_error is True and parser.text() == "falhou"


AGY_LINES = [
    {"event": "init", "init": {"model": "gemini-3.8-flash-low", "tools": ["x"]}},
    {"event": "step_update", "step_update": {"step_index": 0, "state": "DONE", "step_type": "user_input"}},
    {"event": "step_update", "step_update": {"step_index": 2, "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command",
                                              "tool_info": {"name": "run_command", "parameters": {"CommandLine": "Get-ChildItem"}}}},
    {"event": "step_update", "step_update": {"step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "run_command",
                                              "tool_info": {"name": "run_command", "parameters": {"CommandLine": "Get-ChildItem"}, "output": "a.txt\nb.txt"}}},
    {"event": "step_update", "step_update": {"step_index": 3, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "Os arquivos "}},
    {"event": "step_update", "step_update": {"step_index": 3, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "sao dois.\n"}},
    {"event": "step_update", "step_update": {"step_index": 3, "state": "DONE", "step_type": "agent_response", "text_delta": "",
                                              "usage": {"input_tokens": 100, "output_tokens": 7}}},
    {"event": "result", "result": {"status": "SUCCESS", "response": "Os arquivos sao dois.\n", "duration_seconds": 5.0}},
]


def test_antigravity_stream_json_junta_as_falas_e_mostra_ferramentas():
    parser, seen = collect(AntigravityStream, AGY_LINES)
    assert [k for k, _, _ in seen] == ["status", "tool", "tool_result", "text"]
    assert seen[1][1] == "run_command: Get-ChildItem" and seen[2][1] == "a.txt\nb.txt"
    assert seen[3][1] == "Os arquivos sao dois.\n"                                    # as duas falas curtas viraram uma so
    assert parser.text() == "Os arquivos sao dois." and parser.ok is True
    assert parser.meta["input_tokens"] == 100 and parser.meta["output_tokens"] == 7 and parser.meta["duration_ms"] == 5000


@pytest.mark.asyncio
async def test_run_streaming_le_linha_a_linha_inclusive_enorme_e_estoura_o_prazo(tmp_path):
    script = tmp_path / "fala.py"
    script.write_text(
        "import sys, json\n"
        "print(json.dumps({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'oi'}]}}), flush=True)\n"
        "print('z' * 200000, flush=True)\n"
        "sys.exit(3)\n", encoding="utf-8")
    parser, seen = collect(ClaudeStream, [])
    token = progress.bind(lambda kind, text="", **m: seen.append((kind, text)))
    try:
        rc, raw, err = await run_streaming([sys.executable, str(script)], None, 20, parser)
    finally:
        progress.unbind(token)
    assert rc == 3 and ("text", "oi") in seen and len(raw) >= 200_000
    sleeper = tmp_path / "dorme.py"
    sleeper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    with pytest.raises(asyncio.TimeoutError):
        await run_streaming([sys.executable, str(sleeper)], None, 1, ClaudeStream())


# --------------------------------------------------------------------------- escalonador
class Talker:
    def __init__(self, name, fail=False, say_text="trabalhando"):
        self.name, self.fail, self.say_text, self.default_model = name, fail, say_text, "m"
        self._results = {}

    async def health_check(self):
        return HealthStatus(healthy=True, version="x", available_models=["m"])

    async def submit(self, task):
        async def run():
            progress.say("status", f"{self.name} comecou")
            progress.say("text", self.say_text)
            await asyncio.sleep(0.01)
            if self.fail:
                return AgentResult(task_id=task.id, status=TaskStatus.FAILED, summary="deu ruim", error_message="sem memoria")
            return AgentResult(task_id=task.id, status=TaskStatus.SUCCEEDED, summary="ok", output_text="feito",
                               metadata={"cost_usd": 0.01, "output_tokens": 5})
        self._results[task.id] = asyncio.create_task(run())
        return RunHandle(task_id=task.id)

    async def collect(self, handle):
        return await self._results.pop(handle.task_id)

    async def cancel(self, handle):
        return True


@pytest.mark.asyncio
async def test_escalonador_narra_inicio_fala_fim_e_cancelamento(tmp_path):
    from app.coordinator import Coordinator
    from app.state_store import StateStore
    bus = ConsoleBus(tmp_path / "runs")
    router = TaskRouter(adapters={"ollama": Talker("ollama"), "claude_code": Talker("claude_code", fail=True)})
    coordinator = Coordinator(store=StateStore(db_path=str(tmp_path / "t.db")), on_progress=lambda m: None)
    dag = TaskDAG()
    dag.add_task(AgentTask(id="run-7-s1", title="Primeiro", instruction="a", assigned_agent="claude_code", max_retries=0))
    dag.add_task(AgentTask(id="run-7-s2", title="Segundo", instruction="b", assigned_agent="ollama", dependencies=["run-7-s1"], max_retries=0))
    dag.add_task(AgentTask(id="run-7-s3", title="Terceiro", instruction="c", assigned_agent="ollama", max_retries=0))
    await DAGScheduler(router=router, coordinator=coordinator, max_concurrency=2, console=bus, run_id="run-7").execute_dag(dag)
    events = bus.events(run="run-7")
    by_step = {}
    for e in events:
        by_step.setdefault(e["step"], []).append(e["kind"])
    assert by_step["s1"] == ["step_start", "status", "text", "step_end"] or by_step["s1"][0] == "step_start" and by_step["s1"][-1] == "step_end"
    ends = {e["step"]: e for e in events if e["kind"] == "step_end"}
    assert ends["s1"]["status"] == "FAILED" and "sem memoria" in ends["s1"]["error"]
    assert ends["s2"]["status"] == "CANCELLED"                                           # dependia do que falhou
    assert ends["s3"]["status"] == "SUCCEEDED" and ends["s3"]["cost_usd"] == 0.01 and ends["s3"]["output_tokens"] == 5
    assert any(e["kind"] == "text" and e["step"] == "s3" and e["engine"] == "ollama" for e in events)


# --------------------------------------------------------------------------- API
def client():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS)


@pytest.mark.asyncio
async def test_api_console_exige_token_e_nao_deixa_ler_fora_da_pasta(tmp_path, monkeypatch):
    bus = ConsoleBus(tmp_path / "runs")
    monkeypatch.setattr(server, "CONSOLE", bus)
    bus.emit("plan_start", "meta", run="run-9", title="T", total=1)
    (tmp_path / "segredo.jsonl").write_text('{"kind":"x"}\n', encoding="utf-8")
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL) as anon:
        assert (await anon.get("/api/console")).status_code == 401
    async with client() as c:
        assert (await c.get("/api/console")).json()["events"][0]["kind"] == "plan_start"
        assert (await c.get("/api/console/runs")).json()["runs"][0]["id"] == "run-9"
        assert (await c.get("/api/console/runs/run-9")).status_code == 200
        for evil in ("..%2Fsegredo", "%2e%2e%2fsegredo", "nao-existe"):
            assert (await c.get(f"/api/console/runs/{evil}")).status_code in (404, 400)


@pytest.mark.asyncio
async def test_api_executar_plano_emite_a_historia_completa(tmp_path, monkeypatch):
    bus = ConsoleBus(tmp_path / "runs")
    monkeypatch.setattr(server, "CONSOLE", bus)
    fakes = {"ollama": Talker("ollama")}
    monkeypatch.setattr(server, "adapters", fakes)
    monkeypatch.setattr(server.router, "adapters", fakes)
    monkeypatch.setattr(server.router, "disabled", set())
    monkeypatch.setattr(server.router, "default_offline", False)
    body = {"goal": "Teste do console", "steps": [{"id": "s1", "title": "Falar", "instruction": "fala", "engine": "ollama"}]}
    async with client() as c:
        run_id = (await c.post("/api/orchestration/execute", json=body)).json()["run_id"]
    for _ in range(80):
        if any(e["kind"] == "plan_end" for e in bus.events(run=run_id)):
            break
        await asyncio.sleep(0.1)
    kinds = [e["kind"] for e in bus.events(run=run_id)]
    assert kinds[0] == "plan_start" and kinds[-1] == "plan_end" and "step_start" in kinds and "text" in kinds and "step_end" in kinds
    end = bus.events(run=run_id)[-1]
    assert end["status"] == "ok" and end["done"] == 1 and end["total"] == 1
    assert (tmp_path / "runs" / f"{run_id}.jsonl").is_file()                              # ficou guardado em disco


@pytest.mark.asyncio
@pytest.mark.parametrize("pedir_resumo, chamadas", [(None, 1), (True, 1), (False, 0)])
async def test_api_executar_resumo_final_e_opcional(tmp_path, monkeypatch, pedir_resumo, chamadas):
    """Quem ja e o planejador (o Claude da sessao) manda final_summary=False: o resumo automatico gastaria o
    plano do Claude Code a toa. Sem o campo, continua como sempre (resume)."""
    monkeypatch.setattr(server, "CONSOLE", ConsoleBus(tmp_path / "runs"))
    fakes = {"ollama": Talker("ollama")}
    monkeypatch.setattr(server, "adapters", fakes)
    monkeypatch.setattr(server.router, "adapters", fakes)
    monkeypatch.setattr(server.router, "disabled", set())
    monkeypatch.setattr(server.router, "default_offline", False)
    feitos = []

    async def resumo_falso(goal, report, run_id):
        feitos.append(run_id)
        return "resumo"

    monkeypatch.setattr(server, "_final_summary", resumo_falso)
    body = {"goal": "Teste", "steps": [{"id": "s1", "title": "Falar", "instruction": "fala", "engine": "ollama"}]}
    if pedir_resumo is not None:
        body["final_summary"] = pedir_resumo
    async with client() as c:
        run_id = (await c.post("/api/orchestration/execute", json=body)).json()["run_id"]
    for _ in range(80):
        if any(e["kind"] == "plan_end" for e in server.CONSOLE.events(run=run_id)):
            break
        await asyncio.sleep(0.1)
    assert len(feitos) == chamadas


def test_pagina_tem_o_console_e_trata_os_eventos():
    html = (server.WEB_DIR / "index.html").read_text(encoding="utf-8")
    for needle in ('id="hera-console"', 'id="hc-body"', "function hcAdd(", 'data.type === "console"', "hcInit();", "/api/console/runs"):
        assert needle in html, needle
    assert "innerHTML = ev.text" not in html and ".innerHTML = `" not in html.split("function hcRenderEvent")[1].split("// ---- execucoes anteriores")[0]
