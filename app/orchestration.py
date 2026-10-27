"""Plano multiagente: um modelo PLANEJADOR quebra o objetivo em passos e os demais motores ligados os executam.

Fluxo (duas chamadas, para o humano ver o plano antes de gastar nada):
  1. `build_planning_task`  -> o planejador devolve um JSON com os passos;
  2. `parse_plan`           -> o JSON e tratado como NAO CONFIAVEL: tamanho, motores ligados, dependencias e ciclos
                               sao validados aqui; o que nao serve e descartado, nunca executado;
  3. `build_dag`            -> os passos viram tarefas; o roteador (DLP / modo offline) continua mandando.
"""
from __future__ import annotations
from models.plan_contract import PlanContractModel, PlanStepModel
from app.context_reducer import ContextReducer

import json
import re
import uuid
from typing import Any, Dict, Iterable, List, Optional, Tuple

from adapters.model_args import clean_model
from app.dag_scheduler import CycleDetectedError, DependencyNotFoundError, TaskDAG
from app.engine_profiles import catalog_text, pick_model
from app.project_match import match_project_folders  # noqa: F401  (reexportado)
from models.task import AgentTask

MAX_STEPS_LIMIT = 12
MAX_GOAL_CHARS = 8000
MAX_TITLE_CHARS = 120
MAX_REASON_CHARS = 200
MAX_INSTRUCTION_CHARS = 4000
MAX_CRITERIA = 5
MAX_CRITERION_CHARS = 200
MAX_COMMANDS = 3
MAX_COMMAND_CHARS = 200
STEP_TYPES = {"coding", "research", "review", "test", "docs", "summary"}
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ID = re.compile(r"^[a-z0-9_-]{1,40}$")


class PlanError(ValueError):
    """Plano recusado (mensagem pronta para mostrar ao usuario)."""


def clean_text(value: Any, limit: int) -> str:
    text = _CTRL.sub("", str(value or "")).strip()
    return text[:limit]


_POLICY = None


def _command_allowed(cmd: str) -> Tuple[bool, str]:
    """O comando de validacao passaria na politica de seguranca? Senao o passo falharia SEMPRE (ex.: `python -c ...`)."""
    global _POLICY
    if _POLICY is None:
        from pathlib import Path
        from app.policy_engine import PolicyEngine
        _POLICY = PolicyEngine(str(Path(__file__).resolve().parent.parent / "policies" / "permissions.yaml"))
    try:
        return _POLICY.evaluate_command(cmd, workspace=None)
    except Exception as exc:        # fail-closed: erro na politica recusa o comando
        return False, f"Falha na avaliacao de politica de seguranca (fail-closed): {exc}" 


PLAN_STEP_TIMEOUT = 2700


