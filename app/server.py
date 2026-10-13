import os
import re
import sys
import logging
import json
import time
import uuid
import asyncio
import psutil
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Literal, Optional, Union

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StringConstraints
from typing_extensions import Annotated

from app.config_manager import ConfigManager
from app.security import LocalSecurityMiddleware, SecuritySettings
from app.security.paths import PathNotAllowedError, ensure_inside, ensure_safe_relative_list
from app.security.human_code import HumanApprovalCode, HumanCodeLockedError, load_or_create_secret
from app.security.data_dir import data_dir
from models.task import AgentTask, TaskStatus, AgentResult
from models.agent import HealthStatus
from app.state_store import StateStore
from app.coordinator import Coordinator
from app.router import TaskRouter
from app.obsidian import ObsidianVaultWriter
from app.vault_memory import ObsidianMemoryManager
from app.validator import TaskValidator
from app.auto_fix import AutoFixEngine
from app.dag_scheduler import TaskDAG, DAGScheduler
from app import orchestration
from app.services_control import SERVICES, ServiceManager, port_open
from app.vault_manager import VaultManager, VaultError, VaultConflict
from app.locations import find_vault_root, resolve_vault_dir
from adapters.ollama import OllamaAdapter
from adapters.claude_code import ClaudeCodeAdapter
from adapters.antigravity import AntigravityAdapter

from app.gateway.schemas import ChatCompletionRequest, ChatCompletionResponse, ChatMessage
from app.gateway.presets import PROVIDER_PRESETS, get_preset
from app.gateway.key_manager import load_keys_from_env
from app.gateway.secret_store import SecretStore, SecretStoreError
from app.gateway.usage import UsageLedger
from adapters.hera_router import HeraRouterAdapter
from app.gateway.privacy import GatewayPrivacyManager
from app.gateway.providers import OllamaGatewayAdapter, GatewayProviderError
from app.gateway.provider_policy import SSRFSecurityError, validate_provider_url
from app.gateway.safe_transport import make_safe_async_client
from app.gateway.factory import build_gateway_adapters
from app.gateway.tier_router import TierRouter, CLIENT_NAME, REQUEST_ID, STRATEGIES
from app.gateway.client_keys import ClientKeyStore

from app.hitl.schemas import (
    ApprovalRequest,
    RejectRequest,
    RequestChangesRequest,
    DiffProposal,
    MergeResult,
)
from app.hitl.service import HitlService
from app.hitl.git_manager import GitManager
from app.hitl.diff_analyzer import DiffAnalyzer
from app.hitl.approval_engine import ApprovalEngine

from app.mcp.schemas import (
    McpServerConfig,
    McpToolDefinition,
    McpToolCallRequest,
    McpToolCallResult,
)
from app.mcp.catalog import McpCatalog, McpSecurityError, McpPermissionDeniedError
from app.mcp.launch import McpLaunchError, validate_stdio_server
from app.mcp.transport import McpTransportError
from app.mcp.client import McpClientManager

from app.arena.schemas import (
    ArenaCandidate,
    CandidateResult,
    ArenaRunRequest,
    ArenaRunReport,
)
from app.arena.runner import ArenaRunner
from contextlib import asynccontextmanager
import httpx

gateway_http_client: Optional[httpx.AsyncClient] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global gateway_http_client
    gateway_http_client = make_safe_async_client(timeout=60.0)   # valida o destino na conexão (anti-rebinding)
    yield
    await mcp_client.close_all()                              # encerra os servidores MCP lançados
    if gateway_http_client:
        await gateway_http_client.aclose()

app = FastAPI(title="Hera Multiagent API", version="1.0.0", lifespan=lifespan)

# Seguranca local (fase 1, 12/10/2026): token + Host/Origin + limite de taxa, e NENHUM CORS.
# Antes era `allow_origins=["*"]` sem autenticacao: qualquer site aberto no navegador podia
# chamar esta API em 127.0.0.1. A UI e servida pela propria origem, entao nao precisa de CORS.
# O token fica em data/.hera_token (ou HERA_API_KEY); a UI o recebe embutido em "/".
DATA_DIR = data_dir()
SECURITY = SecuritySettings.from_env(DATA_DIR / ".hera_token")
app.state.security = SECURITY
CLIENT_KEYS = ClientKeyStore(DATA_DIR / ".hera_client_keys.json")        # chaves de acesso de ferramentas externas (so /v1)
app.add_middleware(LocalSecurityMiddleware, settings=SECURITY, client_keys=CLIENT_KEYS)

config_mgr = ConfigManager()
cfg = config_mgr.load_config()

# Diretórios
ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"
WEB_DIR.mkdir(parents=True, exist_ok=True)

# Inicializar adaptadores
adapters = {
    "ollama": OllamaAdapter(
        base_url=cfg["engines"]["ollama"].get("base_url", "http://localhost:11434"),
        default_model=cfg["engines"]["ollama"].get("default_model", "qwen2.5-coder:1.5b")
    ),
    "claude_code": ClaudeCodeAdapter(),
    "antigravity": AntigravityAdapter()
}

store = StateStore(db_path=cfg["general"].get("db_path", "data/orchestrator.db"))
router = TaskRouter(adapters=adapters, default_offline=cfg["general"].get("default_offline", False))
validator = TaskValidator()

vault_dir = cfg["general"].get("vault_dir")
vault_writer = ObsidianVaultWriter(vault_project_dir=vault_dir)
memory_manager = ObsidianMemoryManager(vault_project_dir=vault_dir)

# Inicializar Gateway Roteador em camadas
gateway_adapters = build_gateway_adapters(cfg)     # Ollama + provedores de nuvem ligados (chaves via env)
ollama_gw_adapter = gateway_adapters[0]
gateway_privacy_mgr = GatewayPrivacyManager(default_offline=cfg["general"].get("default_offline", False))
gateway_tier_router = TierRouter(
    adapters=gateway_adapters,
    privacy_manager=gateway_privacy_mgr,
    max_concurrent_requests=cfg.get("gateway", {}).get("max_concurrent_requests", 10),
    total_timeout_seconds=float(cfg.get("gateway", {}).get("total_timeout_seconds", 180.0)),
    tier_order=cfg.get("gateway", {}).get("default_tier_order"),
    stream_idle_timeout_seconds=float(cfg.get("gateway", {}).get("stream_idle_timeout_seconds", 60.0)),
    stream_total_timeout_seconds=float(cfg.get("gateway", {}).get("stream_total_timeout_seconds", 900.0)),
)



RUNTIME_FLAGS = {"rtk": bool(cfg.get("feature_flags", {}).get("enable_rtk", False))}


async def _gateway_call(req: ChatCompletionRequest, only_provider: Optional[str] = None,
                        label: str = "hera (motor interno)") -> ChatCompletionResponse:
    """Chamada em memoria ao gateway (usada pelo motor `hera_router` e pelo teste de provedor)."""
    client = gateway_http_client
    owns = client is None or client.is_closed
    if owns:
        client = make_safe_async_client(timeout=60.0)
    t1, t2 = CLIENT_NAME.set(label), REQUEST_ID.set(f"req-{uuid.uuid4().hex[:10]}")
    try:
        return await gateway_tier_router._route_and_execute(
            req, client, force_offline=router.default_offline, enable_rtk=RUNTIME_FLAGS["rtk"], only_provider=only_provider)
    finally:
        CLIENT_NAME.reset(t1)
        REQUEST_ID.reset(t2)
        if owns:
            await client.aclose()


def _router_models() -> List[str]:
    names = list(gateway_tier_router.combos)
    for a in gateway_tier_router.adapters:
        names += sorted(a.served_models or [])
    return list(dict.fromkeys(names))


adapters["hera_router"] = HeraRouterAdapter(
    call=_gateway_call, models=_router_models,
    has_cloud=lambda: any(not a.is_local for a in gateway_tier_router.adapters),
    default_model=cfg["engines"].get("hera_router", {}).get("default_model", "default"))

# Inicializar Serviço HITL (Human-in-the-Loop)
# Código de aprovação humana (fase 2): segredo persistente; o código aparece só no console / CLI.
HUMAN_CODE = HumanApprovalCode(load_or_create_secret(DATA_DIR / ".hera_approval_secret"))
OPERATOR_ID = "local-operator"      # identidade da aprovação: decidida pelo servidor, nunca pelo cliente
hitl_service = HitlService(
    state_store=store,
    obsidian_writer=vault_writer,
    approval_engine=ApprovalEngine(
        secret_key=load_or_create_secret(DATA_DIR / ".hera_hmac_secret").decode("utf-8"),
        state_path=DATA_DIR / ".hera_nonces.json",
    ),
)
_last_code_announce = 0.0


def _announce_human_code(reason: str) -> None:
    """Mostra o código no CONSOLE do servidor (no máximo 1x a cada 30 s). Quem só tem o token da
    API nunca vê o console; o humano que pediu a ação vê e digita na UI."""
    global _last_code_announce
    now = time.monotonic()
    if now - _last_code_announce < 30:
        return
    _last_code_announce = now
    line = (f"[HERA] {reason}. Código de aprovação humana: {HUMAN_CODE.current()} "
            f"(válido por ~{HUMAN_CODE.seconds_left()}s; ou rode: python main.py approval-code)")
    try:
        print("\n🔑 " + line + "\n", flush=True)
    except UnicodeEncodeError:
        print("\n" + line.encode("ascii", "replace").decode("ascii") + "\n", flush=True)


def _require_human_code(code: Optional[str], reason: str) -> None:
    """403 sem código válido; 429 se bloqueado por tentativas erradas."""
    if not code:
        _announce_human_code(reason)
        raise HTTPException(status_code=403, detail={
            "error": "human_code_required",
            "message": "Digite o código de aprovação exibido no console do servidor "
                       "(ou rode `python main.py approval-code`)."})
    try:
        valid = HUMAN_CODE.verify(code)
    except HumanCodeLockedError as exc:
        raise HTTPException(status_code=429, detail={"error": "human_code_locked", "message": str(exc)},
                            headers={"Retry-After": str(exc.retry_after)})
    if not valid:
        _announce_human_code(reason)
        raise HTTPException(status_code=403, detail={"error": "human_code_invalid",
                                                     "message": "Código de aprovação incorreto ou expirado."})

# Inicializar MCP Client Manager e Catálogo
mcp_catalog = McpCatalog(allowed_commands=cfg.get("mcp", {}).get("allowed_commands"))
mcp_client = McpClientManager(
    catalog=mcp_catalog,
    privacy_manager=gateway_privacy_mgr,
    enabled=cfg.get("feature_flags", {}).get("enable_mcp", True),
    roots_provider=lambda: _allowed_repo_roots(),          # scripts de servidores MCP só dentro destas raízes
)

