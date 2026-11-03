from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from app.telegram import autodeploy as ad


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "nethub"
    (repo / "Bot" / "gateway").mkdir(parents=True)
    (repo / "Bot" / "ui.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "Bot" / "gateway" / "gw_server.py").write_text("y = 1\n", encoding="utf-8")
    _git(repo.parent, "init", "-q", str(repo))
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base")
    return repo


class FakeSc:
    """Simula sc.exe/tasklist/taskkill; o resto (git, python) passa para o sistema."""

    def __init__(self, estados=None, start_falha=False):
        self.estados = estados or {}
        self.chamadas = []
        self.start_falha = start_falha

    def __call__(self, cmd, cwd=None, timeout=120):
        cmd = list(cmd)
        if cmd[0] == "sc.exe":
            self.chamadas.append(" ".join(cmd))
            acao, svc = cmd[1], cmd[2]
            if acao == "query":
                fila = self.estados.get(svc, ["RUNNING"])
                est = fila.pop(0) if len(fila) > 1 else fila[0]
                return 0, f"        STATE              : 4  {est}\n"
            if acao == "start" and self.start_falha:
                return 5, "FALHA 5: Acesso negado."
            return 0, ""
        if cmd[0] in ("tasklist", "taskkill"):
            self.chamadas.append(" ".join(cmd))
            return 0, '"nssm.exe","123"'
        return ad.rodar_padrao(cmd, cwd, timeout)


def _sem_dormir(_):
    return None


def test_arquivos_do_agente_filtra_por_tempo_e_revertidos():
    E = lambda arq, ts, rev=False: SimpleNamespace(arquivo=arq, ts=ts, revertido=rev)
    entradas = [E("Bot/a.py", 5), E("Bot/b.py", 15), E("Bot/b.py", 16), E("Bot/c.py", 17, True), E("@ext/C/x", 18)]
    assert ad.arquivos_do_agente(entradas, 10) == ["Bot/b.py"]


def test_planejar_mapeia_servicos_e_bloqueia_sensiveis(tmp_path):
    ws = tmp_path
    (ws / "Bot" / "gateway").mkdir(parents=True)
    (ws / "Bot" / "gateway" / "gw.py").write_text("from rastreio_erros import x\n", encoding="utf-8")
    assert ad.planejar(["Bot/ui.py"], "br", ws).servicos == ["nethub-bot"]
    assert ad.planejar(["Bot/rastreio_erros.py"], "br", ws).servicos == ["nethub-bot", "nethub-gateway"]
    assert ad.planejar(["Bot/gateway/gw.py"], "es", ws).servicos == ["nethub-es-gateway"]
    assert ad.planejar(["App/index.html"], "br", ws).servicos == []
    for sens in ("Bot/pix_efi.py", "Admin/main.py", "Bot/.env", "Bot/acesso.py"):
        assert ad.planejar([sens], "br", ws).bloqueio, sens
    assert ad.planejar(["WebPlayer/src/app.js"], "br", ws).bloqueio


def test_commit_leva_so_os_arquivos_do_agente(tmp_path):
    repo = _repo(tmp_path)
    # trabalho pendente do Kaio, sem relação com o erro
    (repo / "Bot" / "gateway" / "gw_server.py").write_text("y = 2  # SEO do Kaio\n", encoding="utf-8")
    sujos = ad.arquivos_sujos(repo)
    assert sujos == {"Bot/gateway/gw_server.py"}
    # o agente corrige o ui.py
    (repo / "Bot" / "ui.py").write_text("x = 2\n", encoding="utf-8")
    sc = FakeSc()
    r = ad.publicar(repo, ["Bot/ui.py"], sujos, None, "br", "fix: teste", "Kaio", "k@k",
                    rodar=sc, dormir=_sem_dormir, porta_ok=lambda p: True)
    assert r.no_ar and r.servicos == ["nethub-bot"], r.mensagem
    assert _git(repo, "show", "--name-only", "--format=", "HEAD").split() == ["Bot/ui.py"]
    # o trabalho do Kaio continua pendente, fora do commit
    assert "Bot/gateway/gw_server.py" in _git(repo, "status", "--porcelain")
    assert any("sc.exe stop nethub-bot" in c for c in sc.chamadas)


def test_nao_commita_arquivo_que_ja_tinha_alteracao_pendente(tmp_path):
    repo = _repo(tmp_path)
    (repo / "Bot" / "ui.py").write_text("x = 9  # do Kaio\n", encoding="utf-8")
    sujos = ad.arquivos_sujos(repo)
    r = ad.publicar(repo, ["Bot/ui.py"], sujos, None, "br", "fix", "Kaio", "k@k",
                    rodar=FakeSc(), dormir=_sem_dormir)
    assert not r.commit and not r.no_ar and "pendente" in r.mensagem
    assert _git(repo, "log", "--oneline").count("\n") == 1


def test_sensivel_commita_mas_nao_sobe(tmp_path):
    repo = _repo(tmp_path)
    (repo / "Bot" / "pix_efi.py").write_text("z = 1\n", encoding="utf-8")
    sc = FakeSc()
    r = ad.publicar(repo, ["Bot/pix_efi.py"], set(), None, "br", "fix", "Kaio", "k@k", rodar=sc, dormir=_sem_dormir)
    assert r.commit and not r.no_ar and "sensível" in r.mensagem
    assert not any("stop" in c for c in sc.chamadas)


def test_codigo_quebrado_reverte_sem_reiniciar(tmp_path):
    repo = _repo(tmp_path)
    (repo / "Bot" / "ui.py").write_text("def f(:\n", encoding="utf-8")
    sc = FakeSc()
    r = ad.publicar(repo, ["Bot/ui.py"], set(), None, "br", "fix", "Kaio", "k@k", rodar=sc, dormir=_sem_dormir)
    assert not r.no_ar and r.revertido and "compila" in r.mensagem
    assert (repo / "Bot" / "ui.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not any("stop" in c for c in sc.chamadas)


def test_teste_novo_falhando_reverte(tmp_path):
    repo = _repo(tmp_path)
    (repo / "Bot" / "ui.py").write_text("x = 3\n", encoding="utf-8")
    r = ad.publicar(repo, ["Bot/ui.py"], set(), {"t::velho"}, "br", "fix", "Kaio", "k@k",
                    rodar=FakeSc(), dormir=_sem_dormir, suite=lambda ws: {"t::velho", "t::novo"})
    assert r.revertido and "t::novo" in r.mensagem and not r.no_ar


def test_servico_que_cai_depois_de_subir_reverte_e_reinicia_de_novo(tmp_path):
    repo = _repo(tmp_path)
    (repo / "Bot" / "ui.py").write_text("x = 4\n", encoding="utf-8")
    # stop -> STOPPED; start; saúde: RUNNING e depois PAUSED (crash-loop); após reverter: RUNNING
    sc = FakeSc(estados={"nethub-bot": ["STOPPED", "STOPPED", "RUNNING", "PAUSED", "STOPPED", "STOPPED", "RUNNING"]})
    r = ad.publicar(repo, ["Bot/ui.py"], set(), None, "br", "fix", "Kaio", "k@k",
                    rodar=sc, dormir=_sem_dormir, porta_ok=lambda p: True)
    assert not r.no_ar and r.revertido and "PAUSED" in r.mensagem
    assert (repo / "Bot" / "ui.py").read_text(encoding="utf-8") == "x = 1\n"
    assert sum("sc.exe stop nethub-bot" in c for c in sc.chamadas) == 2


def test_stop_pending_so_mata_se_for_nssm():
    sc = FakeSc(estados={"svc": ["STOP_PENDING", "STOP_PENDING", "RUNNING"]})
    sc_outro = lambda cmd, cwd=None, t=0: (0, '"python.exe","123"') if cmd[0] == "tasklist" else sc(cmd, cwd, t)
    erro = ad.reiniciar("svc", sc_outro, _sem_dormir)
    assert "não é nssm" in erro
    assert not any("taskkill" in c for c in sc.chamadas)
