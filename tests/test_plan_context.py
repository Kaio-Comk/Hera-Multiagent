"""Planos: contexto dos passos anteriores, passo BLOQUEADO nao conta como sucesso, pasta do projeto validada."""
from pathlib import Path
from types import SimpleNamespace

from app.dag_scheduler import DAGScheduler
from app.orchestration import build_dag
from models.task import AgentResult, AgentTask, TaskStatus


def _sched(run_id="run-x"):
    return DAGScheduler(router=None, coordinator=None, validator=None, run_id=run_id)


def _res(tid, text):
    return AgentResult(task_id=tid, status=TaskStatus.SUCCEEDED, summary="ok", output_text=text)


def test_instrucao_recebe_saida_dos_passos_anteriores_e_regra_de_bloqueio():
    t = AgentTask(id="run-x-s2", title="b", instruction="analise", dependencies=["run-x-s1"])
    out = _sched()._with_context(t, {"run-x-s1": _res("run-x-s1", "achei 3 erros")})
    assert "achei 3 erros" in out and "BLOQUEADO:" in out
    assert _sched()._with_context(AgentTask(id="a", title="t", instruction=out, dependencies=[]), {}) == out  # idempotente


def test_saida_de_dependencia_e_cortada():
    t = AgentTask(id="run-x-s2", title="b", instruction="x", dependencies=["run-x-s1"])
    out = _sched()._with_context(t, {"run-x-s1": _res("run-x-s1", "Ж" * 50000)})
    assert out.count("Ж") == DAGScheduler.MAX_DEP_CHARS


def test_fora_de_plano_nao_acrescenta_regra():
    t = AgentTask(id="t1", title="b", instruction="faca")
    assert _sched(run_id=None)._with_context(t, {}) == "faca"


def test_bloqueado_e_detectado():
    s = _sched()
    assert s._is_blocked(_res("a", "  bloqueado: sem acesso"))
    assert not s._is_blocked(_res("a", "Feito. Nada bloqueado."))


def test_build_dag_leva_workspace_e_pastas_extras(tmp_path: Path):
    steps = [{"id": "s1", "title": "t", "instruction": "i", "type": "coding", "engine": "", "model": "", "depends_on": []}]
    dag = build_dag(steps, "run-x", str(tmp_path), [str(tmp_path / "vault")])
    task = dag.tasks["run-x-s1"]
    assert task.workspace == str(tmp_path) and task.extra_dirs == [str(tmp_path / "vault")]


def test_claude_code_passa_add_dir(tmp_path: Path, monkeypatch):
    import asyncio
    from adapters import claude_code as cc

    seen = {}

    async def fake_run(cmd, cwd, timeout, parser, on_proc=None):
        seen["cmd"] = cmd
        return 0, "", ""

    monkeypatch.setattr(cc, "run_streaming", fake_run)
    adapter = cc.ClaudeCodeAdapter(binary_path="claude")
    task = AgentTask(id="t", title="t", instruction="oi", workspace=str(tmp_path), extra_dirs=[str(tmp_path)])
    asyncio.run(adapter._execute_cli(task, SimpleNamespace(start_time=0.0, process_id=None)))
    cmd = seen["cmd"]
    assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path)
    assert cmd[-2] == "-p" and cmd.index("--add-dir") < cmd.index("--output-format")


def test_ollama_recusa_modelo_que_nao_cabe_na_gpu():
    import asyncio
    from adapters.ollama import OllamaAdapter

    adapter = OllamaAdapter()

    async def refuse(model):
        return f"Modelo recusado: {model} grande demais"

    adapter.fit_check = refuse
    task = AgentTask(id="t", title="t", instruction="oi", assigned_model="gigante:70b")
    res = asyncio.run(adapter._execute_generate(task, SimpleNamespace(start_time=0.0, process_id=None)))
    assert res.status == TaskStatus.FAILED and "gigante:70b" in res.error_message