# Inicializar Multi-Model Arena Runner
arena_runner = ArenaRunner(
    adapters=adapters,
    privacy_manager=gateway_privacy_mgr,
    state_store=store,
    enabled=cfg.get("feature_flags", {}).get("enable_arena", True)
)

# Gerenciador de conexões WebSocket para logs ao vivo
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: Dict[str, Any]):
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)

ws_manager = ConnectionManager()

def broadcast_log(msg: str, level: str = "info"):
    timestamp = time.strftime("%H:%M:%S")
    payload = {
        "type": "log",
        "timestamp": timestamp,
        "message": msg,
        "level": level
    }
    # Criar task assíncrona segura no event loop ativo
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(ws_manager.broadcast(payload))
    except RuntimeError:
        pass

ENGINE_ADAPTER_NAMES = {"ollama": "ollama", "claude_code": "claude_code", "antigravity": "antigravity",
                        "hera_router": "hera_router"}


def _apply_engine_runtime(config: Dict[str, Any]) -> None:
    """Liga/desliga motores e aplica o modelo padrao de cada um no roteador, sem reiniciar."""
    disabled, models = set(), {}
    for key, eng in (config.get("engines") or {}).items():
        name = ENGINE_ADAPTER_NAMES.get(key)
        if not name or not isinstance(eng, dict):
            continue
        if eng.get("enabled") is False:
            disabled.add(name)
        if eng.get("default_model"):
            models[name] = eng["default_model"]
    router.disabled = disabled
    router.default_models = models


_apply_engine_runtime(cfg)

coordinator = Coordinator(store=store, on_progress=broadcast_log)
autofix = AutoFixEngine(
    router=router,
    coordinator=coordinator,
    validator=validator,
    on_feedback=broadcast_log
)

# Schemas Pydantic
# Limites de entrada (fase 3): a API nao aceita corpo ilimitado nem comando gigante.
ShortStr = Annotated[str, StringConstraints(max_length=300)]
CriterionStr = Annotated[str, StringConstraints(max_length=500)]


class TaskRunRequest(BaseModel):
    title: str = Field("", max_length=200)
    instruction: str = Field(max_length=20000)
    task_type: str = Field("coding", max_length=50)
    assigned_agent: Optional[str] = Field(None, max_length=50)
    assigned_model: Optional[str] = Field(None, max_length=200)
    acceptance_criteria: List[CriterionStr] = Field(default_factory=list, max_length=50)
    validation_commands: List[ShortStr] = Field(default_factory=list, max_length=10)
    max_retries: int = Field(2, ge=0, le=5)
    save_to_obsidian: bool = True

class ModelPullRequest(BaseModel):
    model: str

class ConfigUpdateRequest(BaseModel):
    general: Dict[str, Any]
    engines: Dict[str, Any]

# Endpoints REST

@app.get("/api/health")
async def get_health():
    """Retorna saúde e modelos disponíveis de cada motor."""
    results = {}
    for name, adapter in adapters.items():
        try:
            status: HealthStatus = await adapter.health_check()
            results[name] = {
                "enabled": name not in router.disabled,
                "default_model": router.default_models.get(name),
                "healthy": status.healthy,
                "version": status.version,
                "available_models": status.available_models,
                "error_message": status.error_message
            }
        except Exception as e:
            results[name] = {
                "enabled": name not in router.disabled,
                "default_model": router.default_models.get(name),
                "healthy": False,
                "version": None,
                "available_models": [],
                "error_message": str(e)
            }
    return {"engines": results, "default_offline": router.default_offline}

@app.get("/api/config")
async def get_config():
    """Retorna a configuração atual de config.yaml."""
    return config_mgr.load_config()

ALLOWED_GENERAL_KEYS = {"host", "port", "default_offline", "default_engine", "vault_dir", "vault_root", "db_path",
                        "allowed_repo_roots"}
# Ajustes de INICIALIZACAO: a API nao pode muda-los (so editando o config.yaml com o servidor parado).
FROZEN_GENERAL_KEYS = ("host", "port", "vault_dir", "vault_root", "db_path", "allowed_repo_roots")
ALLOWED_ENGINES = {"ollama", "claude_code", "antigravity", "hera_router"}
ALLOWED_ENGINE_FIELDS = {"enabled", "base_url", "default_model", "timeout"}
LOCAL_ONLY_URL_ENGINES = {"ollama"}


def _validate_config_update(req: "ConfigUpdateRequest") -> None:
    """Fase 3: a configuracao enviada pela API e validada (chaves conhecidas, URLs locais,
    ajustes de inicializacao imutaveis). Levanta HTTPException 400."""
    def bad(msg: str):
        raise HTTPException(status_code=400, detail={"error": "invalid_config", "message": msg})

    current = config_mgr.load_config()
    unknown = set(req.general) - ALLOWED_GENERAL_KEYS
    if unknown:
        bad(f"Chaves desconhecidas em 'general': {sorted(unknown)}")
    for key in FROZEN_GENERAL_KEYS:
        if key in req.general and req.general[key] != current["general"].get(key):
            bad(f"'general.{key}' so pode ser alterado editando o config.yaml com o servidor parado")
    if not isinstance(req.general.get("default_offline", False), bool):
        bad("'general.default_offline' precisa ser booleano")
    if req.general.get("default_engine") not in (None, *ALLOWED_ENGINES):
        bad("'general.default_engine' desconhecido")

    for name, eng in req.engines.items():
        if name not in ALLOWED_ENGINES:
            bad(f"Motor desconhecido: {name!r}")
        if not isinstance(eng, dict):
            bad(f"Configuracao do motor {name!r} precisa ser um objeto")
        extra = set(eng) - ALLOWED_ENGINE_FIELDS
        if extra:
            bad(f"Campos desconhecidos em engines.{name}: {sorted(extra)}")
        if "enabled" in eng and not isinstance(eng["enabled"], bool):
            bad(f"engines.{name}.enabled precisa ser booleano")
        model = eng.get("default_model")
        if model is not None and (not isinstance(model, str) or len(model) > 200
                                  or any(ord(c) < 32 for c in model)):
            bad(f"engines.{name}.default_model invalido")
        timeout = eng.get("timeout")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                                    or not 1 <= timeout <= 3600):
            bad(f"engines.{name}.timeout precisa estar entre 1 e 3600")
        url = eng.get("base_url")
        if url is not None:
            if name not in LOCAL_ONLY_URL_ENGINES:
                bad(f"engines.{name} nao usa base_url")
            try:
                validate_provider_url(url, is_local_profile=True)       # so loopback + portas locais
            except (SSRFSecurityError, ValueError) as exc:
                bad(f"engines.{name}.base_url rejeitada: {exc}")


@app.post("/api/config")
async def update_config(req: ConfigUpdateRequest):
    """Salva novas configurações no disco e atualiza em tempo de execução."""
    _validate_config_update(req)
    current = config_mgr.load_config()
    # Mescla com o que ja existe: antes so `general` e `engines` eram regravados e `feature_flags`
    # e `gateway` sumiam do config.yaml a cada clique (ex.: alternar o modo offline).
    new_cfg = {**current, "general": {**current["general"], **req.general},
               "engines": {**current["engines"], **req.engines}}
    if current["general"].get("default_offline") and not new_cfg["general"].get("default_offline"):
        logging.getLogger("hera.audit").warning("Modo offline DESATIVADO via API (DLP volta a permitir nuvem).")
        broadcast_log("🔓 [AUDITORIA] Modo offline foi DESATIVADO: o roteador pode voltar a usar a nuvem.",
                      level="warning")
    config_mgr.save_config(new_cfg)
    
    # Atualizar modo offline no roteador
    router.default_offline = new_cfg["general"].get("default_offline", False)   # sem `general` na requisicao, mantem
    _apply_engine_runtime(new_cfg)
    
    # Atualizar defaults nos adaptadores
    if "ollama" in req.engines and "ollama" in adapters:
        adapters["ollama"].default_model = req.engines["ollama"].get("default_model", "qwen2.5-coder:1.5b")
        
    broadcast_log("⚙️ Configurações atualizadas e sincronizadas com sucesso.", level="success")
    return {"status": "ok", "config": new_cfg}

# ---------------------------------------------------------------------------------------------
# Plano multiagente: um modelo PLANEJADOR divide o objetivo e os motores ligados executam os passos
# ---------------------------------------------------------------------------------------------
class PlannerChoice(BaseModel):
    engine: ShortStr
    model: Annotated[str, StringConstraints(max_length=200)] = ""


class PlanRequest(BaseModel):
    goal: Annotated[str, StringConstraints(min_length=3, max_length=orchestration.MAX_GOAL_CHARS)]
    executors: Optional[List[ShortStr]] = Field(default=None, max_length=8)


class PlanStepModel(BaseModel):
    id: Annotated[str, StringConstraints(max_length=40)] = ""
    title: Annotated[str, StringConstraints(max_length=orchestration.MAX_TITLE_CHARS)] = ""
    instruction: Annotated[str, StringConstraints(max_length=orchestration.MAX_INSTRUCTION_CHARS)]
    type: Annotated[str, StringConstraints(max_length=20)] = "coding"
    engine: Annotated[str, StringConstraints(max_length=40)] = ""
    depends_on: List[Annotated[str, StringConstraints(max_length=40)]] = Field(default_factory=list, max_length=12)


class PlanExecuteRequest(BaseModel):
    goal: Annotated[str, StringConstraints(max_length=orchestration.MAX_GOAL_CHARS)] = ""
    steps: List[PlanStepModel] = Field(min_length=1, max_length=orchestration.MAX_STEPS_LIMIT)
    executors: Optional[List[ShortStr]] = Field(default=None, max_length=8)


def _orchestration_cfg() -> Dict[str, Any]:
    section = config_mgr.load_config().get("orchestration") or {}
    planner = section.get("planner") or {}
    return {"planner": {"engine": planner.get("engine") or "ollama", "model": planner.get("model") or ""},
            "max_steps": int(section.get("max_steps") or 8)}


def _active_executors(requested: Optional[List[str]] = None) -> List[str]:
    active = list(router.active_adapters())
    if requested is not None:
        active = [name for name in active if name in set(requested)]
    if not active:
        raise HTTPException(status_code=400, detail={
            "error": "no_executor", "message": "Nenhum motor ligado para executar o plano. Ligue um em Motores & IAs."})
    return active


@app.get("/api/orchestration")
async def get_orchestration():
    """Planejador escolhido, motores ligados e se o modo offline vai forcar tudo para o Ollama."""
    ocfg = _orchestration_cfg()
    return {**ocfg, "active_engines": list(router.active_adapters()), "offline": router.default_offline}


