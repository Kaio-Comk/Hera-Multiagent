from enum import Enum
from typing import List, Dict, Optional, Any
from pydantic import BaseModel, Field
import time

class TaskStatus(str, Enum):
    PENDING = "PENDING"
    PLANNING = "PLANNING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    VALIDATING = "VALIDATING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

class AgentTask(BaseModel):
    id: str
    parent_id: Optional[str] = None
    title: str
    instruction: str
    task_type: str = "coding"  # coding, research, review, test, docs
    workspace: str = ""
    status: TaskStatus = TaskStatus.PENDING
    dependencies: List[str] = Field(default_factory=list)
    timeout_seconds: int = 900
    max_retries: int = 2
    retry_count: int = 0
    assigned_agent: Optional[str] = None
    assigned_model: Optional[str] = None
    acceptance_criteria: List[str] = Field(default_factory=list)
    validation_commands: List[str] = Field(default_factory=list)
    routing_reason: Optional[str] = None
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    completed_at: Optional[float] = None

class AgentResult(BaseModel):
    task_id: str
    status: TaskStatus
    summary: str
    output_text: str = ""
    artifacts: List[str] = Field(default_factory=list)
    execution_time_seconds: float = 0.0
    error_message: Optional[str] = None
    validation_evidence: Dict[str, bool] = Field(default_factory=dict)
    metadata: Dict[str, Any] = Field(default_factory=dict)       # custo, tokens, duracao (quando o motor informa)
