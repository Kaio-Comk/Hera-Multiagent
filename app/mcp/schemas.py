from typing import Dict, List, Optional, Any, Literal
from pydantic import BaseModel, Field


class McpServerConfig(BaseModel):
    """Configuração declarativa e segura de um servidor MCP registrado."""
    id: str
    name: str
    transport: Literal["stdio", "streamable_http"] = "stdio"
    command: Optional[str] = None  # Executável para stdio (ex: python)
    args: List[str] = Field(default_factory=list)
    env: Dict[str, str] = Field(default_factory=dict)
    url: Optional[str] = None  # URL para streamable_http
    enabled: bool = True
    timeout_seconds: float = 30.0


class McpToolDefinition(BaseModel):
    """Definição e contrato de segurança de uma ferramenta exposta via MCP."""
    name: str
    description: str
    server_id: str
    parameters_schema: Dict[str, Any] = Field(default_factory=dict)
    effect: Literal["read", "write", "network", "destructive"] = "read"
    requires_hitl: bool = False
    enabled: bool = True


class McpToolCallRequest(BaseModel):
    """Requisição de invocação de uma ferramenta MCP."""
    tool_name: str
    arguments: Dict[str, Any] = Field(default_factory=dict)
    task_id: Optional[str] = None
    user_id: str = "operator"
    confirmed_hitl: bool = False


class McpToolCallResult(BaseModel):
    """Resultado determinístico de execução com sanitização de saída."""
    tool_name: str
    success: bool
    output: str
    duration_ms: float = 0.0
    error: Optional[str] = None