@app.post("/api/orchestration/planner")
async def set_planner(req: PlannerChoice):
    engine = ENGINE_ADAPTER_NAMES.get(req.engine, req.engine)
    if engine not in adapters:
        raise HTTPException(status_code=400, detail={"error": "invalid_planner", "message": f"Motor desconhecido: {req.engine!r}"})
    if engine in router.disabled:
        raise HTTPException(status_code=400, detail={"error": "invalid_planner",
                                                     "message": "Esse motor esta desligado; ligue-o antes de escolhe-lo como planejador."})
    model = orchestration.clean_text(req.model, 200)
    current = config_mgr.load_config()
    section = {**(current.get("orchestration") or {}), "planner": {"engine": engine, "model": model}}
    config_mgr.save_config({**current, "orchestration": section})
    broadcast_log(f"🧠 Planejador definido: {engine}{' / ' + model if model else ''}.", level="success")
    return {"status": "ok", "planner": section["planner"]}


@app.post("/api/orchestration/plan")
async def make_plan(req: PlanRequest):
    """Pede ao planejador um plano em passos. NADA e executado aqui: o usuario revisa e depois chama /execute."""
    ocfg = _orchestration_cfg()
    executors = _active_executors(req.executors)
    planner_engine = ocfg["planner"]["engine"]
    if planner_engine in router.disabled or planner_engine not in adapters:
        raise HTTPException(status_code=400, detail={"error": "planner_off",
                                                     "message": f"O planejador '{planner_engine}' esta desligado ou nao existe."})
    task = orchestration.build_planning_task(req.goal, executors, ocfg["max_steps"], planner_engine,
                                             ocfg["planner"]["model"], str(ROOT_DIR))
    try:
        adapter, model, reason = await router.route(task)       # o Gate DLP / modo offline continua valendo
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail={"error": "blocked", "message": str(exc)})
    forced = adapter.name != planner_engine
    task.assigned_agent, task.assigned_model = adapter.name, model
    warnings: List[str] = []
    if forced:
        warnings.append(f"O planejador escolhido ({planner_engine}) nao pode ver este pedido: {reason}")
    try:
        handle = await adapter.submit(task)
        result = await asyncio.wait_for(adapter.collect(handle), timeout=task.timeout_seconds + 20)
        if result.status != TaskStatus.SUCCEEDED:
            raise orchestration.PlanError(result.error_message or result.summary or "o planejador falhou")
        steps, plan_warnings = orchestration.parse_plan(result.output_text, executors, ocfg["max_steps"])
        warnings += plan_warnings
    except Exception as exc:        # noqa: BLE001 - qualquer falha do planejador cai no plano de 1 passo
        warnings.append(f"Plano automatico indisponivel ({type(exc).__name__}: {str(exc)[:200]}); usando 1 passo com o objetivo inteiro.")
        steps, _ = orchestration.normalize_steps(orchestration.fallback_single_step(req.goal), executors, 1)
    return {"planner": {"engine": adapter.name, "model": model, "forced_local": forced},
            "steps": steps, "warnings": warnings, "executors": executors, "offline": router.default_offline}


@app.post("/api/orchestration/execute")
async def execute_plan(req: PlanExecuteRequest, background_tasks: BackgroundTasks):
    """Executa o plano (revalidado aqui: o corpo e nao confiavel) distribuindo os passos aos motores ligados."""
    ocfg = _orchestration_cfg()
    executors = _active_executors(req.executors)
    try:
        steps, warnings = orchestration.normalize_steps([s.model_dump() for s in req.steps], executors, ocfg["max_steps"])
    except orchestration.PlanError as exc:
        raise HTTPException(status_code=400, detail={"error": "invalid_plan", "message": str(exc)})
    run_id = f"run-{uuid.uuid4().hex[:8]}"
    dag = orchestration.build_dag(steps, run_id, str(ROOT_DIR))

    async def _execute():
        broadcast_log(f"🧭 Plano {run_id}: {len(steps)} passo(s) em execucao.", level="info")
        try:
            scheduler = DAGScheduler(router=router, coordinator=coordinator, validator=validator,
                                     max_concurrency=2, on_feedback=broadcast_log)
            results = await scheduler.execute_dag(dag)
            report = []
            for step in steps:
                tid = f"{run_id}-{step['id']}"
                res, tsk = results.get(tid), dag.tasks[tid]
                report.append({"id": step["id"], "title": step["title"], "engine": tsk.assigned_agent or "",
                               "model": tsk.assigned_model or "",
                               "status": res.status.value if res else "CANCELLED",
                               "summary": (res.summary if res else "")[:500],
                               "output_text": (res.output_text if res else "")[:20000]})
            await ws_manager.broadcast({"type": "orchestration_finished", "run_id": run_id, "steps": report})
        except Exception as exc:        # noqa: BLE001
            broadcast_log(f"❌ Plano {run_id} falhou: {exc}", level="error")
            await ws_manager.broadcast({"type": "orchestration_finished", "run_id": run_id, "steps": [],
                                        "error": str(exc)[:500]})

    background_tasks.add_task(_execute)
    return {"status": "enqueued", "run_id": run_id, "steps": steps, "warnings": warnings}


# ---------------------------------------------------------------------------------------------
# Servico local (Ollama): iniciar e parar pelo painel. Comandos FIXOS (ver services_control).
# ---------------------------------------------------------------------------------------------
service_manager = ServiceManager(DATA_DIR / "logs")


def _service_or_404(name: str) -> str:
    if name not in SERVICES:
        raise HTTPException(status_code=404, detail={"error": "unknown_service", "message": "Servico desconhecido."})
    return name


@app.get("/api/services")
async def list_services():
    return {"services": await asyncio.to_thread(service_manager.all_status)}


@app.post("/api/services/{name}/start")
async def start_service(name: str):
    _service_or_404(name)
    try:
        info = await asyncio.to_thread(service_manager.start, name)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"error": "not_installed", "message": str(exc)})
    except OSError as exc:
        raise HTTPException(status_code=500, detail={"error": "start_failed", "message": f"Nao foi possivel iniciar: {exc}"})
    if not info["running"]:
        ready = await asyncio.to_thread(service_manager.wait_until_up, name, 25.0)
        info = await asyncio.to_thread(service_manager.status, name)
        info["message"] = f"{info['label']} no ar." if ready else f"{info['label']} nao respondeu a tempo."
    broadcast_log(f"🔌 [SERVICOS] {info['message']}", level="success" if info["running"] else "warning")
    return info


@app.post("/api/services/{name}/stop")
async def stop_service(name: str):
    _service_or_404(name)
    try:
        info = await asyncio.to_thread(service_manager.stop, name)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail={"error": "not_managed", "message": str(exc)})
    broadcast_log(f"🔌 [SERVICOS] {info['message']}", level="info")
    return info


@app.post("/api/ollama/pull")
async def pull_ollama_model(req: ModelPullRequest, background_tasks: BackgroundTasks):
    """Inicia download de um modelo pelo Ollama com telemetria via WebSocket."""
    model_name = req.model.strip()
    if not model_name:
        raise HTTPException(status_code=400, detail="Nome do modelo não informado")

    async def _do_pull():
        broadcast_log(f"📥 [OLLAMA] Iniciando download do modelo '{model_name}'...", level="info")
        import httpx
        try:
            async with httpx.AsyncClient(timeout=600.0) as client:
                async with client.stream("POST", f"{adapters['ollama'].base_url}/api/pull", json={"name": model_name, "stream": True}) as response:
                    async for line in response.aiter_lines():
                        if line:
                            try:
                                data = json.loads(line)
                                status = data.get("status", "")
                                total = data.get("total", 0)
                                completed = data.get("completed", 0)
                                pct = (completed / total * 100) if total else 0
                                
                                await ws_manager.broadcast({
                                    "type": "pull_progress",
                                    "model": model_name,
                                    "status": status,
                                    "percent": round(pct, 1)
                                })
                            except Exception:
                                pass
            broadcast_log(f"✅ [OLLAMA] Modelo '{model_name}' baixado e pronto para uso!", level="success")
            await ws_manager.broadcast({"type": "pull_completed", "model": model_name})
        except Exception as e:
            broadcast_log(f"❌ [OLLAMA] Falha no download de '{model_name}': {e}", level="error")

    background_tasks.add_task(_do_pull)
    return {"status": "started", "model": model_name}

@app.get("/api/hardware")
async def get_hardware():
    """Telemetria ao vivo de GPU, CPU e Memória RAM."""
    cpu_pct = psutil.cpu_percent(interval=None)
    ram = psutil.virtual_memory()

    gpu_info = {
        "available": False,
        "name": "Nenhuma GPU detectada",
        "vram_used_mb": 0,
        "vram_total_mb": 0,
        "utilization_pct": 0,
        "temperature_c": 0
    }

    try:
        cmd = [
            "nvidia-smi",
            "--query-gpu=name,memory.used,memory.total,utilization.gpu,temperature.gpu",
            "--format=csv,noheader,nounits"
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=2.0)
        if proc.returncode == 0 and proc.stdout.strip():
            parts = [p.strip() for p in proc.stdout.strip().split(",")]
            if len(parts) >= 5:
                gpu_info = {
                    "available": True,
                    "name": parts[0],
                    "vram_used_mb": int(float(parts[1])),
                    "vram_total_mb": int(float(parts[2])),
                    "utilization_pct": int(float(parts[3])),
                    "temperature_c": int(float(parts[4]))
                }
    except Exception:
        pass

    return {
        "cpu_pct": cpu_pct,
        "ram_used_gb": round(ram.used / (1024**3), 1),
        "ram_total_gb": round(ram.total / (1024**3), 1),
        "ram_pct": ram.percent,
        "gpu": gpu_info
    }

@app.get("/api/tasks")
async def list_tasks(limit: int = 20):
    """Lista tarefas recentes registradas no SQLite WAL."""
    tasks = store.list_tasks(limit=limit)
    return [
        {
            "id": t.id,
            "title": t.title,
            "status": t.status.value,
            "assigned_agent": t.assigned_agent,
            "assigned_model": t.assigned_model,
            "created_at": t.created_at,
            "updated_at": t.updated_at
        }
        for t in tasks
    ]