def build_planning_prompt(goal: str, catalog: List[Dict[str, Any]], max_steps: int, context: str = "",
                          image_paths: Optional[List[str]] = None, folders: Optional[List[str]] = None) -> str:
    """Pedido ao planejador: o objetivo + o CATALOGO de motores e modelos (o que cada um faz bem, se mexe em arquivos,
    se e local/pago e a forca de cada modelo), para ele escolher motor E modelo de cada passo."""
    first = catalog[0]["engine"] if catalog else "ollama"
    return (
        "Voce e o PLANEJADOR de um time de agentes de IA. Quebre o OBJETIVO abaixo em passos pequenos que OUTROS motores "
        "e modelos vao executar. NAO execute nada e NAO escreva arquivos: responda somente com JSON valido, sem texto "
        "antes ou depois, neste formato:\n"
        '{"steps": [{"id": "s1", "title": "titulo curto", "instruction": "o que fazer, completo e autocontido", '
        '"type": "coding|research|review|test|docs|summary", "engine": "<motor>", "model": "<modelo da lista do motor>", '
        '"reason": "por que este motor e este modelo", '
        '"acceptance_criteria": ["resultado verificavel que prova que o passo foi cumprido"], '
        '"validation_commands": ["comando de teste opcional, ex.: python -m pytest -q"], "depends_on": []}]}\n'
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
        "- DIVIDA O TRABALHO: com 2 ou mais motores com acesso a arquivos na lista, cada um deles recebe passos (em plano de 3+ passos, nenhum motor com acesso a arquivos fica sem passo). Ser o motor que planeja NAO te faz o preferido: de ao Antigravity (e aos outros) analise de logs/codigo, revisao independente, testes, pesquisa e documentacao, e deixe os passos mais dificeis de implementacao para o motor que melhor os resolve.\n"
        "- Em PROJETOS GRANDES, oriente o agente a NUNCA varrer a pasta inteira de forma recursiva (trava por muitos minutos em "
        ".venv, node_modules, build, tmp, Backups, APKs, logs e videos de GB): use `git ls-files`/`git grep` ou liste so as pastas "
        "de codigo.\n"
        "- Passos de LEVANTAMENTO/analise em projeto grande devem ser PEQUENOS e por area (ex.: um por pasta de codigo: Bot, Admin, apps), "
        "rodando em paralelo quando possivel; um passo unico 'analise tudo' passa de 15 min. Em analise NAO rode a suite de testes "
        "inteira (so se o passo for sobre testes) e peca que o agente devolva a lista de achados no fim, curta e objetiva.\n"
        "- Pastas citadas que sejam COPIAS DO MESMO REPOSITORIO git (mesmo remote, ex.: edicoes BR e ES) NAO recebem passos "
        "paralelos que facam commit: faca a mudanca versionada UMA vez, em uma pasta, e a outra recebe pelo git; commits "
        "paralelos geram historicos divergentes.\n"
        "- Cada passo deve ser CUMPRIVEL so com o que o motor tem (arquivos da pasta, comandos locais). NAO peca dados que exijam "
        "acesso remoto/credenciais (tamanho de banco em producao, paineis, APIs) como parte de um passo de localizacao/levantamento; "
        "se forem necessarios, faca um passo separado cujo titulo COMECA com 'OPCIONAL:' (se ele falhar, o plano continua). "
        "O ultimo passo de CONSOLIDACAO/relatorio deve listar em 'depends_on' TODOS os passos anteriores, inclusive os OPCIONAIS, "
        "senao o resultado deles se perde; peca que cada passo devolva a saida curta (ate ~40 linhas), pois o consolidador "
        "recebe so o inicio de cada resultado. Use 'depends_on' so quando o "
        "passo realmente precisa da SAIDA do anterior; passos independentes ficam com [] para um problema nao derrubar os outros.\n"
        "- Objetivo de LIMPEZA/MELHORIA de banco de dados: nunca mande apagar/alterar dados direto. Planeje: (1) diagnostico "
        "somente leitura a partir do schema/docs/scripts do repositorio, (2) scripts SQL de limpeza com dry-run (SELECT contando o "
        "que seria afetado) e backup/rollback, SALVOS em arquivo sem executar, (3) revisao. A execucao em producao fica para o usuario aprovar.\n"
        "- Passos de documentacao devem ATUALIZAR so as secoes afetadas (nao reescrever o arquivo todo) e nunca ler arquivos de "
        "segredos (.env, chaves, sessoes).\n"
        "- Use SOMENTE os nomes de motor e de modelo da lista, escritos exatamente como aparecem.\n"
        "- 'acceptance_criteria': 1 a 3 criterios CURTOS e verificaveis por passo (ex.: 'cita arquivo e linha de cada bug', "
        "'pytest termina sem falhas'); o agente recebe esses criterios junto da instrucao.\n"
        "- 'validation_commands' (opcional, so em passos que mudam codigo): ate 3 comandos de teste/lint SEM shell (so python -m pytest, ruff, flake8, mypy; NUNCA python -c, scripts ou pipes: sao bloqueados e reprovam o passo), rodados na "
        "pasta do projeto depois do passo; o passo so e aprovado se todos terminarem com codigo 0. Deixe [] quando nao houver teste.\n"
        f"- No maximo {max_steps} passos; 'depends_on' lista ids de passos anteriores; cada instrucao deve ser entendida sem ver as outras.\n\n"
        + (f"CONTEXTO DA CONVERSA (o que ja foi dito e feito; use para continuar, nao para repetir):\n{context}\n\n" if context else "")
        + (f"IMAGENS ANEXADAS pelo usuario (todos os passos recebem estes arquivos; so alguns motores enxergam imagens, entao "
           f"coloque no passo que precisa delas a instrucao de abrir e descrever o arquivo):\n"
           + "\n".join(f"- {p}" for p in image_paths) + "\n\n" if image_paths else "")
        + (("PASTAS DO PROJETO CITADO NO OBJETIVO (o trabalho e NELAS: use estes caminhos absolutos nas instrucoes e NAO assuma que o "
            "projeto e a pasta do Hera):\n" + "\n".join(f"- {f}" for f in folders) + "\n\n") if folders else
           "PASTA DE TRABALHO: nenhuma pasta de projeto foi citada. Se o objetivo falar de um projeto especifico, peca no primeiro passo "
           "para localizar a pasta dele; so trate a pasta do Hera como alvo se o objetivo for sobre o proprio Hera.\n\n")
        + f"OBJETIVO:\n{goal}"
    )