def test_planejador_pede_e_normaliza_criterios_de_aceite():
    from app import orchestration as o

    assert "acceptance_criteria" in o.build_planning_prompt("x", [], 4)
    raw = [{"id": "s1", "instruction": "faca", "acceptance_criteria": ["  cita linha  ", "", "b" * 999] + ["c"] * 9},
           {"id": "s2", "instruction": "sem criterio", "acceptance_criteria": "texto solto"}]
    steps, warnings = o.normalize_steps(raw, ["ollama"], 5)
    assert len(steps[0]["acceptance_criteria"]) == o.MAX_CRITERIA and steps[0]["acceptance_criteria"][0] == "cita linha"
    assert all(len(c) <= o.MAX_CRITERION_CHARS for c in steps[0]["acceptance_criteria"])
    assert steps[1]["acceptance_criteria"] == [] and any("s2" in w and "criterio" in w for w in warnings)
    dag = o.build_dag(steps, "run-x", ".")
    assert dag.tasks["run-x-s1"].acceptance_criteria[0] == "cita linha"


def test_plano_aceita_comandos_de_validacao_e_o_validador_os_executa(tmp_path):
    import asyncio
    from app import orchestration as o
    from app.validator import TaskValidator

    raw = [{"id": "s1", "instruction": "faca", "validation_commands": ["python -m py_compile ok.py", "", "python -m pytest -q"] + ["python -m pytest -q"] * 5}]
    steps, _ = o.normalize_steps(raw, ["ollama"], 5)
    cmds = steps[0]["validation_commands"]
    assert len(cmds) == o.MAX_COMMANDS and cmds[0] == "python -m py_compile ok.py" and all(len(c) <= o.MAX_COMMAND_CHARS for c in cmds)

    (tmp_path / "ok.py").write_text("raise SystemExit(0)", encoding="utf-8")
    (tmp_path / "falha.py").write_text("def (:", encoding="utf-8")
    dag = o.build_dag(steps, "run-x", str(tmp_path))
    task = dag.tasks["run-x-s1"]
    assert task.validation_commands == cmds

    res = AgentResult(task_id=task.id, status=TaskStatus.SUCCEEDED, summary="ok", output_text="feito")
    task.validation_commands = ["python -m py_compile ok.py"]
    assert asyncio.run(TaskValidator().validate(task, res)).passed is True
    task.validation_commands = ["python -m py_compile falha.py"]          # o passo so passa se o comando de teste terminar com 0
    out = asyncio.run(TaskValidator().validate(task, res))
    assert out.passed is False and "py_compile falha.py" in out.failure_summary
    task.validation_commands = ["python ok.py"]             # a politica so libera py_compile/pytest: o resto e recusado
    assert asyncio.run(TaskValidator().validate(task, res)).passed is False


def test_passo_opcional_que_falha_nao_cancela_dependentes():
    s = _sched()
    opt = AgentTask(id="run-x-s4", title="OPCIONAL: medir algo", instruction="x")
    normal = AgentTask(id="run-x-s5", title="medir algo", instruction="x")
    falhou = AgentResult(task_id="a", status=TaskStatus.FAILED, summary="x")
    cancelado = AgentResult(task_id="a", status=TaskStatus.CANCELLED, summary="x")
    assert s._is_optional(opt, falhou)
    assert not s._is_optional(normal, falhou)
    assert not s._is_optional(opt, cancelado) and not s._is_optional(opt, None)