@app.post("/api/tasks/run")
async def run_task(req: TaskRunRequest, background_tasks: BackgroundTasks):
    """Executa uma tarefa assíncrona com auto-validação e streaming de logs."""
    # Politica ANTES de enfileirar (fase 3): comando fora da allowlist e recusado com 400 aqui,
    # em vez de so falhar la na frente, dentro da tarefa. O validador reaplica a mesma regra.
    for cmd in req.validation_commands:
        allowed, reason = validator.policy_engine.evaluate_command(cmd, workspace=str(ROOT_DIR))
        if not allowed:
            raise HTTPException(status_code=400, detail={
                "error": "validation_command_blocked", "command": cmd, "reason": reason})

    task_id = f"task-{uuid.uuid4().hex[:8]}"
    title = req.title.strip() or req.instruction[:40]

    task = AgentTask(
        id=task_id,
        title=title,
        instruction=req.instruction,
        task_type=req.task_type,
        assigned_agent=req.assigned_agent,
        assigned_model=req.assigned_model,
        acceptance_criteria=req.acceptance_criteria,
        validation_commands=req.validation_commands,
        max_retries=req.max_retries,
        workspace=str(ROOT_DIR)
    )

    async def _execute():
        broadcast_log(f"🚀 Iniciando tarefa: {task.title} ({task.id})", level="info")
        await ws_manager.broadcast({"type": "task_started", "task_id": task.id, "title": task.title})
        try:
            res: AgentResult = await autofix.execute_task_with_retry(task, workspace=str(ROOT_DIR))
            
            # Persistir no Obsidian se solicitado
            if req.save_to_obsidian and res.status == TaskStatus.SUCCEEDED:
                try:
                    note = vault_writer.record_execution(task=task, result=res, validation_items=task.acceptance_criteria)
                    broadcast_log(f"📝 [OBSIDIAN] Nota persistida no Vault: {os.path.basename(note)}", level="success")
                except Exception as ex:
                    broadcast_log(f"⚠️ [OBSIDIAN] Erro ao gravar nota: {ex}", level="warning")

            await ws_manager.broadcast({
                "type": "task_finished",
                "task_id": task.id,
                "status": res.status.value,
                "summary": res.summary,
                "output_text": res.output_text
            })
        except Exception as e:
            broadcast_log(f"❌ Erro fatal na execução de {task.id}: {e}", level="error")
            await ws_manager.broadcast({
                "type": "task_finished",
                "task_id": task.id,
                "status": "failed",
                "summary": str(e),
                "output_text": ""
            })

    background_tasks.add_task(_execute)
    return {"status": "enqueued", "task_id": task_id, "title": title}

@app.get("/api/vault/search")
async def search_vault(q: str):
    """Busca semântica no Obsidian Vault."""
    if not q.strip():
        return []
    return memory_manager.search_context(q.strip(), max_notes=6)

# ---------------------------------------------------------------------------------------------
# Obsidian dentro do Hera: explorar, ler, escrever, renomear (com links), lixeira, busca, tags, grafo, IA
# (as regras de seguranca estao em app/vault_manager.py)
# ---------------------------------------------------------------------------------------------
VAULT = VaultManager(find_vault_root(resolve_vault_dir(cfg["general"].get("vault_dir")),
                                     cfg["general"].get("vault_root")))


async def _vault(fn, *args, **kwargs):
    """Roda uma operacao do cofre fora do loop e converte os erros do cofre em HTTP."""
    if not VAULT.exists():
        raise HTTPException(status_code=503, detail={"error": "vault_missing",
                                                     "message": "O cofre do Obsidian nao foi encontrado. Ajuste `general.vault_root` no config.yaml."})
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except VaultConflict as exc:
        raise HTTPException(status_code=409, detail={"error": "conflict", "message": str(exc), "current_hash": exc.current_hash})
    except VaultError as exc:
        raise HTTPException(status_code=exc.status, detail={"error": "vault", "message": str(exc)})
    except OSError as exc:
        raise HTTPException(status_code=500, detail={"error": "vault_io", "message": f"Erro de disco: {exc.strerror or type(exc).__name__}"})


class VaultCreateBody(BaseModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=600)]
    content: Annotated[str, StringConstraints(max_length=2_200_000)] = ""


class VaultWriteBody(VaultCreateBody):
    base_hash: Optional[Annotated[str, StringConstraints(max_length=64)]] = None
    force: bool = False


class VaultPathBody(BaseModel):
    path: Annotated[str, StringConstraints(min_length=1, max_length=600)]


class VaultRenameBody(BaseModel):
    src: Annotated[str, StringConstraints(min_length=1, max_length=600)]
    dst: Annotated[str, StringConstraints(min_length=1, max_length=600)]
    update_links: bool = True
    dry_run: bool = False


class VaultAiBody(BaseModel):
    action: Literal["summarize", "continue", "improve", "ask", "tags"]
    path: Annotated[str, StringConstraints(min_length=1, max_length=600)]
    selection: Optional[Annotated[str, StringConstraints(max_length=8000)]] = None
    question: Optional[Annotated[str, StringConstraints(max_length=500)]] = None
    confirm_cloud: bool = False


@app.get("/api/obsidian/info")
async def obsidian_info():
    return await _vault(VAULT.info)


@app.get("/api/obsidian/tree")
async def obsidian_tree():
    return await _vault(VAULT.tree)


@app.get("/api/obsidian/note")
async def obsidian_read(path: str):
    return await _vault(VAULT.read, path)


@app.put("/api/obsidian/note")
async def obsidian_write(body: VaultWriteBody):
    return await _vault(VAULT.write, body.path, body.content, body.base_hash, body.force)


@app.post("/api/obsidian/note")
async def obsidian_create(body: VaultCreateBody):
    return await _vault(VAULT.create, body.path, body.content)


@app.delete("/api/obsidian/note")
async def obsidian_trash(path: str):
    return await _vault(VAULT.trash, path)


@app.post("/api/obsidian/folder")
async def obsidian_mkdir(body: VaultPathBody):
    return await _vault(VAULT.mkdir, body.path)


@app.post("/api/obsidian/rename")
async def obsidian_rename(body: VaultRenameBody):
    return await _vault(VAULT.rename, body.src, body.dst, body.update_links, body.dry_run)


@app.get("/api/obsidian/search")
async def obsidian_search(q: str):
    return {"results": await _vault(VAULT.search, q[:200])}


@app.get("/api/obsidian/tags")
async def obsidian_tags(tag: Optional[str] = None):
    return {"tags": await _vault(VAULT.tags, tag)}


@app.get("/api/obsidian/graph")
async def obsidian_graph():
    return await _vault(VAULT.graph)


@app.post("/api/obsidian/daily")
async def obsidian_daily():
    return await _vault(VAULT.daily)


@app.get("/api/obsidian/file")
async def obsidian_file(path: Optional[str] = None, name: Optional[str] = None):
    """Imagem do cofre (para a pre-visualizacao). `name` procura pelo nome, como o Obsidian faz com ![[foto.png]]."""
    if name and not path:
        path = await _vault(VAULT.find_attachment, name)
        if not path:
            raise HTTPException(status_code=404, detail={"error": "vault", "message": "Imagem nao encontrada."})
    if not path:
        raise HTTPException(status_code=400, detail={"error": "vault", "message": "Informe path ou name."})
    data, kind = await _vault(VAULT.read_image, path)
    return Response(content=data, media_type=kind, headers={"X-Content-Type-Options": "nosniff",
                                                            "Content-Security-Policy": "sandbox", "Cache-Control": "private, max-age=60"})


AI_PROMPTS = {
    "summarize": "Resuma a nota abaixo em portugues, em ate 8 topicos curtos.",
    "continue": "Continue escrevendo a nota abaixo em portugues, no mesmo estilo e formato Markdown. Devolva so o texto novo.",
    "improve": "Reescreva o trecho abaixo em portugues, mais claro e organizado, mantendo o sentido e o Markdown. Devolva so o texto reescrito.",
    "tags": "Sugira de 3 a 8 tags (no formato #tag, sem espacos) para a nota abaixo. Devolva so as tags, separadas por espaco.",
}


@app.post("/api/obsidian/ai")
async def obsidian_ai(body: VaultAiBody):
    """Pede ao Hera ajuda com uma nota. NADA e gravado: o texto volta para voce decidir. O roteador aplica o filtro
    de segredos e o Modo Offline; se o motor escolhido nao for local, e preciso confirmar (`confirm_cloud`)."""
    note = await _vault(VAULT.read, body.path)
    text = (body.selection or note["content"])[:12000]
    if body.action == "ask":
        if not (body.question or "").strip():
            raise HTTPException(status_code=400, detail={"error": "vault", "message": "Escreva a pergunta."})
        head = f"Responda em portugues, usando apenas a nota abaixo. Pergunta: {body.question.strip()}"
    else:
        head = AI_PROMPTS[body.action]
    task = AgentTask(id=f"vault-ai-{uuid.uuid4().hex[:8]}", title=f"Obsidian: {body.action}", task_type="summary",
                     instruction=f"{head}\n\n--- NOTA ({note['path']}) ---\n{text}", timeout_seconds=120, max_retries=0,
                     workspace=str(ROOT_DIR))
    try:
        adapter, model, reason = await router.route(task)
    except PermissionError as exc:
        raise HTTPException(status_code=409, detail={"error": "blocked", "message": str(exc)})
    local = adapter.name == "ollama"
    if not local and not body.confirm_cloud:
        raise HTTPException(status_code=409, detail={
            "error": "cloud_confirm", "engine": adapter.name,
            "message": f"O texto desta nota sera enviado para '{adapter.name}' (fora do seu PC). Confirme para continuar."})
    task.assigned_agent, task.assigned_model = adapter.name, model
    try:
        handle = await adapter.submit(task)
        result = await asyncio.wait_for(adapter.collect(handle), timeout=140)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail={"error": "timeout", "message": "O motor demorou demais."})
    if result.status != TaskStatus.SUCCEEDED:
        raise HTTPException(status_code=502, detail={"error": "engine", "message": (result.error_message or result.summary or "falhou")[:300]})
    return {"text": result.output_text, "engine": adapter.name, "model": model, "local": local}


@app.websocket("/ws/events")
async def websocket_endpoint(websocket: WebSocket):
    """Canal WebSocket para streaming de eventos e telemetria."""
    await ws_manager.connect(websocket)
    try:
        while True:
            # Manter conexão viva recebendo pings do cliente
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception:
        ws_manager.disconnect(websocket)

# Servir Frontend
@app.get("/healthz")
async def healthz():
    """Verificacao de vida SEM dados (publica). O estado dos motores fica em /api/health (autenticada)."""
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    """Serve a UI com o token da sessao embutido. A pagina so e legivel pela propria origem
    (Host/Origin checados no middleware, sem CORS), entao outro site nao consegue le-lo."""
    index_file = WEB_DIR / "index.html"
    if index_file.exists():
        html = index_file.read_text(encoding="utf-8")
        inject = f"<script>window.HERA_TOKEN = {json.dumps(SECURITY.token)};</script>"
        html = html.replace("</head>", inject + "</head>", 1)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})
    return HTMLResponse("<h1>Hera Multiagent — Frontend inicializando...</h1>")

