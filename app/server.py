import os
import sys
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
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.config_manager import ConfigManager
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
from app.gateway.tier_router import TierRouter
from contextlib import asynccontextmanager
import httpx

gateway_http_client: Optional[httpx.AsyncClient] = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global gateway_http_client
    gateway_http_client = httpx.AsyncClient(timeout=60.0)
    yield
    if gateway_http_client:
        await gateway_http_client.aclose()

app = FastAPI(title="Hera Multiagent API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
ollama_gw_adapter = OllamaGatewayAdapter(
    base_url=cfg["engines"]["ollama"].get("base_url", "http://localhost:11434"),
    default_model=cfg["engines"]["ollama"].get("default_model", "qwen2.5-coder:1.5b")
)
gateway_privacy_mgr = GatewayPrivacyManager(default_offline=cfg["general"].get("default_offline", False))
gateway_tier_router = TierRouter(
    adapters=[ollama_gw_adapter],
    privacy_manager=gateway_privacy_mgr,
    max_concurrent_requests=cfg.get("gateway", {}).get("max_concurrent_requests", 10)
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
class TaskRunRequest(BaseModel):
    title: str = ""
    instruction: str
    task_type: str = "coding"
    assigned_agent: Optional[str] = None
    assigned_model: Optional[str] = None
    acceptance_criteria: List[str] = []
    validation_commands: List[str] = []
    max_retries: int = 2
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

@app.post("/api/config")
async def update_config(req: ConfigUpdateRequest):
    """Salva novas configurações no disco e atualiza em tempo de execução."""
    new_cfg = {"general": req.general, "engines": req.engines}
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
@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = WEB_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
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
    return {"object": "list", "data": models_list}

@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    """Endpoint OpenAI-compatible para chat completions (com streaming SSE, fallback e DLP)."""
    global gateway_http_client
    if gateway_http_client is None or gateway_http_client.is_closed:
        client_to_use = httpx.AsyncClient(timeout=60.0)
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.server:app", host="127.0.0.1", port=8000, reload=False)