def rebalance_steps(steps: List[Dict[str, Any]], catalog: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Rede de seguranca contra o vicio do planejador (mandar tudo para o proprio motor): com 3+ passos, todo motor com acesso a
    arquivos que ficou SEM passo recebe alguns, tirados do motor mais carregado (passos que nao sao de codigo primeiro).
    Mantem a forca do modelo (leve/equilibrado/forte); devolve (passos, avisos)."""
    workers = [c["engine"] for c in catalog if c.get("files")]
    if len(steps) < 3 or len(workers) < 2:
        return steps, []
    count = {w: sum(1 for s in steps if s["engine"] == w) for w in workers}
    target = max(1, len(steps) // len(workers))
    strength_of = {(c["engine"], m["id"]): m["strength"] for c in catalog for m in c["models"]}
    notes: List[str] = []
    prefer = {"review": 0, "test": 1, "docs": 2, "summary": 3, "research": 4, "coding": 9}
    for w in workers:
        while count[w] < target:
            donor = max(workers, key=lambda x: count[x])
            if donor == w or count[donor] - 1 < target:
                break
            pool = [s for s in steps if s["engine"] == donor]
            move = min(pool, key=lambda s: (prefer.get(s.get("type", "coding"), 9), -steps.index(s)))
            strength = strength_of.get((donor, move["model"]), "equilibrado")
            move["engine"], move["model"] = w, pick_model(catalog, w, strength) or ""
            move["reason"] = (f"Redistribuido de {donor} para {w}: o plano punha tudo em um motor so. " + move.get("reason", ""))[:MAX_REASON_CHARS]
            count[donor] -= 1
            count[w] += 1
            notes.append(f"O passo '{move['title']}' foi passado de {donor} para {w} para dividir o trabalho entre os motores.")
    return steps, notes


def build_planning_task(goal: str, catalog: List[Dict[str, Any]], max_steps: int, planner_engine: str,
                        planner_model: Optional[str], workspace: str, context: str = "",
                        images: Optional[List[str]] = None, folders: Optional[List[str]] = None) -> AgentTask:
    return AgentTask(
        id=f"plan-{uuid.uuid4().hex[:8]}",
        title="Planejamento multiagente",
        instruction=build_planning_prompt(goal, catalog, max_steps, context, images, folders),
        images=list(images or []),
        task_type="research",
        assigned_agent=planner_engine,
        assigned_model=planner_model or None,
        timeout_seconds=180,
        max_retries=0,
        workspace=workspace,
    )


SYNTH_STEP_CHARS = 8000


def build_synthesis_prompt(goal: str, report: List[Dict[str, Any]]) -> str:
    """Pedido ao planejador, DEPOIS do plano: sintetizar as saidas reduzidas deterministicamente (Anthropic CMA)."""
    blocks = [ContextReducer.reduce_step_output_for_synthesis(r) for r in report]
    return (
        "Voce e o PLANEJADOR. Um time de IAs terminou o plano abaixo. Leia TODAS as saidas e entregue ao usuario o RESUMO FINAL, "
        "em portugues, pronto para uso. NAO execute nada e NAO use ferramentas.\n"
        "Regras:\n"
        "- Se o objetivo pede para ajudar/responder um cliente, comece com o bloco `MENSAGEM PARA O CLIENTE:` com o texto exato a "
        "enviar (curto, humano, sem prometer o que as saidas nao confirmam), escolhendo a melhor versao entre as dos passos.\n"
        "- Depois, `O QUE FAZER:` com os proximos passos para o usuario (no maximo 6 itens objetivos).\n"
        "- Por fim, `ATENCAO:` so com o que e incerto, divergente entre passos ou falhou. Nao invente fatos que nao estejam nas saidas; "
        "se dois passos se contradizem, diga qual voce considerou e por que.\n"
        "- Se nao for caso de cliente, entregue direto a conclusao do objetivo e os achados principais.\n"
        "- Nao repita o que cada passo fez; sintetize. Sem enrolacao.\n\n"
        f"OBJETIVO:\n{goal}\n\nSAIDAS DOS PASSOS:\n" + "\n\n".join(blocks)
    )


def build_synthesis_task(goal: str, report: List[Dict[str, Any]], planner_engine: str,
                         planner_model: Optional[str], workspace: str) -> AgentTask:
    return AgentTask(
        id=f"synth-{uuid.uuid4().hex[:8]}",
        title="Resumo final do plano",
        instruction=build_synthesis_prompt(goal, report),
        task_type="summary",
        assigned_agent=planner_engine,
        assigned_model=planner_model or None,
        timeout_seconds=240,
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
    no_criteria: List[str] = []
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
        raw_crit = item.get("acceptance_criteria") or []
        criteria = [clean_text(c, MAX_CRITERION_CHARS) for c in raw_crit] if isinstance(raw_crit, list) else []
        criteria = [c for c in criteria if c][:MAX_CRITERIA]
        if not criteria:
            no_criteria.append(sid)
        raw_cmds = item.get("validation_commands") or []
        commands = [clean_text(c, MAX_COMMAND_CHARS) for c in raw_cmds] if isinstance(raw_cmds, list) else []
        commands = [c for c in commands if c][:MAX_COMMANDS]
        allowed_cmds = []
        for c in commands:
            ok, why = _command_allowed(c)
            if ok:
                allowed_cmds.append(c)
            else:
                warnings.append(f"Passo {sid}: comando de validacao descartado ({c[:60]}): {why}")
        commands = allowed_cmds
        steps.append({
            "id": sid,
            "title": clean_text(item.get("title"), MAX_TITLE_CHARS) or instruction[:60],
            "instruction": instruction,
            "type": step_type,
            "engine": engine,
            "model": model,
            "reason": clean_text(item.get("reason"), MAX_REASON_CHARS),
            "depends_on": deps,
            "acceptance_criteria": criteria,
            "validation_commands": commands,
        })
    if not steps:
        raise PlanError("Nenhum passo valido no plano.")
    if no_criteria:
        warnings.append(f"Passos sem criterio de aceite ({', '.join(no_criteria)}): so o protocolo BLOQUEADO confere o resultado.")

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


def _dag_from_steps(steps: List[Dict[str, Any]], run_id: str, workspace: str,
                    extra_dirs: Optional[List[str]] = None, images: Optional[List[str]] = None) -> TaskDAG:
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
            timeout_seconds=PLAN_STEP_TIMEOUT,       # o teto antigo (900 s) matava analises produtivas; travamento e pego pelo limite de silencio
            acceptance_criteria=list(s.get("acceptance_criteria") or []),
            validation_commands=list(s.get("validation_commands") or []),
            workspace=workspace,
            extra_dirs=list(extra_dirs or []),
            images=list(images or []),
        ))
    return dag


def build_dag(steps: List[Dict[str, Any]], run_id: str, workspace: str,
              extra_dirs: Optional[List[str]] = None, images: Optional[List[str]] = None) -> TaskDAG:
    dag = _dag_from_steps(steps, run_id, workspace, extra_dirs, images)
    dag.validate()
    return dag


def fallback_single_step(goal: str) -> List[Dict[str, Any]]:
    """Se o planejador falhar, o objetivo inteiro vira um unico passo (o roteador escolhe o motor)."""
    text = clean_text(goal, MAX_INSTRUCTION_CHARS)
    return [{"id": "s1", "title": text[:60], "instruction": text, "type": "coding", "engine": "", "model": "", "reason": "", "depends_on": []}]


def validate_plan_contract(steps: List[Dict[str, Any]], strategy: str = "balanced") -> PlanContractModel:
    """Valida formalmente a lista de passos contra o contrato Pydantic PlanContractModel."""
    return PlanContractModel.model_validate({"strategy": strategy, "steps": steps})