# Endpoints OpenAI-Compatible Gateway (/v1)
@app.get("/v1/models")
async def list_gateway_models(request: Request):
    """Retorna lista de modelos disponíveis conforme especificação OpenAI (filtrada pela chave de cliente, se for o caso)."""
    models_list = []
    default_ollama = cfg["engines"]["ollama"].get("default_model", "qwen2.5-coder:1.5b")
    models_list.append({
        "id": default_ollama,
        "object": "model",
        "created": 1700000000,
        "owned_by": "hera-local"
    })
    for eng_name, eng_cfg in cfg.get("engines", {}).items():
        if eng_cfg.get("enabled") and eng_cfg.get("default_model"):
            m = eng_cfg.get("default_model")
            if m not in [x["id"] for x in models_list]:
                models_list.append({
                    "id": m,
                    "object": "model",
                    "created": 1700000000,
                    "owned_by": f"hera-{eng_name}"
                })
    for ad in gateway_tier_router.adapters:                    # modelos dos provedores de nuvem ligados
        for m in sorted(ad.served_models or []):
            if m not in [x["id"] for x in models_list]:
                models_list.append({"id": m, "object": "model", "created": 1700000000, "owned_by": f"hera-{ad.name}"})
    for ad in gateway_tier_router.adapters:                    # enderecamento direto: provedor/modelo
        for m in sorted(ad.served_models or [])[:20]:
            models_list.append({"id": f"{ad.name}/{m}", "object": "model", "created": 1700000000, "owned_by": f"hera-{ad.name}"})
    for combo in gateway_tier_router.combos:                   # combos do Roteador embutido
        if combo not in [x["id"] for x in models_list]:
            models_list.append({"id": combo, "object": "model", "created": 1700000000, "owned_by": "hera-combo"})
    for alias in gateway_tier_router.aliases:                  # apelidos
        if alias not in [x["id"] for x in models_list]:
            models_list.append({"id": alias, "object": "model", "created": 1700000000, "owned_by": "hera-alias"})
    ck = getattr(request.state, "client_key", None)
    if ck is not None:
        models_list = [m for m in models_list if ck.allows(m["id"])]
    return {"object": "list", "data": models_list}


@app.get("/api/gateway/telemetry")
async def gateway_stream_telemetry(limit: int = 50):
    """Telemetria dos últimos streams (sem conteúdo): TTFB, fragmentos, fallbacks, resultado."""
    limit = max(1, min(int(limit), 200))
    return {"summary": gateway_tier_router.telemetry.summary(), "recent": gateway_tier_router.telemetry.recent(limit)}


@app.get("/api/gateway/providers")
async def list_gateway_providers():
    """Estado dos provedores do gateway SEM segredos: camada, modelos e situação de cada chave."""
    out = []
    for ad in gateway_tier_router.adapters:
        km = getattr(ad, "key_manager", None)
        out.append({
            "name": ad.name, "tier": ad.tier, "local": ad.is_local,
            "models": sorted(ad.served_models or []),
            "keys": km.status() if km else [],
        })
    return {"tier_order": gateway_tier_router.tier_order, "providers": out}

# ---------------------------------------------------------------------------------------------
# Roteador embutido: provedores de nuvem, combos com fallback, teste
# ---------------------------------------------------------------------------------------------
_PROVIDER_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_COMBO_NAME = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")
_KEY_ENV = re.compile(r"^[A-Z][A-Z0-9_]{1,63}_API_KEYS?$")
_RESERVED_NAMES = {"default", "ollama", "local", "auto"}
_MODEL_OK = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
TIERS = ("cloud_premium", "cloud_cheap", "cloud_free")


def _bad_router(msg: str, status: int = 400):
    raise HTTPException(status_code=status, detail={"error": "invalid_router_config", "message": msg})


