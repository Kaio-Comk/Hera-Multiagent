"""Executa um plano no Hera pelo terminal e imprime o resultado compacto (sem abrir a interface web).

Feito em 26/10/2026 para o Claude Code usar o Hera como "mão de obra": o Claude planeja, o Hera
distribui os passos (Ollama, Antigravity, Roteador) e este script devolve só o que importa de
cada passo — o texto bruto dos motores não passa pelo contexto do Claude.

O resumo final automático vem DESLIGADO (`final_summary: false`): quem chama já é o planejador,
e o resumo pelo Claude Code gastaria o plano do Claude à toa.

Uso:
    python tools\\plano.py plano.json                 # executa e espera
    python tools\\plano.py plano.json --max 1500      # corta cada saída em 1500 caracteres
    python tools\\plano.py --status                   # motores no ar e modelos que cabem

O servidor precisa estar no ar sem navegador:  python main.py web --no-browser
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
BASE = "http://127.0.0.1:8000"


def _token() -> str:
    return (RAIZ / "data" / ".hera_token").read_text(encoding="utf-8").strip()


def _api(caminho: str, corpo: dict | None = None, timeout: int = 60) -> dict:
    dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8") if corpo is not None else None
    req = urllib.request.Request(BASE + caminho, data=dados, method="POST" if dados else "GET",
                                 headers={"X-Hera-Token": _token(), "Content-Type": "application/json; charset=utf-8"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Hera recusou ({e.code}): {e.read().decode('utf-8', 'replace')[:600]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Hera fora do ar ({e.reason}). Suba com: python main.py web --no-browser")


def status() -> None:
    saude = _api("/api/health", timeout=60)["engines"]
    orq = _api("/api/orchestration")
    for nome, e in saude.items():
        if e.get("enabled"):
            print(f"{nome:12s} {'NO AR' if e['healthy'] else 'fora'}  {', '.join(e.get('available_models') or [])[:160]}")
    print("planejador do Hera:", orq["planner"], "| offline:", orq["offline"])
    print("(Ollama: só cabem modelos até ~70% da VRAM; nesta GTX 1650 Super de 4 GB, até ~2,9 GB)")


def executar(arquivo: Path, maximo: int, limite_s: int) -> int:
    plano = json.loads(arquivo.read_text(encoding="utf-8"))
    plano.setdefault("final_summary", False)
    r = _api("/api/orchestration/execute", plano)
    run = r["run_id"]
    for aviso in r.get("warnings") or []:
        print("AVISO:", aviso)
    print(f"plano {run}: {len(r['steps'])} passo(s) na fila")
    t0, eventos = time.time(), []
    while time.time() - t0 < limite_s:
        eventos = _api(f"/api/console/runs/{run}")["events"]
        if any(e.get("kind") == "plan_end" for e in eventos):
            break
        time.sleep(3)
    else:
        print(f"ainda rodando depois de {limite_s}s; consulte de novo ou cancele: POST /api/orchestration/runs/{run}/cancel")
        return 2
    textos: dict[str, list[str]] = {}
    for e in eventos:
        if e.get("kind") == "text" and e.get("step"):
            textos.setdefault(e["step"], []).append(e.get("text") or e.get("message") or "")
    falhas = 0
    for e in eventos:
        if e.get("kind") == "step_end":
            sid = e.get("step")
            ok = e.get("status") == "SUCCEEDED"
            falhas += not ok
            texto = "".join(textos.get(sid, [])).strip()
            print(f"\n## {sid} [{e.get('engine')}/{e.get('model')}] {e.get('status')} em {e.get('seconds')}s")
            print(texto[:maximo] + (" […]" if len(texto) > maximo else ""))
    fim = next(e for e in eventos if e.get("kind") == "plan_end")
    print(f"\n{fim.get('done')}/{fim.get('total')} passo(s) ok em {fim.get('seconds')}s")
    return 0 if falhas == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("plano", nargs="?", type=Path, help="JSON com goal, steps e executors")
    ap.add_argument("--max", type=int, default=1200, help="caracteres por saída de passo")
    ap.add_argument("--limite", type=int, default=900, help="segundos máximos esperando")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if a.status or not a.plano:
        status()
        return 0
    return executar(a.plano, a.max, a.limite)


if __name__ == "__main__":
    sys.exit(main())
