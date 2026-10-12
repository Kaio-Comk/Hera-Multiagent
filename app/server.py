import os
import sys
import logging
import json
import time
import uuid
import asyncio
import psutil
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, StreamingResponse
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
from adapters.ollama import OllamaAdapter
from adapters.nine_router import NineRouterAdapter
from adapters.claude_code import ClaudeCodeAdapter
from adapters.antigravity import AntigravityAdapter

from app.gateway.schemas import ChatCompletionRequest, ChatCompletionResponse
from app.gateway.privacy import GatewayPrivacyManager
from app.gateway.providers import OllamaGatewayAdapter, GatewayProviderError
from app.gateway.provider_policy import SSRFSecurityError, validate_provider_url
from app.gateway.safe_transport import make_safe_async_client
from app.gateway.factory import build_gateway_adapters
from app.gateway.tier_router import TierRouter

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
app.add_middleware(LocalSecurityMiddleware, settings=SECURITY)

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
    "9router": NineRouterAdapter(
        base_url=cfg["engines"]["nine_router"].get("base_url", "http://127.0.0.1:20128/v1"),
        default_model=cfg["engines"]["nine_router"].get("default_model", "alicode-intl/qwen3.5-plus")
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
)

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
    enabled=cfg.get("feature_flags", {}).get("enable_mcp", True)
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
                "healthy": status.healthy,
                "version": status.version,
                "available_models": status.available_models,
                "error_message": status.error_message
            }
        except Exception as e:
            results[name] = {
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

ALLOWED_GENERAL_KEYS = {"host", "port", "default_offline", "default_engine", "vault_dir", "db_path",
                        "allowed_repo_roots"}
# Ajustes de INICIALIZACAO: a API nao pode muda-los (so editando o config.yaml com o servidor parado).
FROZEN_GENERAL_KEYS = ("host", "port", "vault_dir", "db_path", "allowed_repo_roots")
ALLOWED_ENGINES = {"ollama", "nine_router", "9router", "claude_code", "antigravity"}
ALLOWED_ENGINE_FIELDS = {"enabled", "base_url", "default_model", "timeout"}
LOCAL_ONLY_URL_ENGINES = {"ollama", "nine_router", "9router"}


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
    router.default_offline = req.general.get("default_offline", False)
    
    # Atualizar defaults nos adaptadores
    if "ollama" in req.engines and "ollama" in adapters:
        adapters["ollama"].default_model = req.engines["ollama"].get("default_model", "qwen2.5-coder:1.5b")
    nine_router_cfg = req.engines.get("nine_router") or req.engines.get("9router")
    if nine_router_cfg:
        nr_adapter = adapters.get("9router") or adapters.get("nine_router")
        if nr_adapter:
            nr_adapter.default_model = nine_router_cfg.get("default_model", "alicode-intl/qwen3.5-plus")
        
    broadcast_log("⚙️ Configurações atualizadas e sincronizadas com sucesso.", level="success")
    return {"status": "ok", "config": new_cfg}

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
async def list_gateway_models():
    """Retorna lista de modelos disponíveis conforme especificação OpenAI."""
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
    return {"object": "list", "data": models_list}


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

@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    """Endpoint OpenAI-compatible para chat completions (com streaming SSE, fallback e DLP)."""
    global gateway_http_client
    if gateway_http_client is None or gateway_http_client.is_closed:
        client_to_use = make_safe_async_client(timeout=60.0)
        should_close = True
    else:
        client_to_use = gateway_http_client
        should_close = False

    enable_rtk = cfg.get("feature_flags", {}).get("enable_rtk", True)

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

@app.get("/api/mcp/servers")
async def list_mcp_servers():
    return mcp_client.catalog.list_servers()

@app.post("/api/mcp/servers")
async def register_mcp_server(cfg: McpServerConfig):
    try:
        return mcp_client.catalog.register_server(cfg)
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