def _combo_defs_from_cfg(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """`gateway.combos` -> {nome: {"strategy": ..., "steps": [...]}}. Aceita o formato antigo (so a lista de passos)."""
    out: Dict[str, Dict[str, Any]] = {}
    for name, value in ((config.get("gateway") or {}).get("combos") or {}).items():
        strategy, raw_steps = "priority", value
        if isinstance(value, dict):
            strategy, raw_steps = str(value.get("strategy") or "priority"), value.get("steps")
        if strategy not in STRATEGIES or not isinstance(raw_steps, list):
            continue
        steps = []
        for st in raw_steps:
            if isinstance(st, dict) and st.get("provider") and st.get("model"):
                item: Dict[str, Any] = {"provider": str(st["provider"]), "model": str(st["model"])}
                if st.get("weight"):
                    item["weight"] = int(st["weight"])
                steps.append(item)
        if steps:
            out[str(name)] = {"strategy": strategy, "steps": steps}
    return out


def _combos_from_cfg(config: Dict[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    return {name: d["steps"] for name, d in _combo_defs_from_cfg(config).items()}


SECRET_STORE = SecretStore(DATA_DIR / ".hera_provider_keys.json")
USAGE = UsageLedger(DATA_DIR / "usage.db")
gateway_tier_router.usage = USAGE


def reload_gateway() -> None:
    """Reconstroi os provedores do gateway a partir do config.yaml + variaveis de ambiente + cofre (sem reiniciar)."""
    fresh = config_mgr.load_config()
    gateway_tier_router.adapters = build_gateway_adapters(fresh, secrets=SECRET_STORE)
    defs = _combo_defs_from_cfg(fresh)
    gateway_tier_router.combos = {n: d["steps"] for n, d in defs.items()}
    gateway_tier_router.combo_strategies = {n: d["strategy"] for n, d in defs.items()}
    gw = fresh.get("gateway") or {}
    gateway_tier_router.aliases = {str(k): str(v) for k, v in (gw.get("aliases") or {}).items()}
    gateway_tier_router.pricing = {str(k): {"in": float(v.get("in", 0)), "out": float(v.get("out", 0))}
                                   for k, v in (gw.get("pricing") or {}).items() if isinstance(v, dict)}
    gateway_tier_router.tier_order = list(gw.get("default_tier_order") or gateway_tier_router.tier_order)
    gateway_tier_router.guard.threshold = max(1, int(gw.get("breaker_threshold") or 3))
    providers_cfg = fresh.get("providers") or {}
    gateway_tier_router.priorities = {n: int(p["priority"]) for n, p in providers_cfg.items()
                                      if isinstance(p, dict) and p.get("priority")}
    gateway_tier_router.guard.set_limits({n: {"rpm": p.get("rpm"), "daily_requests": p.get("daily_requests"),
                                              "daily_tokens": p.get("daily_tokens")}
                                          for n, p in providers_cfg.items() if isinstance(p, dict)})


reload_gateway()


def _env_keys_found(key_env: str) -> int:
    return len(load_keys_from_env([n.strip() for n in str(key_env or "").split(",") if n.strip()], os.environ))


class RouterKeysBody(BaseModel):
    keys: List[Annotated[str, StringConstraints(max_length=600)]] = Field(min_length=1, max_length=10)
    human_code: Optional[str] = None


def _clean_models(models: Any) -> List[str]:
    if models is None:
        return []
    if not isinstance(models, list) or len(models) > 50:
        _bad_router("'models' precisa ser uma lista de ate 50 nomes")
    out = []
    for m in models:
        if not isinstance(m, str) or not _MODEL_OK.match(m.strip()):
            _bad_router(f"Nome de modelo invalido: {m!r}")
        out.append(m.strip())
    return list(dict.fromkeys(out))


class RouterProviderBody(BaseModel):
    preset: Optional[ShortStr] = None
    name: Optional[ShortStr] = None
    base_url: Optional[Annotated[str, StringConstraints(max_length=300)]] = None
    tier: Optional[ShortStr] = None
    key_env: Optional[ShortStr] = None
    default_model: Optional[Annotated[str, StringConstraints(max_length=200)]] = None
    models: Optional[List[Annotated[str, StringConstraints(max_length=200)]]] = Field(default=None, max_length=50)
    enabled: bool = True
    daily_request_limit: Optional[int] = Field(default=None, ge=1, le=10_000_000)
    human_code: Optional[str] = None


class RouterEnabledBody(BaseModel):
    enabled: bool
    human_code: Optional[str] = None


class RouterStep(BaseModel):
    provider: Annotated[str, StringConstraints(max_length=40)]
    model: Annotated[str, StringConstraints(max_length=200)]
    weight: Optional[int] = Field(default=None, ge=1, le=100)


class RouterCombo(BaseModel):
    strategy: Literal["priority", "round_robin", "fastest", "least_used", "weighted"] = "priority"
    steps: List[RouterStep]


class RouterCombosBody(BaseModel):
    combos: Dict[Annotated[str, StringConstraints(max_length=40)], Union[RouterCombo, List[RouterStep]]] = Field(max_length=20)


class RouterLimitsBody(BaseModel):
    priority: Optional[int] = Field(default=None, ge=0, le=1000)
    rpm: Optional[int] = Field(default=None, ge=0, le=100_000)
    daily_requests: Optional[int] = Field(default=None, ge=0, le=100_000_000)
    daily_tokens: Optional[int] = Field(default=None, ge=0, le=10_000_000_000)


class PriceBody(BaseModel):
    in_: float = Field(default=0.0, alias="in", ge=0, le=10_000)
    out: float = Field(default=0.0, ge=0, le=10_000)
    model_config = {"populate_by_name": True}


class RouterSettingsBody(BaseModel):
    tier_order: Optional[List[Literal["cloud_premium", "cloud_cheap", "cloud_free", "local"]]] = Field(default=None, max_length=4)
    rtk: Optional[bool] = None
    aliases: Optional[Dict[Annotated[str, StringConstraints(max_length=60)], Annotated[str, StringConstraints(max_length=200)]]] = Field(default=None, max_length=50)
    pricing: Optional[Dict[Annotated[str, StringConstraints(max_length=240)], PriceBody]] = Field(default=None, max_length=200)
    breaker_threshold: Optional[int] = Field(default=None, ge=1, le=20)


class ClientCreateBody(BaseModel):
    name: Annotated[str, StringConstraints(min_length=2, max_length=60)]
    models: List[Annotated[str, StringConstraints(max_length=120)]] = Field(default_factory=list, max_length=20)
    rpm: Optional[int] = Field(default=None, ge=1, le=100_000)
    daily_tokens: Optional[int] = Field(default=None, ge=1, le=10_000_000_000)
    human_code: Optional[str] = None


class ClientPatchBody(BaseModel):
    name: Optional[Annotated[str, StringConstraints(min_length=2, max_length=60)]] = None
    enabled: Optional[bool] = None
    models: Optional[List[Annotated[str, StringConstraints(max_length=120)]]] = Field(default=None, max_length=20)
    rpm: Optional[int] = Field(default=None, ge=0, le=100_000)
    daily_tokens: Optional[int] = Field(default=None, ge=0, le=10_000_000_000)
    human_code: Optional[str] = None


class RouterImportBody(BaseModel):
    data: Dict[str, Any]
    human_code: Optional[str] = None


class RouterTestBody(BaseModel):
    provider: Annotated[str, StringConstraints(max_length=40)]
    model: Optional[Annotated[str, StringConstraints(max_length=200)]] = None


def _provider_view(name: str, p: Dict[str, Any]) -> Dict[str, Any]:
    live = next((a for a in gateway_tier_router.adapters if a.name == name), None)
    km = getattr(live, "key_manager", None)
    return {"name": name, "enabled": bool(p.get("enabled")), "tier": p.get("tier", "cloud_cheap"),
            "base_url": p.get("base_url", ""), "default_model": p.get("default_model", ""),
            "models": list(p.get("models") or []), "key_env": p.get("api_key_env", ""),
            "keys_env": _env_keys_found(p.get("api_key_env", "")), "keys_stored": SECRET_STORE.count(name),
            "keys_found": _env_keys_found(p.get("api_key_env", "")) + SECRET_STORE.count(name), "active": live is not None,
            "keys": km.status() if km else [], "daily_request_limit": p.get("daily_request_limit"),
            "priority": p.get("priority") or 100, "rpm": p.get("rpm"), "daily_requests": p.get("daily_requests"),
            "daily_tokens": p.get("daily_tokens"), "health": gateway_tier_router.guard.status(name),
            "today": USAGE.today(provider=name)}


@app.get("/api/router/state")
async def router_state():
    """Presets, provedores configurados (sem segredos: so QUANTAS chaves foram achadas) e combos."""
    current = config_mgr.load_config()
    providers = [_provider_view(n, p) for n, p in (current.get("providers") or {}).items() if isinstance(p, dict)]
    ollama_models: List[str] = []
    try:        # so pergunta ao Ollama se a porta responde (senao a tela esperava o timeout a cada leitura)
        if "ollama" in adapters and await asyncio.to_thread(port_open, SERVICES["ollama"]["port"]):
            ollama_models = (await adapters["ollama"].health_check()).available_models
    except Exception:       # noqa: BLE001
        pass
    gw = current.get("gateway") or {}
    return {"presets": PROVIDER_PRESETS, "providers": providers, "combos": _combo_defs_from_cfg(current),
            "tier_order": gateway_tier_router.tier_order, "offline": router.default_offline,
            "ollama_models": ollama_models, "strategies": list(STRATEGIES), "aliases": gw.get("aliases") or {},
            "pricing": gw.get("pricing") or {}, "breaker_threshold": gateway_tier_router.guard.threshold,
            "rtk": {"enabled": RUNTIME_FLAGS["rtk"], **gateway_tier_router.rtk_stats}}


@app.post("/api/router/providers")
async def router_upsert_provider(body: RouterProviderBody):
    """Cria/atualiza um provedor de nuvem. Exige o codigo humano: ligar um provedor manda dados para fora."""
    base: Dict[str, Any] = {}
    if body.preset:
        try:
            base = get_preset(body.preset)
        except KeyError:
            _bad_router(f"Preset desconhecido: {body.preset!r}")
    name = (body.name or base.get("id") or "").strip().lower()
    if not _PROVIDER_NAME.match(name) or name in _RESERVED_NAMES:
        _bad_router("Nome do provedor invalido (letras minusculas, numeros, - e _; 2 a 32 caracteres).")
    tier = body.tier or base.get("tier") or "cloud_cheap"
    if tier not in TIERS:
        _bad_router(f"'tier' precisa ser um de {TIERS}")
    key_env = (body.key_env if body.key_env is not None else base.get("key_env") or "").strip()
    if key_env and (not _KEY_ENV.match(key_env) or key_env.startswith("HERA_")):
        _bad_router("O nome da variavel da chave precisa terminar em _API_KEY (ex.: GROQ_API_KEY) e nao pode comecar com HERA_.")
    base_url = (body.base_url or base.get("base_url") or "").strip()
    try:
        base_url = validate_provider_url(base_url, is_local_profile=False)
    except (SSRFSecurityError, ValueError) as exc:
        _bad_router(f"URL recusada: {exc}")
    default_model = (body.default_model or base.get("default_model") or "").strip()
    if not _MODEL_OK.match(default_model):
        _bad_router("Informe o modelo padrao.")
    models = _clean_models(body.models if body.models is not None else base.get("models"))
    _require_human_code(body.human_code, f"Provedor de nuvem '{name}' sera configurado")

    current = config_mgr.load_config()
    providers = dict(current.get("providers") or {})
    entry: Dict[str, Any] = {"enabled": body.enabled, "tier": tier, "base_url": base_url, "api_key_env": key_env,
                             "default_model": default_model, "models": models, "timeout": 60}
    if body.daily_request_limit:
        entry["daily_request_limit"] = body.daily_request_limit
    providers[name] = entry
    config_mgr.save_config({**current, "providers": providers})
    reload_gateway()
    broadcast_log(f"🌐 [ROTEADOR] Provedor '{name}' salvo ({'ligado' if body.enabled else 'desligado'}).", level="success")
    return {"status": "ok", "provider": _provider_view(name, entry)}


@app.post("/api/router/providers/{name}/enabled")
async def router_set_enabled(name: str, body: RouterEnabledBody):
    current = config_mgr.load_config()
    providers = dict(current.get("providers") or {})
    if name not in providers:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Provedor desconhecido."})
    if body.enabled:                                    # desligar nao precisa de codigo; ligar sim
        _require_human_code(body.human_code, f"Provedor de nuvem '{name}' sera LIGADO")
    providers[name] = {**providers[name], "enabled": body.enabled}
    config_mgr.save_config({**current, "providers": providers})
    reload_gateway()
    return {"status": "ok", "provider": _provider_view(name, providers[name])}


@app.delete("/api/router/providers/{name}")
async def router_delete_provider(name: str):
    current = config_mgr.load_config()
    providers = dict(current.get("providers") or {})
    if name not in providers:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Provedor desconhecido."})
    used = [c for c, steps in _combos_from_cfg(current).items() if any(s["provider"] == name for s in steps)]
    if used:
        raise HTTPException(status_code=409, detail={"error": "in_use",
                                                     "message": f"O provedor esta nos combos: {', '.join(used)}. Tire-o de la primeiro."})
    providers.pop(name)
    config_mgr.save_config({**current, "providers": providers})
    SECRET_STORE.delete(name)
    reload_gateway()
    return {"status": "ok"}


@app.post("/api/router/providers/{name}/keys")
async def router_set_keys(name: str, body: RouterKeysBody):
    """Guarda as chaves do provedor no cofre local (DPAPI). Exige o codigo humano. Nunca devolve a chave."""
    if name not in (config_mgr.load_config().get("providers") or {}):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Provedor desconhecido."})
    _require_human_code(body.human_code, f"Chave de API do provedor '{name}' sera gravada no cofre")
    try:
        count = SECRET_STORE.set(name, body.keys)
    except SecretStoreError as exc:
        _bad_router(str(exc))
    reload_gateway()
    broadcast_log(f"🔐 [ROTEADOR] {count} chave(s) de '{name}' gravada(s) no cofre.", level="success")
    return {"status": "ok", "keys_stored": count}


@app.delete("/api/router/providers/{name}/keys")
async def router_clear_keys(name: str):
    SECRET_STORE.delete(name)
    reload_gateway()
    return {"status": "ok", "keys_stored": 0}


@app.post("/api/router/providers/{name}/discover")
async def router_discover_models(name: str):
    """Pergunta ao provedor quais modelos ele tem (GET /models) e grava a lista (ate 200). Usa uma chave dele."""
    adapter = next((a for a in gateway_tier_router.adapters if a.name == name), None)
    km = getattr(adapter, "key_manager", None)
    if adapter is None or km is None:
        raise HTTPException(status_code=404, detail={"error": "not_active", "message": "Provedor desligado ou sem chave."})
    lease = km.acquire()
    client = make_safe_async_client(timeout=20.0)
    try:
        resp = await client.get(f"{adapter.base_url}/models", headers={"Authorization": f"Bearer {lease.secret}"})
    except Exception as exc:        # noqa: BLE001
        return {"ok": False, "error": f"Nao foi possivel consultar o provedor ({type(exc).__name__})."}
    finally:
        await client.aclose()
    if resp.status_code != 200:
        return {"ok": False, "status": resp.status_code, "error": f"O provedor respondeu HTTP {resp.status_code}."}
    try:
        items = resp.json().get("data") or resp.json().get("models") or []
        ids = [str(i.get("id") or i.get("name")) for i in items if isinstance(i, dict)]
    except (ValueError, AttributeError):
        return {"ok": False, "error": "Resposta do provedor em formato inesperado."}
    ids = [m.removeprefix("models/") for m in ids if _MODEL_OK.match(m)]
    ids = list(dict.fromkeys(ids))[:200]
    if not ids:
        return {"ok": False, "error": "O provedor nao listou nenhum modelo."}
    current = config_mgr.load_config()
    providers = dict(current.get("providers") or {})
    if name in providers:
        providers[name] = {**providers[name], "models": ids}
        config_mgr.save_config({**current, "providers": providers})
        reload_gateway()
    return {"ok": True, "count": len(ids), "models": ids[:50]}


@app.get("/api/router/usage")
async def router_usage(days: int = 7):
    """Chamadas, tokens, erros e latencia por provedor/modelo/dia. So metadados: nunca o conteudo."""
    return await asyncio.to_thread(USAGE.summary, days)


@app.put("/api/router/combos")
async def router_set_combos(body: RouterCombosBody):
    """Substitui os combos. Um combo so reordena provedores ja configurados: nao precisa de codigo."""
    current = config_mgr.load_config()
    known = set((current.get("providers") or {})) | {"ollama"}
    clean: Dict[str, Dict[str, Any]] = {}
    for cname, value in body.combos.items():
        if not _COMBO_NAME.match(cname) or cname in _RESERVED_NAMES or cname in known:
            _bad_router(f"Nome de combo invalido: {cname!r}")
        strategy, steps = (value.strategy, value.steps) if isinstance(value, RouterCombo) else ("priority", value)
        if not 1 <= len(steps) <= 8:
            _bad_router(f"O combo {cname!r} precisa ter de 1 a 8 passos.")
        out = []
        for step in steps:
            if step.provider not in known:
                _bad_router(f"O combo {cname!r} usa um provedor que nao existe: {step.provider!r}")
            if not _MODEL_OK.match(step.model):
                _bad_router(f"Modelo invalido no combo {cname!r}.")
            item: Dict[str, Any] = {"provider": step.provider, "model": step.model}
            if step.weight:
                item["weight"] = step.weight
            out.append(item)
        clean[cname] = {"strategy": strategy, "steps": out}
    gateway_cfg = {**(current.get("gateway") or {}), "combos": clean}
    config_mgr.save_config({**current, "gateway": gateway_cfg})
    reload_gateway()
    return {"status": "ok", "combos": clean}


@app.post("/api/router/providers/{name}/limits")
async def router_set_limits(name: str, body: RouterLimitsBody):
    """Prioridade dentro da camada e limites do provedor (por minuto / requisicoes por dia / tokens por dia). 0 limpa."""
    current = config_mgr.load_config()
    providers = dict(current.get("providers") or {})
    if name not in providers:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Provedor desconhecido."})
    entry = dict(providers[name])
    for field in body.model_fields_set:
        value = getattr(body, field)
        if value:
            entry[field] = value
        else:
            entry.pop(field, None)
    providers[name] = entry
    config_mgr.save_config({**current, "providers": providers})
    reload_gateway()
    return {"status": "ok", "provider": _provider_view(name, entry)}


@app.post("/api/router/providers/{name}/reset")
async def router_reset_breaker(name: str):
    """Tira o provedor da pausa do disjuntor (volta a ser tentado na hora)."""
    gateway_tier_router.guard.reset(name)
    return {"status": "ok", "health": gateway_tier_router.guard.status(name)}


@app.put("/api/router/settings")
async def router_set_settings(body: RouterSettingsBody):
    """Ordem das camadas, RTK, apelidos de modelo, precos (US$ por 1M tokens) e sensibilidade do disjuntor."""
    current = config_mgr.load_config()
    gw = dict(current.get("gateway") or {})
    flags = dict(current.get("feature_flags") or {})
    if body.tier_order is not None:
        if len(set(body.tier_order)) != len(body.tier_order) or "local" not in body.tier_order:
            _bad_router("A ordem das camadas nao pode repetir e precisa incluir 'local'.")
        gw["default_tier_order"] = list(body.tier_order)
    if body.aliases is not None:
        known = set((current.get("providers") or {})) | set(_combo_defs_from_cfg(current))
        clean_aliases = {}
        for alias, target in body.aliases.items():
            if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,59}$", alias) or alias in _RESERVED_NAMES or alias in known or alias == target:
                _bad_router(f"Apelido invalido: {alias!r}")
            if not _MODEL_OK.match(target):
                _bad_router(f"Destino invalido para o apelido {alias!r}")
            clean_aliases[alias] = target
        gw["aliases"] = clean_aliases
    if body.pricing is not None:
        clean_prices = {}
        for key, price in body.pricing.items():
            if not re.match(r"^[a-z][a-z0-9_-]{1,31}(/[^\x00-\x1f\x7f]{1,200})?$", key):
                _bad_router(f"Chave de preco invalida: {key!r} (use 'provedor' ou 'provedor/modelo')")
            clean_prices[key] = {"in": price.in_, "out": price.out}
        gw["pricing"] = clean_prices
    if body.breaker_threshold is not None:
        gw["breaker_threshold"] = body.breaker_threshold
    if body.rtk is not None:
        flags["enable_rtk"] = body.rtk
        RUNTIME_FLAGS["rtk"] = body.rtk
    config_mgr.save_config({**current, "gateway": gw, "feature_flags": flags})
    reload_gateway()
    return {"status": "ok"}


# ---- chaves de acesso para ferramentas externas (so /v1)
def _client_view(ck) -> Dict[str, Any]:
    return {**ck.public(), "today": USAGE.today(client=ck.name)}


@app.get("/api/router/clients")
async def router_list_clients():
    return {"clients": [_client_view(c) for c in CLIENT_KEYS.list()]}


def _clean_patterns(patterns: List[str]) -> List[str]:
    out = []
    for pat in patterns:
        pat = pat.strip()
        if not re.match(r"^[A-Za-z0-9_./:*\-]{1,120}$", pat):
            _bad_router(f"Padrao de modelo invalido: {pat!r} (letras, numeros, _ . / : - e * apenas)")
        out.append(pat)
    return list(dict.fromkeys(out))


@app.post("/api/router/clients")
async def router_create_client(body: ClientCreateBody):
    """Cria uma chave de acesso. Exige o codigo humano. A chave aparece UMA vez, nesta resposta."""
    name = body.name.strip()
    if any(c.name.lower() == name.lower() for c in CLIENT_KEYS.list()):
        _bad_router("Ja existe uma chave com esse nome.", 409)
    patterns = _clean_patterns(body.models)
    _require_human_code(body.human_code, f"Chave de acesso '{name}' sera criada para ferramentas externas")
    try:
        created = CLIENT_KEYS.create(name, patterns, body.rpm, body.daily_tokens)
    except ValueError as exc:
        _bad_router(str(exc))
    broadcast_log(f"🔑 [ROTEADOR] Chave de acesso '{name}' criada.", level="success")
    return created


@app.patch("/api/router/clients/{key_id}")
async def router_patch_client(key_id: str, body: ClientPatchBody):
    changes: Dict[str, Any] = {}
    if body.name is not None:
        if any(c.name.lower() == body.name.strip().lower() and c.id != key_id for c in CLIENT_KEYS.list()):
            _bad_router("Ja existe uma chave com esse nome.", 409)
        changes["name"] = body.name.strip()
    if body.enabled is not None:
        if body.enabled:
            _require_human_code(body.human_code, "Uma chave de acesso sera REATIVADA")
        changes["enabled"] = body.enabled
    if body.models is not None:
        changes["models"] = _clean_patterns(body.models)
    for field in ("rpm", "daily_tokens"):
        if field in body.model_fields_set:
            changes[field] = getattr(body, field) or None
    updated = CLIENT_KEYS.update(key_id, **changes)
    if updated is None:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Chave desconhecida."})
    return {"status": "ok", "client": _client_view(updated)}


@app.delete("/api/router/clients/{key_id}")
async def router_delete_client(key_id: str):
    if not CLIENT_KEYS.delete(key_id):
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Chave desconhecida."})
    return {"status": "ok"}


@app.get("/api/router/logs")
async def router_logs(limit: int = 100, provider: Optional[str] = None, client: Optional[str] = None, errors: bool = False):
    """Ultimas chamadas (so metadados). Chamadas da mesma requisicao (fallback) compartilham o `request_id`."""
    return {"logs": await asyncio.to_thread(USAGE.logs, limit, provider, client, errors)}


# ---- exportar / importar a configuracao do roteador (nunca inclui chaves)
@app.get("/api/router/export")
async def router_export():
    current = config_mgr.load_config()
    gw = current.get("gateway") or {}
    return {"version": 1, "providers": current.get("providers") or {}, "combos": _combo_defs_from_cfg(current),
            "aliases": gw.get("aliases") or {}, "pricing": gw.get("pricing") or {},
            "tier_order": gw.get("default_tier_order") or gateway_tier_router.tier_order,
            "breaker_threshold": gw.get("breaker_threshold") or 3,
            "note": "Sem chaves de API: elas ficam no cofre/ambiente de cada maquina."}


def _validated_provider_entry(name: str, raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict) or not _PROVIDER_NAME.match(name) or name in _RESERVED_NAMES:
        _bad_router(f"Provedor invalido no arquivo: {name!r}")
    tier = raw.get("tier", "cloud_cheap")
    key_env = str(raw.get("api_key_env") or "")
    if tier not in TIERS or (key_env and (not _KEY_ENV.match(key_env) or key_env.startswith("HERA_"))):
        _bad_router(f"Provedor {name!r}: tier ou variavel de chave invalidos.")
    try:
        url = validate_provider_url(str(raw.get("base_url", "")), is_local_profile=False)
    except (SSRFSecurityError, ValueError) as exc:
        _bad_router(f"Provedor {name!r}: URL recusada ({exc})")
    model = str(raw.get("default_model", "")).strip()
    if not _MODEL_OK.match(model):
        _bad_router(f"Provedor {name!r}: modelo padrao invalido.")
    entry: Dict[str, Any] = {"enabled": False, "tier": tier, "base_url": url, "api_key_env": key_env,
                             "default_model": model, "models": _clean_models(raw.get("models")), "timeout": 60}
    for field, hi in (("priority", 1000), ("rpm", 100_000), ("daily_requests", 10 ** 8), ("daily_tokens", 10 ** 10),
                      ("daily_request_limit", 10 ** 7)):
        if raw.get(field):
            value = raw[field]
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= hi:
                _bad_router(f"Provedor {name!r}: {field} invalido.")
            entry[field] = value
    return entry


@app.post("/api/router/import")
async def router_import(body: RouterImportBody):
    """Importa provedores (chegam DESLIGADOS: ligar exige o codigo humano de novo), combos, apelidos e precos."""
    data = body.data
    if data.get("version") != 1:
        _bad_router("Arquivo de versao desconhecida.")
    _require_human_code(body.human_code, "Uma configuracao do roteador sera IMPORTADA")
    providers = {n: _validated_provider_entry(n, p) for n, p in (data.get("providers") or {}).items()}
    if len(providers) > 40:
        _bad_router("Provedores demais no arquivo.")
    current = config_mgr.load_config()
    gw = dict(current.get("gateway") or {})
    known = set(providers) | {"ollama"}
    combos: Dict[str, Any] = {}
    for cname, value in (data.get("combos") or {}).items():
        strategy = value.get("strategy", "priority") if isinstance(value, dict) else "priority"
        steps = value.get("steps") if isinstance(value, dict) else value
        if (not _COMBO_NAME.match(str(cname)) or cname in _RESERVED_NAMES or cname in known or strategy not in STRATEGIES
                or not isinstance(steps, list) or not 1 <= len(steps) <= 8):
            _bad_router(f"Combo invalido no arquivo: {cname!r}")
        clean_steps = []
        for st in steps:
            if not isinstance(st, dict) or st.get("provider") not in known or not _MODEL_OK.match(str(st.get("model", ""))):
                _bad_router(f"Passo invalido no combo {cname!r}.")
            item = {"provider": st["provider"], "model": str(st["model"])}
            if st.get("weight"):
                item["weight"] = max(1, min(100, int(st["weight"])))
            clean_steps.append(item)
        combos[cname] = {"strategy": strategy, "steps": clean_steps}
    gw["combos"] = combos
    aliases = {}
    for a, t in (data.get("aliases") or {}).items():
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,59}$", str(a)) and _MODEL_OK.match(str(t)) and a not in known and a not in combos:
            aliases[str(a)] = str(t)
    gw["aliases"] = aliases
    prices = {}
    for k, v in (data.get("pricing") or {}).items():
        if isinstance(v, dict) and re.match(r"^[a-z][a-z0-9_-]{1,31}(/[^\x00-\x1f\x7f]{1,200})?$", str(k)):
            prices[str(k)] = {"in": max(0.0, min(10_000.0, float(v.get("in", 0)))), "out": max(0.0, min(10_000.0, float(v.get("out", 0))))}
    gw["pricing"] = prices
    order = data.get("tier_order")
    if isinstance(order, list) and "local" in order and set(order) <= {"cloud_premium", "cloud_cheap", "cloud_free", "local"} and len(set(order)) == len(order):
        gw["default_tier_order"] = order
    config_mgr.save_config({**current, "providers": providers, "gateway": gw})
    reload_gateway()
    return {"status": "ok", "providers": len(providers), "combos": len(combos)}


@app.post("/api/router/test")
async def router_test(body: RouterTestBody):
    """Manda UMA pergunta fixa e inocente ('Responda apenas: OK') so a esse provedor. Nao envia nada seu."""
    adapter = next((a for a in gateway_tier_router.adapters if a.name == body.provider), None)
    if adapter is None:
        raise HTTPException(status_code=404, detail={
            "error": "not_active", "message": "Provedor desligado, sem chave na variavel de ambiente ou inexistente."})
    req = ChatCompletionRequest(model=body.model or adapter.default_model, temperature=0,
                                max_tokens=16, messages=[ChatMessage(role="user", content="Responda apenas: OK")])
    started = time.perf_counter()
    try:
        resp = await asyncio.wait_for(_gateway_call(req, only_provider=body.provider, label="hera (teste)"), timeout=40)
        text = (resp.choices[0].message.content if resp.choices and resp.choices[0].message else "") or ""
        return {"ok": True, "latency_ms": int((time.perf_counter() - started) * 1000), "model": resp.model,
                "text": str(text)[:80]}
    except GatewayProviderError as exc:
        hint = " (o Modo 100% Offline bloqueia a nuvem)" if router.default_offline and not adapter.is_local else ""
        return {"ok": False, "status": exc.status_code, "error": str(exc)[:300] + hint}
    except asyncio.TimeoutError:
        return {"ok": False, "status": 504, "error": "O provedor nao respondeu em 40 s."}


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest, request: Request):
    """Endpoint OpenAI-compatible para chat completions (com streaming SSE, fallback e DLP)."""
    global gateway_http_client
    ck = getattr(request.state, "client_key", None)          # chave de ACESSO de cliente (nao e o token mestre)
    if ck is not None:
        if not ck.allows(req.model):
            raise HTTPException(status_code=403, detail=f"A chave '{ck.name}' nao tem acesso ao modelo '{req.model}'.")
        if ck.daily_tokens and USAGE.today(client=ck.name)["tokens"] >= ck.daily_tokens:
            raise HTTPException(status_code=429, detail=f"A chave '{ck.name}' atingiu o limite diario de {ck.daily_tokens} tokens.",
                                headers={"Retry-After": "3600"})
        CLIENT_NAME.set(ck.name)
    REQUEST_ID.set(f"req-{uuid.uuid4().hex[:10]}")
    if gateway_http_client is None or gateway_http_client.is_closed:
        client_to_use = make_safe_async_client(timeout=60.0)
        should_close = True
    else:
        client_to_use = gateway_http_client
        should_close = False

    enable_rtk = RUNTIME_FLAGS["rtk"]

    try:
        if req.stream:
            stream_gen = await gateway_tier_router.route_and_stream(
                request=req,
                client=client_to_use,
                force_offline=router.default_offline,
                enable_rtk=enable_rtk
            )
            return StreamingResponse(
                stream_gen,
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive"
                }
            )

        resp = await gateway_tier_router.route_and_execute(
            request=req,
            client=client_to_use,
            force_offline=router.default_offline,
            enable_rtk=enable_rtk
        )
        return resp
    except GatewayProviderError as e:
        raise HTTPException(status_code=e.status_code, detail=str(e))
    finally:
        if should_close and not req.stream:
            await client_to_use.aclose()

