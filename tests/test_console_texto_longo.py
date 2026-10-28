"""Saida longa de um passo nao pode ser cortada em silencio.

Bug de 28/10/2026: o ConsoleBus cortava cada evento de texto em 8.000 caracteres e descartava o resto; o plano.py
(que junta os eventos do passo) entregava SKILL.md e codigo pela metade, mesmo com --max alto.
"""
from app.console_bus import MAX_TEXT, ConsoleBus


def _texto_do_passo(bus, run, step):
    return "".join(e["text"] for e in bus.read_run(run) if e["kind"] == "text" and e["step"] == step)


def test_texto_maior_que_o_limite_chega_inteiro_em_varios_eventos(tmp_path):
    bus = ConsoleBus(tmp_path / "runs")
    original = "".join(f"linha {i:05d} do arquivo gerado\n" for i in range(1400))      # ~42 mil caracteres
    assert len(original) > 4 * MAX_TEXT
    bus.emit("text", original, run="run-1", step="s1")
    assert _texto_do_passo(bus, "run-1", "s1") == original
    assert all(len(e["text"]) <= MAX_TEXT for e in bus.read_run("run-1"))


def test_texto_curto_continua_um_evento_so(tmp_path):
    bus = ConsoleBus(tmp_path / "runs")
    bus.emit("text", "ok", run="run-2", step="s1")
    assert [e["text"] for e in bus.read_run("run-2") if e["kind"] == "text"] == ["ok"]


def test_outros_tipos_de_evento_continuam_cortados_no_limite(tmp_path):
    bus = ConsoleBus(tmp_path / "runs")
    bus.emit("tool_result", "x" * (3 * MAX_TEXT), run="run-3", step="s1")
    eventos = [e for e in bus.read_run("run-3") if e["kind"] == "tool_result"]
    assert len(eventos) == 1 and len(eventos[0]["text"]) == MAX_TEXT
