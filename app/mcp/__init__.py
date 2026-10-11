"""HERA Multiagent - Model Context Protocol (MCP) Integration Module.

Fornece catálogo de ferramentas e servidores locais/remotos com validação
de schemas, prevenção de SSRF, restrições HITL para ações destrutivas e DLP de saída.
"""

from app.mcp.schemas import (
    McpServerConfig,
    McpToolDefinition,
    McpToolCallRequest,
    McpToolCallResult,
)
from app.mcp.catalog import McpCatalog, McpSecurityError, McpPermissionDeniedError
from app.mcp.client import McpClientManager

__all__ = [
    "McpServerConfig",
    "McpToolDefinition",
    "McpToolCallRequest",
    "McpToolCallResult",
    "McpCatalog",
    "McpClientManager",
    "McpSecurityError",
    "McpPermissionDeniedError",
]
