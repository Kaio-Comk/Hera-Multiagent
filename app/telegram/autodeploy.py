"""Commit e deploy da auto-cura do NetHub (03/11/2026).

Duas regras que o laço antigo não tinha:
1. O commit leva SÓ os arquivos que o agente editou (antes era `git add -u`, que levava
   qualquer alteração pendente da pasta: o 1331037 "auto-cura do gateway" levou junto
   robots.txt, sitemap e a troca 302->301 da raiz).
2. A correção entra no ar sozinha, com portões: pyflakes (undefined name) e py_compile nos
   arquivos editados, nenhuma falha NOVA na suíte, reinício só do serviço afetado,
   conferência de que ele continua de pé e, se algo falhar, `git revert` + novo reinício.

Arquivo sensível (pagamento, acesso, Admin com o webhook da Efí, .env) nunca sobe sozinho:
fica commitado e o Kaio revisa.
"""
from __future__ import annotations

import re
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Sequence, Set, Tuple

Rodar = Callable[[Sequence[str], Optional[Path], int], Tuple[int, str]]


def rodar_padrao(cmd: Sequence[str], cwd: Optional[Path] = None, timeout: int = 120) -> Tuple[int, str]:
    try:
        p = subprocess.run(list(cmd), cwd=str(cwd) if cwd else None, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"tempo esgotado ({timeout} s)"
    except OSError as exc:
        return 127, str(exc)


# ---------------------------------------------------------------- arquivos do agente

def arquivos_sujos(workspace: Path, rodar: Rodar = rodar_padrao) -> Set[str]:
    """Caminhos (posix, relativos) com alteração pendente no git, rastreados ou não."""
    cod, out = rodar(["git", "-C", str(workspace), "status", "--porcelain", "-z", "--untracked-files=all"], None, 60)
    if cod != 0:
        return set()
    sujos: Set[str] = set()
    partes = out.split("\0")
    i = 0
    while i < len(partes):
        item = partes[i]
        i += 1
        if len(item) < 4:
            continue
        estado, caminho = item[:2], item[3:]
        sujos.add(caminho)
        if "R" in estado or "C" in estado:   # renomeado: o nome antigo vem no próximo campo
            i += 1
    return sujos


def arquivos_do_agente(entradas: Iterable, desde: float) -> List[str]:
    """Arquivos que o agente gravou desde `desde`, pelo livro-razão de checkpoints."""
    vistos: List[str] = []
    for e in entradas:
        if getattr(e, "revertido", False) or getattr(e, "ts", 0) < desde:
            continue
        arq = getattr(e, "arquivo", "")
        if arq and not arq.startswith("@ext/") and arq not in vistos:
            vistos.append(arq)
    return sorted(vistos)


# ---------------------------------------------------------------- mapa arquivo -> serviço

_SENSIVEL = re.compile(r"pix|pagament|efi|paywall|acesso|assinatur|webhook|token|segred|secret", re.I)

PORTAS = {"nethub-gateway": 8200, "nethub-app": 8085, "nethub-admin": 8001}
ORDEM = ("bot", "admin", "app", "gateway")


@dataclass
class Plano:
    servicos: List[str] = field(default_factory=list)
    bloqueio: str = ""


def _modulo(caminho: str) -> str:
    return Path(caminho).stem


def _importado_em(workspace: Path, modulo: str, pasta: str) -> bool:
    base = workspace / pasta
    if not base.is_dir():
        return False
    padrao = re.compile(rf"^\s*(from\s+(\w+\.)*{re.escape(modulo)}\b|import\s+.*\b{re.escape(modulo)}\b)", re.M)
    for f in base.rglob("*.py"):
        if "tests" in f.parts:
            continue
        try:
            if padrao.search(f.read_text(encoding="utf-8", errors="ignore")):
                return True
        except OSError:
            continue
    return False


def planejar(arquivos: Sequence[str], edicao: str, workspace: Path) -> Plano:
    """Quais serviços reiniciar, ou o motivo para não subir sozinho."""
    plano = Plano()
    curtos: Set[str] = set()
    for arq in arquivos:
        a = arq.replace("\\", "/")
        nome = a.rsplit("/", 1)[-1]
        if nome.startswith(".env") or a.startswith("Admin/") or _SENSIVEL.search(nome):
            plano.bloqueio = f"arquivo sensível ({a}): revise e suba à mão"
            return plano
        if a.startswith("Bot/tests/") or a.startswith("Docs/") or a.endswith(".md"):
            continue
        if a == "App/index.html":
            continue                                  # servido do disco a cada pedido
        if a.startswith("Bot/gateway/"):
            curtos.add("gateway")
        elif a.startswith("App/") and a.endswith(".py"):
            curtos.add("app")
        elif a.startswith("Bot/"):
            curtos.add("bot")
            if a.endswith(".py"):                     # módulo compartilhado: quem importa também reinicia
                mod = _modulo(a)
                if _importado_em(workspace, mod, "Bot/gateway"):
                    curtos.add("gateway")
                if _importado_em(workspace, mod, "App"):
                    curtos.add("app")
        else:
            plano.bloqueio = f"fora do mapa de deploy automático ({a}): revise e suba à mão"
            return plano
    pre = "nethub-es-" if edicao.lower() == "es" else "nethub-"
    plano.servicos = [pre + c for c in ORDEM if c in curtos]
    return plano


# ---------------------------------------------------------------- portões

def _python(workspace: Path) -> str:
    venv = workspace / ".venv" / "Scripts" / "python.exe"
    return str(venv) if venv.exists() else sys.executable


def checar_codigo(workspace: Path, arquivos: Sequence[str], rodar: Rodar = rodar_padrao) -> str:
    """'' se passou; senão o motivo. py_compile + pyflakes (undefined name) nos .py editados."""
    pys = [a for a in arquivos if a.endswith(".py") and (workspace / a).exists()]
    if not pys:
        return ""
    py = _python(workspace)
    for a in pys:
        cod, out = rodar([py, "-m", "py_compile", a], workspace, 60)
        if cod != 0:
            return f"não compila: {a}: {out.strip()[:300]}"
    cod, out = rodar([py, "-m", "pyflakes", *pys], workspace, 120)
    ruins = [l for l in out.splitlines() if "undefined name" in l]
    if ruins:
        return "pyflakes: " + "; ".join(ruins[:3])
    return ""


_FALHA = re.compile(r"^(FAILED|ERROR) (\S+)", re.M)


def falhas_da_suite(workspace: Path, rodar: Rodar = rodar_padrao, timeout: int = 900) -> Optional[Set[str]]:
    """Ids dos testes que falham hoje (None se a suíte nem rodou)."""
    if not (workspace / "Bot" / "tests").is_dir():
        return set()
    cod, out = rodar([_python(workspace), "-m", "pytest", "Bot/tests", "-q", "-rfE", "-p", "no:cacheprovider"],
                     workspace, timeout)
    if cod not in (0, 1):
        return None
    return {m.group(2) for m in _FALHA.finditer(out)}


# ---------------------------------------------------------------- reinício (NSSM)

def _estado(servico: str, rodar: Rodar) -> str:
    _, out = rodar(["sc.exe", "query", servico], None, 30)
    m = re.search(r"(?:STATE|ESTADO)\s*:\s*\d+\s+(\w+)", out)
    return m.group(1).upper() if m else ""


def reiniciar(servico: str, rodar: Rodar = rodar_padrao, dormir: Callable[[float], None] = time.sleep) -> str:
    """Reinicia um serviço NSSM sem Restart-Service (trava em StopPending). '' se ok."""
    rodar(["sc.exe", "stop", servico], None, 30)
    dormir(6)
    if _estado(servico, rodar) == "STOP_PENDING":
        _, q = rodar(["sc.exe", "queryex", servico], None, 30)
        m = re.search(r"PID\s*:\s*(\d+)", q)
        pid = m.group(1) if m else ""
        _, t = rodar(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], None, 30) if pid else (1, "")
        if pid and pid != "0" and t.lower().startswith('"nssm.exe"'):   # só mata se for o nssm DESTE serviço
            rodar(["taskkill", "/PID", pid, "/T", "/F"], None, 30)
            dormir(4)
        else:
            return f"{servico} travou parando e o PID {pid or '?'} não é nssm.exe; nada foi morto"
    if _estado(servico, rodar) != "RUNNING":
        cod, out = rodar(["sc.exe", "start", servico], None, 30)
        dormir(6)
        if cod != 0 and "1056" not in out:               # 1056 = já está rodando
            return f"não consegui iniciar {servico}: {out.strip()[:200]}"
    return ""


