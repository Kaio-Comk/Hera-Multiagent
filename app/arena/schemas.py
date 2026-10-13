from typing import List, Optional, Literal, Dict, Any
from pydantic import BaseModel, Field
import time


class ArenaCandidate(BaseModel):
    """Candidato configurado para competir na Multi-Model Arena."""
    model_id: str
    adapter_name: str  # ollama, hera_router, claude_code, antigravity
    is_local: bool = True
    max_tokens: int = 1024
    temperature: float = 0.2


class CandidateResult(BaseModel):
    """Métricas determinísticas obtidas na avaliação de um candidato."""
    model_id: str
    adapter_name: str
    is_local: bool
    status: Literal["completed", "failed", "cancelled", "skipped_privacy"] = "completed"
    ttft_ms: float = 0.0  # Time To First Token
    duration_ms: float = 0.0
    output_text: str = ""
    tokens_generated: int = 0
    tokens_per_second: float = 0.0
    error: Optional[str] = None


class ArenaRunRequest(BaseModel):
    """Requisição de execução comparativa entre modelos."""
    prompt: str
    candidates: List[ArenaCandidate]
    system_prompt: Optional[str] = None
    task_id: Optional[str] = None
    user_id: str = "operator"


class ArenaRunReport(BaseModel):
    """Relatório estruturado com telemetria comparativa e ranking da rodada."""
    run_id: str
    task_id: Optional[str] = None
    prompt_summary: str
    privacy_classification: str
    results: List[CandidateResult]
    started_at: float
    completed_at: float
    fastest_model: Optional[str] = None
    highest_throughput_model: Optional[str] = None
