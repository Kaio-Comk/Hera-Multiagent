"""Plano multiagente: um modelo PLANEJADOR quebra o objetivo em passos e os demais motores ligados os executam.

Fluxo (duas chamadas, para o humano ver o plano antes de gastar nada):
  1. `build_planning_task`  -> o planejador devolve um JSON com os passos;
  2. `parse_plan`           -> o JSON e tratado como NAO CONFIAVEL: tamanho, motores ligados, dependencias e ciclos
                               sao validados aqui; o que nao serve e descartado, nunca executado;
  3. `build_dag`            -> os passos viram tarefas; o roteador (DLP / modo offline) continua mandando.
"""
from __future__ import annotations

import json
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from adapters.model_args import clean_model
from app.dag_scheduler import CycleDetectedError, DependencyNotFoundError, TaskDAG
from app.engine_profiles import catalog_text
from models.task import AgentTask

MAX_STEPS_LIMIT = 12
MAX_GOAL_CHARS = 8000
MAX_TITLE_CHARS = 120
MAX_REASON_CHARS = 200
MAX_INSTRUCTION_CHARS = 4000
STEP_TYPES = {"coding", "research", "review", "test", "docs", "summary"}
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ID = re.compile(r"^[a-z0-9_-]{1,40}$")


class PlanError(ValueError):
    """Plano recusado (mensagem pronta para mostrar ao usuario)."""


def clean_text(value: Any, limit: int) -> str:
    text = _CTRL.sub("", str(value or "")).strip()
    return text[:limit]


def build_planning_prompt(goal: str, catalog: List[Dict[str, Any]], max_steps: int) -> str:
    """Pedido ao planejador: o objetivo + o CATALOGO de motores e modelos (o que cada um faz bem, se mexe em arquivos,
    se e local/pago e a forca de cada modelo), para ele escolher motor E modelo de cada passo."""
    first = catalog[0]["engine"] if catalog else "ollama"
    return (
        "Voce e o PLANEJADOR de um time de agentes de IA. Quebre o OBJETIVO abaixo em passos pequenos que OUTROS motores "
        "e modelos vao executar. NAO execute nada e NAO escreva arquivos: responda somente com JSON valido, sem texto "
        "antes ou depois, neste formato:\n"
        '{"steps": [{"id": "s1", "title": "titulo curto", "instruction": "o que fazer, completo e autocontido", '
        '"type": "coding|research|review|test|docs|summary", "engine": "<motor>", "model": "<modelo da lista do motor>", '
        '"reason": "por que este motor e este modelo", "depends_on": []}]}\n'
        f"Exemplo de escolha: {{\"engine\": \"{first}\", \"model\": \"...\", \"reason\": \"...\"}}\n\n"
        "MOTORES E MODELOS DISPONIVEIS AGORA (escolha SO entre estes):\n" + catalog_text(catalog) + "\n\n"
        "COMO ESCOLHER:\n"
        "- Passo que precisa LER/EDITAR arquivos, rodar comandos ou analisar logs do projeto: so motores com acesso a arquivos.\n"
        "- Passo so de texto (pesquisar, resumir, comparar, redigir, revisar algo ja colado na instrucao): prefira motores sem "
        "acesso a arquivos; sao mais baratos.\n"
        "- Escolha o MODELO pela dificuldade: leve para o simples e repetitivo, equilibrado para o comum, forte so para raciocinio "
        "dificil, arquitetura, bugs sutis ou revisao critica. Nao gaste modelo forte em tarefa simples.\n"
        "- Prefira motor local/gratis quando ele da conta; varie os motores quando fizer sentido (inclusive para uma segunda "
        "opiniao); NAO coloque tudo no mesmo motor so por habito.\n"
        "- Use SOMENTE os nomes de motor e de modelo da lista, escritos exatamente como aparecem.\n"
        f"- No maximo {max_steps} passos; 'depends_on' lista ids de passos anteriores; cada instrucao deve ser entendida sem ver as outras.\n\n"
        f"OBJETIVO:\n{goal}"
    )


def build_planning_task(goal: str, catalog: List[Dict[str, Any]], max_steps: int, planner_engine: str,
                        planner_model: Optional[str], workspace: str) -> AgentTask:
    return AgentTask(
        id=f"plan-{uuid.uuid4().hex[:8]}",
        title="Planejamento multiagente",
        instruction=build_planning_prompt(goal, catalog, max_steps),
        task_type="research",
        assigned_agent=planner_engine,
        assigned_model=planner_model or None,
        timeout_seconds=180,
        max_retries=0,
        workspace=workspace,
    )


