"""Motores por CLI oficial (Codex, Cursor, Qoder, CodeBuddy, Kimi): comando montado sem shell, prompt grande, login e execução."""
import asyncio
import sys

import pytest

from adapters.cli_agents import (CLI_ENGINES, CodeBuddyAdapter, CodexAdapter, CursorAdapter, HeadlessCliAdapter, KimiAdapter,
                                 MAX_ARGV_PROMPT, QoderAdapter, fit_prompt)
from models.agent import RunHandle
from models.task import AgentTask, TaskStatus


def task(**kw):
    return AgentTask(id="t1", title="t", instruction="faca & isso | aquilo", **kw)


def test_prompt_grande_e_cortado_no_meio_e_o_curto_passa_intacto():
    assert fit_prompt("curto") == "curto"
    big = "A" * 20000 + "B" * 20000
    out = fit_prompt(big)
    assert len(out) <= MAX_ARGV_PROMPT and out.startswith("AAAA") and out.endswith("BBBB") and "omitido" in out


def test_todos_os_motores_estao_registrados_e_o_prompt_vai_sempre_inteiro_num_unico_argumento():
    assert set(CLI_ENGINES) == {"codex", "cursor", "qoder", "codebuddy", "kimi"}
    prompt = "faca & isso | aquilo; rm -rf / && echo $HOME"
    for cls in (CursorAdapter, QoderAdapter, CodeBuddyAdapter, KimiAdapter):
        args = cls().build_args(task(), prompt, None, None)
        assert prompt in args and all(isinstance(a, str) for a in args)          # lista de argumentos: nada passa por shell
    assert CodexAdapter.prompt_by_stdin and CodexAdapter().build_args(task(), prompt, None, None)[-1] == "-"


def test_somente_leitura_por_padrao_e_escrita_so_com_auto_approve(tmp_path):
    ws = str(tmp_path)
    assert "read-only" in CodexAdapter().build_args(task(), "p", None, ws)
    assert "workspace-write" in CodexAdapter(auto_approve=True).build_args(task(), "p", None, ws)
    assert "--force" not in CursorAdapter().build_args(task(), "p", None, ws)
    assert "--force" in CursorAdapter(auto_approve=True).build_args(task(), "p", None, ws)
    assert "--permission-mode" not in QoderAdapter().build_args(task(), "p", None, ws)
    assert "--permission-mode" in CodeBuddyAdapter(auto_approve=True).build_args(task(), "p", None, ws)
    assert "--yolo" not in KimiAdapter().build_args(task(), "p", None, ws) and "--yolo" in KimiAdapter(auto_approve=True).build_args(task(), "p", None, ws)
    for cls in CLI_ENGINES.values():                              # nenhum liga o modo "pular todas as permissões"
        assert not any("dangerously" in a or "bypass" in a for a in cls(auto_approve=True).build_args(task(), "p", "m1", ws))


def test_modelo_e_pasta_de_trabalho_chegam_ao_cli(tmp_path):
    ws = str(tmp_path)
    assert CodexAdapter().build_args(task(), "p", "gpt-x", ws)[:0] == [] and "gpt-x" in CodexAdapter().build_args(task(), "p", "gpt-x", ws)
    assert ws in CodexAdapter().build_args(task(), "p", None, ws) and ws in CursorAdapter().build_args(task(), "p", None, ws)
    assert ws in QoderAdapter().build_args(task(), "p", None, ws)


def test_binario_fixo_por_variavel_de_ambiente(tmp_path, monkeypatch):
    exe = tmp_path / "meu-codex.exe"
    exe.write_text("x")
    monkeypatch.setenv("HERA_CODEX_PATH", str(exe))
    assert CodexAdapter().launcher() == [str(exe)]


@pytest.mark.asyncio
async def test_cli_ausente_vira_aviso_claro_e_nao_excecao(monkeypatch):
    class Ausente(HeadlessCliAdapter):
        name, label, install_hint = "x", "X", "Instale o X."
        def launcher(self): return None
    h = await Ausente().health_check()
    assert not h.healthy and "Instale o X" in h.error_message
    res = await Ausente()._execute_cli(task(), RunHandle(task_id="t1", start_time=0.0))
    assert res.status == TaskStatus.FAILED and "Instale o X" in res.error_message


class Falso(HeadlessCliAdapter):
    """CLI de mentira: o próprio Python imprime o que recebeu (argumento ou entrada padrão)."""
    name, label, login_hint = "falso", "Falso", "Rode `falso login`."

    def __init__(self, mode, **kw):
        super().__init__(binary_path=sys.executable, **kw)
        self.mode = mode
        self.prompt_by_stdin = mode == "stdin"

    def build_args(self, task, prompt, model, workspace):
        code = {"arg": "import sys;print('ARG:'+sys.argv[1])",
                "stdin": "import sys;print('IN:'+sys.stdin.read())",
                "erro": "import sys;sys.stderr.write('Not logged in');sys.exit(1)",
                "vazio": "pass"}[self.mode]
        return ["-c", code] + ([prompt] if self.mode == "arg" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,prefix", [("arg", "ARG:"), ("stdin", "IN:")])
async def test_execucao_devolve_o_texto_e_o_prompt_chega_por_argumento_ou_stdin(mode, prefix):
    ad = Falso(mode)
    handle = await ad.submit(task())
    res = await ad.collect(handle)
    assert res.status == TaskStatus.SUCCEEDED and res.output_text.startswith(prefix) and "faca & isso | aquilo" in res.output_text


@pytest.mark.asyncio
async def test_sem_login_e_saida_vazia_falham_com_motivo():
    res = await (ad := Falso("erro")).collect(await ad.submit(task()))
    assert res.status == TaskStatus.FAILED and "sem login" in res.error_message and "falso login" in res.error_message
    res = await (ad := Falso("vazio")).collect(await ad.submit(task()))
    assert res.status == TaskStatus.FAILED


@pytest.mark.asyncio
async def test_workspace_invalido_aborta():
    ad = Falso("arg")
    res = await ad.collect(await ad.submit(task(workspace="Z:/nao/existe")))
    assert res.status == TaskStatus.FAILED and "workspace" in res.error_message.lower()
