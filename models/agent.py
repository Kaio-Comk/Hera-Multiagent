from typing import List, Dict, Optional
from pydantic import BaseModel, Field

class HealthStatus(BaseModel):
    healthy: bool
    version: Optional[str] = None
    available_models: List[str] = Field(default_factory=list)
    error_message: Optional[str] = None

class AgentCapabilities(BaseModel):
    supports_code_edit: bool = False
    supports_terminal: bool = False
    supports_streaming: bool = True
    supports_json_schema: bool = True
    supports_vision: bool = False
    is_local_offline: bool = False