def _extract_json(text: str) -> Any:
    """Acha o JSON na resposta do modelo (aceita cerca ```json e texto em volta)."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S | re.I)
    candidates = [fenced.group(1)] if fenced else []
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for chunk in candidates:
        try:
            return json.loads(chunk)
        except (ValueError, TypeError):
            continue
    raise PlanError("O planejador nao devolveu um JSON valido.")


def normalize_steps(raw_steps: Any, executors: Iterable[str], max_steps: int,
                    models: Optional[Dict[str, List[str]]] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Valida e limpa os passos. Devolve (passos, avisos). Levanta PlanError se nada aproveitavel.
    `models` (motor -> modelos que ele lista): um modelo fora da lista do motor e descartado (vira o padrao do motor)."""
    allowed = set(executors)
    if not allowed:
        raise PlanError("Nenhum motor esta ligado para executar o plano.")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise PlanError("O plano veio sem passos.")
    max_steps = max(1, min(int(max_steps), MAX_STEPS_LIMIT))
    warnings: List[str] = []
    if len(raw_steps) > max_steps:
        warnings.append(f"O plano tinha {len(raw_steps)} passos; so os {max_steps} primeiros foram mantidos.")
        raw_steps = raw_steps[:max_steps]

    steps: List[Dict[str, Any]] = []
    seen: set = set()
    for index, item in enumerate(raw_steps, start=1):
        if not isinstance(item, dict):
            continue
        instruction = clean_text(item.get("instruction"), MAX_INSTRUCTION_CHARS)
        if not instruction:
            continue
        sid = str(item.get("id") or f"s{index}").strip().lower()
        if not _ID.match(sid) or sid in seen:
            sid = f"s{index}"
            while sid in seen:
                sid += "x"
        seen.add(sid)
        engine = str(item.get("engine") or "").strip()
        if engine and engine not in allowed:
            warnings.append(f"Passo {sid}: motor '{engine}' nao esta ligado; o roteador vai escolher.")
            engine = ""
        model = str(item.get("model") or "").strip()
        if model and not clean_model(model):
            model = ""
        if model and not engine:
            model = ""                                   # sem motor escolhido nao ha como validar o modelo
        if model and models is not None and models.get(engine) and model not in models[engine]:
            warnings.append(f"Passo {sid}: o modelo '{model}' nao existe no motor '{engine}'; vai usar o padrao do motor.")
            model = ""
        step_type = str(item.get("type") or "coding").strip().lower()
        if step_type not in STEP_TYPES:
            step_type = "coding"
        deps = item.get("depends_on") or []
        deps = [str(d).strip().lower() for d in deps] if isinstance(deps, list) else []
        steps.append({
            "id": sid,
            "title": clean_text(item.get("title"), MAX_TITLE_CHARS) or instruction[:60],
            "instruction": instruction,
            "type": step_type,
            "engine": engine,
            "model": model,
            "reason": clean_text(item.get("reason"), MAX_REASON_CHARS),
            "depends_on": deps,
        })
    if not steps:
        raise PlanError("Nenhum passo valido no plano.")

    ids = {s["id"] for s in steps}
    for s in steps:
        kept = [d for d in dict.fromkeys(s["depends_on"]) if d in ids and d != s["id"]]
        if len(kept) != len(s["depends_on"]):
            warnings.append(f"Passo {s['id']}: dependencia invalida ignorada.")
        s["depends_on"] = kept
    try:
        _dag_from_steps(steps, "check", "").validate()
    except (CycleDetectedError, DependencyNotFoundError):
        warnings.append("As dependencias formavam um ciclo; os passos vao rodar em sequencia.")
        for i, s in enumerate(steps):
            s["depends_on"] = [steps[i - 1]["id"]] if i else []
    return steps, warnings


def parse_plan(text: str, executors: Iterable[str], max_steps: int,
               models: Optional[Dict[str, List[str]]] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    data = _extract_json(text or "")
    raw = data.get("steps") if isinstance(data, dict) else data
    return normalize_steps(raw, executors, max_steps, models)


def _dag_from_steps(steps: List[Dict[str, Any]], run_id: str, workspace: str) -> TaskDAG:
    dag = TaskDAG()
    for s in steps:
        dag.add_task(AgentTask(
            id=f"{run_id}-{s['id']}",
            title=s["title"],
            instruction=s["instruction"],
            task_type=s["type"],
            assigned_agent=s["engine"] or None,
            assigned_model=s.get("model") or None,
            dependencies=[f"{run_id}-{d}" for d in s["depends_on"]],
            max_retries=1,
            workspace=workspace,
        ))
    return dag


def build_dag(steps: List[Dict[str, Any]], run_id: str, workspace: str) -> TaskDAG:
    dag = _dag_from_steps(steps, run_id, workspace)
    dag.validate()
    return dag


def fallback_single_step(goal: str) -> List[Dict[str, Any]]:
    """Se o planejador falhar, o objetivo inteiro vira um unico passo (o roteador escolhe o motor)."""
    text = clean_text(goal, MAX_INSTRUCTION_CHARS)
    return [{"id": "s1", "title": text[:60], "instruction": text, "type": "coding", "engine": "", "model": "", "reason": "", "depends_on": []}]