def porta_aberta(porta: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", porta), timeout=timeout):
            return True
    except OSError:
        return False


def saudavel(servico: str, rodar: Rodar = rodar_padrao, dormir: Callable[[float], None] = time.sleep,
             porta_ok: Callable[[int], bool] = porta_aberta) -> str:
    """'' se o serviço segue de pé 30 s depois de subir (crash-loop do NSSM derruba antes)."""
    for espera in (10, 20):
        dormir(espera)
        est = _estado(servico, rodar)
        if est != "RUNNING":
            return f"{servico} está {est or 'desconhecido'}"
    porta = PORTAS.get(servico)
    if porta and not porta_ok(porta):
        return f"{servico} subiu mas a porta {porta} não abriu"
    return ""


# ---------------------------------------------------------------- git

def commitar(workspace: Path, arquivos: Sequence[str], mensagem: str, nome: str, email: str,
             rodar: Rodar = rodar_padrao) -> Tuple[str, str]:
    """(hash, erro). Commit só dos arquivos dados (`commit --only`), nada mais entra."""
    ws = str(workspace)
    cod, out = rodar(["git", "-C", ws, "add", "--", *arquivos], None, 60)
    if cod != 0:
        return "", f"git add falhou: {out.strip()[:200]}"
    cod, out = rodar(["git", "-C", ws, "-c", f"user.name={nome}", "-c", f"user.email={email}",
                      "commit", "-m", mensagem, "--only", "--", *arquivos], None, 60)
    if cod != 0:
        return "", f"git commit falhou: {out.strip()[:200]}"
    _, h = rodar(["git", "-C", ws, "rev-parse", "--short", "HEAD"], None, 30)
    return h.strip(), ""


def reverter(workspace: Path, commit: str, nome: str, email: str, rodar: Rodar = rodar_padrao) -> str:
    cod, out = rodar(["git", "-C", str(workspace), "-c", f"user.name={nome}", "-c", f"user.email={email}",
                      "revert", "--no-edit", commit], None, 60)
    return "" if cod == 0 else f"git revert falhou: {out.strip()[:200]}"


# ---------------------------------------------------------------- orquestração

@dataclass
class Resultado:
    commit: str = ""
    no_ar: bool = False
    servicos: List[str] = field(default_factory=list)
    revertido: bool = False
    mensagem: str = ""


def publicar(workspace: Path, arquivos: Sequence[str], sujos_antes: Set[str], falhas_antes: Optional[Set[str]],
             edicao: str, mensagem_commit: str, nome: str, email: str, *, rodar: Rodar = rodar_padrao,
             dormir: Callable[[float], None] = time.sleep, porta_ok: Callable[[int], bool] = porta_aberta,
             suite: Callable[[Path], Optional[Set[str]]] = None) -> Resultado:
    r = Resultado()
    if not arquivos:
        r.mensagem = "o agente não editou nenhum arquivo"
        return r
    misturados = [a for a in arquivos if a in sujos_antes]
    if misturados:
        r.mensagem = ("não commitei: o agente editou arquivo que já tinha alteração sua pendente "
                      f"({', '.join(misturados)}); revise o diff")
        return r
    plano = planejar(arquivos, edicao, workspace)
    r.commit, erro = commitar(workspace, arquivos, mensagem_commit, nome, email, rodar)
    if erro:
        r.mensagem = erro
        return r
    if plano.bloqueio:
        r.mensagem = f"commit {r.commit} feito, mas não subi: {plano.bloqueio}"
        return r

    def _desfazer(motivo: str, reiniciar_de_novo: Sequence[str] = ()) -> Resultado:
        erro_rev = reverter(workspace, r.commit, nome, email, rodar)
        r.revertido = not erro_rev
        extra = ""
        for s in reiniciar_de_novo:
            e = reiniciar(s, rodar, dormir) or saudavel(s, rodar, dormir, porta_ok)
            if e:
                extra += f" ATENÇÃO, depois de reverter: {e}."
        r.mensagem = f"não subi: {motivo}. " + (f"Commit {r.commit} revertido." if r.revertido else erro_rev) + extra
        return r

    erro = checar_codigo(workspace, arquivos, rodar)
    if erro:
        return _desfazer(erro)
    suite = suite or (lambda ws: falhas_da_suite(ws, rodar))
    if falhas_antes is not None:
        depois = suite(workspace)
        if depois is None:
            return _desfazer("a suíte de testes não rodou")
        novas = sorted(depois - falhas_antes)
        if novas:
            return _desfazer("testes novos falhando: " + ", ".join(novas[:5]))
    if not plano.servicos:
        r.no_ar = True
        r.mensagem = f"commit {r.commit}; nenhum serviço precisa reiniciar"
        return r
    feitos: List[str] = []
    for s in plano.servicos:
        feitos.append(s)
        e = reiniciar(s, rodar, dormir) or saudavel(s, rodar, dormir, porta_ok)
        if e:
            return _desfazer(e, feitos)
    r.no_ar = True
    r.servicos = feitos
    r.mensagem = f"commit {r.commit} no ar; reiniciado(s): {', '.join(feitos)}"
    return r