# ==================== ENDPOINTS HITL (VISUAL DIFF & APPROVAL) ====================

class CreateProposalPayload(BaseModel):
    task_id: str
    repo_path: Optional[str] = None
    feature_branch: str
    target_branch: str = "master"
    worktree_id: Optional[str] = None
    worktree_path: Optional[str] = None
    allowed_paths: Optional[List[str]] = None

def _allowed_repo_roots() -> List[Path]:
    """Projeto + raizes extras do config.yaml (`general.allowed_repo_roots`, nao editavel pela API)."""
    extra = config_mgr.load_config().get("general", {}).get("allowed_repo_roots") or []
    return [ROOT_DIR, *[Path(r) for r in extra if isinstance(r, str) and r.strip()]]


@app.post("/api/hitl/proposals")
async def create_hitl_proposal(payload: CreateProposalPayload):
    roots = _allowed_repo_roots()
    try:
        repo = str(ensure_inside(payload.repo_path or str(ROOT_DIR), roots))
        if payload.worktree_path:
            payload.worktree_path = str(ensure_inside(payload.worktree_path, roots))
        if payload.allowed_paths:
            ensure_safe_relative_list(payload.allowed_paths)
    except PathNotAllowedError as exc:
        raise HTTPException(status_code=400, detail={"error": "path_not_allowed", "message": str(exc)})
    try:
        prop = hitl_service.create_proposal(
            task_id=payload.task_id,
            repo_path=repo,
            feature_branch=payload.feature_branch,
            target_branch=payload.target_branch,
            worktree_id=payload.worktree_id,
            worktree_path=payload.worktree_path,
            allowed_paths=payload.allowed_paths,
        )
        return prop
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/hitl/proposals")
async def list_hitl_proposals(status: Optional[str] = None):
    return hitl_service.list_proposals(status=status)

