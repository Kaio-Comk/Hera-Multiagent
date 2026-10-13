"""O modelo escolhido no painel chega ao CLI (--model), validado; a lista do Antigravity vem do proprio CLI."""
import asyncio

import pytest

from adapters.antigravity import AntigravityAdapter
from adapters.claude_code import CLAUDE_MODELS, ClaudeCodeAdapter
from adapters.model_args import clean_model, model_args
from app.config_manager import ConfigManager
from models.task import AgentTask, TaskStatus


@pytest.mark.parametrize("value,expected", [
    ("sonnet", "sonnet"), ("claude-opus-5-5", "claude-opus-5-5"), ("gemini-3.8-flash-low", "gemini-3.8-flash-low"),
    ("meta/llama-3.3-70b:free", "meta/llama-3.3-70b:free"), ("sonnet[1m]", "sonnet[1m]"),
    (None, None), ("", None), ("  ", None), ("default", None), ("DEFAULT", None), ("auto", None),
    ("--dangerously-skip-permissions", None), ("-p", None), ("a b", None), ("x;rm -rf /", None), ("$(calc)", None),
    ("a\nb", None), ("x" * 200, None), ("`id`", None), ("a|b", None),
])
def test_clean_model(value, expected):
    assert clean_model(value) == expected


def test_model_args_so_aceita_o_que_o_cli_lista():
    assert model_args("opus") == ["--model", "opus"]
    assert model_args("opus", ["sonnet", "opus"]) == ["--model", "opus"]
    assert model_args("gemini-2.5-pro", ["gemini-3.8-flash-low"]) == []        # modelo que o CLI nao conhece: usa o padrao
    assert model_args("default") == [] and model_args(None) == []


class FakeProc:
    def __init__(self, out=b"OK", code=0):
        self._out, self.returncode, self.pid = out, code, 1234

    async def communicate(self):
        return self._out, b""


def capture_exec(monkeypatch, outputs=None):
    calls = []

    async def fake(*args, **kwargs):
        calls.append(list(args))
        out = (outputs or {}).get(args[1] if len(args) > 1 else "", b"OK")
        return FakeProc(out)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)
    return calls


@pytest.mark.asyncio
async def test_claude_code_passa_o_modelo_escolhido(monkeypatch, tmp_path):
    calls = capture_exec(monkeypatch)
    adapter = ClaudeCodeAdapter()
    task = AgentTask(id="t1", title="x", instruction="faz", assigned_model="haiku", workspace=str(tmp_path))
    result = await adapter.collect(await adapter.submit(task))
    assert result.status == TaskStatus.SUCCEEDED
    assert calls[0][1:3] == ["--model", "haiku"] and "-p" in calls[0]


@pytest.mark.asyncio
async def test_claude_code_sem_modelo_ou_modelo_perigoso_nao_leva_flag(monkeypatch, tmp_path):
    calls = capture_exec(monkeypatch)
    adapter = ClaudeCodeAdapter()
    for model in (None, "default", "--dangerously-skip-permissions", "x; calc"):
        task = AgentTask(id=f"t-{model}", title="x", instruction="faz", assigned_model=model, workspace=str(tmp_path))
        await adapter.collect(await adapter.submit(task))
    assert all("--model" not in c and "--dangerously-skip-permissions" not in c for c in calls)


def test_lista_do_claude_tem_apelidos_e_nomes_novos():
    assert {"sonnet", "opus", "haiku"} <= set(CLAUDE_MODELS) and "claude-sonnet-5-5" in CLAUDE_MODELS


AGY_MODELS = (b"Fetching available models...\n"
              b"gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
              b"claude-opus-5-5-low\tClaude Opus 5.5 (Low)\n"
              b"gpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n")


@pytest.mark.asyncio
async def test_antigravity_le_a_lista_real_e_so_passa_modelo_conhecido(monkeypatch, tmp_path):
    calls = capture_exec(monkeypatch, outputs={"models": AGY_MODELS, "--version": b"agy 9.9"})
    adapter = AntigravityAdapter()
    health = await adapter.health_check()
    assert health.healthy and health.available_models == ["gemini-3.8-flash-high", "claude-opus-5-5-low", "gpt-oss-120b-medium"]
    ok = AgentTask(id="a1", title="x", instruction="faz", assigned_model="claude-opus-5-5-low", workspace=str(tmp_path))
    await adapter.collect(await adapter.submit(ok))
    assert calls[-1][1:3] == ["--model", "claude-opus-5-5-low"]
    unknown = AgentTask(id="a2", title="x", instruction="faz", assigned_model="gemini-2.5-pro", workspace=str(tmp_path))
    await adapter.collect(await adapter.submit(unknown))
    assert "--model" not in calls[-1]                                     # nome antigo que o CLI nao lista: usa o padrao
    assert sum(1 for c in calls if len(c) > 1 and c[1] == "models") == 1  # a lista fica em cache


@pytest.mark.asyncio
async def test_antigravity_sem_lista_cai_nos_modelos_conhecidos(monkeypatch):
    async def boom(*args, **kwargs):
        raise OSError("sem agy")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    models, real = await AntigravityAdapter()._discover_models()
    assert real is False and models and all(clean_model(m) for m in models)


def test_config_antiga_com_modelos_que_os_clis_nao_aceitam_e_migrada(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("engines:\n  claude_code:\n    enabled: true\n    default_model: claude-3-7-sonnet\n"
                    "  antigravity:\n    enabled: true\n    default_model: gemini-2.5-pro\n  nine_router:\n    enabled: true\n", encoding="utf-8")
    cfg = ConfigManager(config_path=str(path)).load_config()
    assert cfg["engines"]["claude_code"]["default_model"] == "sonnet" and cfg["engines"]["antigravity"]["default_model"] == "default"
    assert "nine_router" not in cfg["engines"]