def test_dag_segue_para_o_consolidador_quando_so_o_opcional_falha():
    import asyncio
    from app.dag_scheduler import TaskDAG

    class FakeAdapter:
        name = "fake"

        def __init__(self):
            self.last = None

        async def submit(self, task):
            self.last = task
            return task

        async def collect(self, task):
            ok = "falha" not in task.title
            return AgentResult(task_id=task.id, status=TaskStatus.SUCCEEDED if ok else TaskStatus.FAILED,
                               summary="ok" if ok else "erro", output_text="feito", error_message="" if ok else "erro")

    class FakeRouter:
        quota = None

        async def route(self, task):
            return FakeAdapter(), "m", "r"

    class FakeCoord:
        def __getattr__(self, name):
            return lambda *a, **k: None

    class FakeValidator:
        async def validate(self, task, result):
            return SimpleNamespace(passed=result.status == TaskStatus.SUCCEEDED, failure_summary="x", commands_executed={})

    def mk(i, title, deps):
        return AgentTask(id=f"run-x-{i}", title=title, instruction="i", dependencies=[f"run-x-{d}" for d in deps])

    for opt_title, esperado in (("OPCIONAL: falha", TaskStatus.SUCCEEDED), ("falha obrigatoria", TaskStatus.CANCELLED)):
        dag = TaskDAG()
        for t in (mk("s1", "ok", []), mk("s2", opt_title, []), mk("s3", "consolidar", ["s1", "s2"])):
            dag.add_task(t)
        sched = DAGScheduler(router=FakeRouter(), coordinator=FakeCoord(), validator=FakeValidator(), run_id="run-x")
        res = asyncio.run(sched.execute_dag(dag))
        assert res["run-x-s3"].status == esperado


def test_resumo_final_leva_a_saida_de_todos_os_passos_cortada():
    from app import orchestration as o

    report = [{"id": "s1", "title": "a", "status": "SUCCEEDED", "engine": "x", "model": "m", "output_text": "achado-um"},
              {"id": "s2", "title": "b", "status": "FAILED", "engine": "x", "model": "m", "output_text": "", "error": "sem acesso"},
              {"id": "s3", "title": "c", "status": "SUCCEEDED", "engine": "x", "model": "m", "output_text": "§" * 20000}]
    p = o.build_synthesis_prompt("ajude o cliente", report)
    assert "achado-um" in p and "sem acesso" in p and "MENSAGEM PARA O CLIENTE" in p
    assert p.count("§") == o.SYNTH_STEP_CHARS
    t = o.build_synthesis_task("g", report, "claude_code", "sonnet", ".")
    assert t.assigned_agent == "claude_code" and t.assigned_model == "sonnet"


def test_validador_usa_o_python_do_venv_do_projeto_e_um_temp_proprio(tmp_path):
    import asyncio, os, sys
    from app.validator import TaskValidator

    v = TaskValidator()
    assert v._project_python(str(tmp_path)) is None
    venv = tmp_path / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    venv.mkdir(parents=True)
    exe = venv / ("python.exe" if os.name == "nt" else "python")
    exe.write_text("x")
    assert v._project_python(str(tmp_path)) == str(exe)
    env = v._build_clean_env(str(tmp_path))
    assert "USERNAME" in env or "USERNAME" not in os.environ          # o nome do usuario passa (evita `pytest-of-unknown`)
    assert "GROQ_API_KEY" not in env

    import tempfile

    made = []
    real = tempfile.mkdtemp
    limpo = tmp_path / "limpo"                       # pasta SEM .venv: cai no Python do Hera
    limpo.mkdir()
    (limpo / "ok.py").write_text("x = 1", encoding="utf-8")

    def spy(*a, **k):
        d = real(*a, **k)
        made.append(d)
        return d

    tempfile_mod = __import__("app.validator", fromlist=["tempfile"]).tempfile
    tempfile_mod.mkdtemp = spy
    try:
        code, _out, err = asyncio.run(v._run_command("python -m py_compile ok.py", cwd=str(limpo)))
    finally:
        tempfile_mod.mkdtemp = real
    assert code == 0, err
    assert made and "hera-val-" in made[0] and not os.path.exists(made[0])        # o TEMP do filho e proprio e some no fim


def test_nome_do_projeto_da_conversa_aponta_a_pasta(tmp_path):
    from app.project_match import match_project_folders

    roots = [str(tmp_path / "NetHub BR"), str(tmp_path / "NetHub ES")]
    assert match_project_folders("Ajude essa cliente que reclamou do audio", roots) == []
    assert match_project_folders("Ajude essa cliente que reclamou do audio NetHub", roots) == roots