@app.get("/api/hitl/proposals/{proposal_id}")
async def get_hitl_proposal(proposal_id: str):
    prop = hitl_service.get_proposal(proposal_id)
    if not prop:
        raise HTTPException(status_code=404, detail="Proposta não encontrada")
    return prop

@app.post("/api/hitl/nonces")
async def issue_hitl_nonce():
    nonce = hitl_service.approval_engine.generate_nonce()
    return {"nonce": nonce, "expires_in_seconds": hitl_service.approval_engine.token_ttl_seconds}

@app.post("/api/hitl/approve")
async def approve_hitl_proposal(req: ApprovalRequest):
    _require_human_code(req.human_code, f"Aprovação do merge da proposta {req.proposal_id} solicitada")
    req.user_id = OPERATOR_ID           # a identidade vem do servidor, não do corpo da requisição
    res = hitl_service.approve_and_merge(req)
    if not res.success:
        raise HTTPException(status_code=400, detail={"error": res.error, "message": res.message})
    return res

@app.post("/api/hitl/reject")
async def reject_hitl_proposal(req: RejectRequest):
    success = hitl_service.reject_proposal(req)
    if not success:
        raise HTTPException(status_code=404, detail="Proposta não encontrada")
    return {"status": "rejected", "proposal_id": req.proposal_id}

@app.post("/api/hitl/request-changes")
async def request_hitl_changes(req: RequestChangesRequest):
    success = hitl_service.request_changes(req)
    if not success:
        raise HTTPException(status_code=404, detail="Proposta não encontrada")
    return {"status": "changes_requested", "proposal_id": req.proposal_id}

@app.get("/api/hitl/worktrees")
async def list_hitl_worktrees():
    return [w.to_dict() for w in hitl_service.git_manager.registry.list_all()]

# ==================== ENDPOINTS MCP (MODEL CONTEXT PROTOCOL) ====================

class McpServerRegistration(McpServerConfig):
    """Registro de servidor MCP. Servidor stdio é CÓDIGO que roda na máquina: exige o código humano."""
    human_code: Optional[str] = None


class HumanCodeBody(BaseModel):
    human_code: Optional[str] = None


class McpToolPolicyBody(BaseModel):
    name: str
    enabled: bool
    effect: Optional[Literal["read", "write", "network", "destructive"]] = None
    requires_hitl: Optional[bool] = None
    human_code: Optional[str] = None


@app.get("/api/mcp/servers")
async def list_mcp_servers():
    """Lista os servidores SEM os valores das variáveis de ambiente (podem conter segredos)."""
    return [{**s.model_dump(), "env": {k: "***" for k in s.env}} for s in mcp_client.catalog.list_servers()]

@app.post("/api/mcp/servers")
async def register_mcp_server(body: McpServerRegistration):
    cfg = McpServerConfig(**body.model_dump(exclude={"human_code"}))
    if cfg.transport == "stdio":
        _require_human_code(body.human_code, f"Registro do servidor MCP '{cfg.id}' (executa código local)")
        try:
            validate_stdio_server(cfg, _allowed_repo_roots())
        except McpLaunchError as e:
            raise HTTPException(status_code=400, detail=str(e))
    try:
        saved = mcp_client.catalog.register_server(cfg)
    except McpSecurityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {**saved.model_dump(), "env": {k: "***" for k in saved.env}}

@app.post("/api/mcp/servers/{server_id}/discover")
async def discover_mcp_tools(server_id: str, body: HumanCodeBody):
    """Lança o servidor (código!) e registra suas ferramentas DESLIGADAS: exige o código humano."""
    _require_human_code(body.human_code, f"Descoberta de ferramentas do servidor MCP '{server_id}'")
    try:
        tools = await mcp_client.discover(server_id)
    except McpSecurityError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except McpTransportError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return [t.model_dump() for t in tools]

@app.post("/api/mcp/tools/policy")
async def set_mcp_tool_policy(body: McpToolPolicyBody):
    """Decisão humana sobre uma ferramenta descoberta: liberar/bloquear, efeito e confirmação."""
    _require_human_code(body.human_code, f"Mudança de política da ferramenta MCP '{body.name}'")
    try:
        return mcp_client.catalog.set_tool_policy(body.name, enabled=body.enabled, effect=body.effect,
                                                  requires_hitl=body.requires_hitl)
    except McpSecurityError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/mcp/tools")
async def list_mcp_tools():
    return mcp_client.catalog.list_tools()

@app.post("/api/mcp/tools")
async def register_mcp_tool(tool: McpToolDefinition):
    try:
        return mcp_client.catalog.register_tool(tool)
    except McpSecurityError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/mcp/execute")
async def execute_mcp_tool(req: McpToolCallRequest):
    # `confirmed_hitl` do corpo NUNCA vale: só um código humano válido confirma (fase 2).
    req.confirmed_hitl = False
    if req.human_code:
        _require_human_code(req.human_code, f"Ferramenta MCP '{req.tool_name}' exige aprovação humana")
        req.confirmed_hitl = True
    try:
        return await mcp_client.execute_tool(req)
    except McpPermissionDeniedError as e:
        _announce_human_code(f"Ferramenta MCP '{req.tool_name}' exige aprovação humana")
        raise HTTPException(status_code=403, detail={"error": "human_code_required", "message": str(e)})
    except McpSecurityError as e:
        raise HTTPException(status_code=400, detail=str(e))

# ==================== ENDPOINTS MULTI-MODEL ARENA ====================

@app.post("/api/arena/run")
async def run_arena_benchmark(req: ArenaRunRequest):
    try:
        return await arena_runner.run_arena(req)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/arena/runs")
async def list_arena_runs():
    return arena_runner.list_runs()

@app.get("/api/arena/runs/{run_id}")
async def get_arena_run(run_id: str):
    run = arena_runner.get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Relatório da Arena não encontrado")
    return run

@app.post("/api/arena/runs/{run_id}/cancel")
async def cancel_arena_run(run_id: str):
    cancelled = arena_runner.cancel_run(run_id)
    return {"run_id": run_id, "cancelled": cancelled}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.server:app", host="127.0.0.1", port=8000, reload=False)
