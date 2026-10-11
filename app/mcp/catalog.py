import os
import re
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from app.mcp.schemas import McpServerConfig, McpToolDefinition
from app.gateway.provider_policy import validate_provider_url, SSRFSecurityError

FORBIDDEN_COMMANDS = frozenset({"npx", "npm", "cmd.exe", "cmd", "powershell.exe", "powershell", "bash", "sh"})


class McpSecurityError(RuntimeError):
    """Erro lançado para violações de segurança no ecossistema MCP."""
    pass


class McpPermissionDeniedError(McpSecurityError):
    """Erro lançado quando uma ferramenta exige autorização explícita ou HITL."""
    pass


class McpCatalog:
    """Catálogo central de servidores e ferramentas MCP com controle de acesso."""

    def __init__(self):
        self._servers: Dict[str, McpServerConfig] = {}
        self._tools: Dict[str, McpToolDefinition] = {}

    def register_server(self, cfg: McpServerConfig) -> McpServerConfig:
        """Registra um servidor MCP após validação de sintaxe e restrições de segurança."""
        if cfg.transport == "stdio":
            if not cfg.command:
                raise McpSecurityError("Servidor stdio exige comando executável válido.")
            base_cmd = os.path.basename(cfg.command).lower()
            if base_cmd in FORBIDDEN_COMMANDS:
                raise McpSecurityError(
                    f"Comando proibido para servidor MCP stdio: '{base_cmd}'. "
                    f"Execuções arbitrárias de shell e gerenciadores de pacotes não são permitidas."
                )
        elif cfg.transport == "streamable_http":
            if not cfg.url:
                raise McpSecurityError("Servidor streamable_http exige URL válida.")
            try:
                # Validação anti-SSRF do gateway
                validate_provider_url(cfg.url)
            except (SSRFSecurityError, ValueError) as e:
                raise McpSecurityError(f"URL de servidor MCP rejeitada pela política anti-SSRF: {e}")

        self._servers[cfg.id] = cfg
        return cfg

    def get_server(self, server_id: str) -> Optional[McpServerConfig]:
        return self._servers.get(server_id)

    def list_servers(self) -> List[McpServerConfig]:
        return list(self._servers.values())

    def register_tool(self, tool: McpToolDefinition) -> McpToolDefinition:
        """Registra uma ferramenta sob um servidor já homologado."""
        if tool.server_id not in self._servers:
            raise McpSecurityError(f"Servidor '{tool.server_id}' não está registrado no catálogo.")

        # Efeitos de escrita e destrutivos forçam HITL por padrão
        if tool.effect in {"write", "destructive"}:
            tool.requires_hitl = True

        self._tools[tool.name] = tool
        return tool

    def get_tool(self, tool_name: str) -> Optional[McpToolDefinition]:
        return self._tools.get(tool_name)

    def list_tools(self) -> List[McpToolDefinition]:
        return list(self._tools.values())

    def check_permission(self, tool_name: str, confirmed_hitl: bool = False) -> McpToolDefinition:
        """Verifica se a ferramenta está habilitada e se possui as permissões necessárias."""
        tool = self.get_tool(tool_name)
        if not tool:
            raise McpSecurityError(f"Ferramenta MCP desconhecida ou não registrada: '{tool_name}'")

        if not tool.enabled:
            raise McpSecurityError(f"Ferramenta '{tool_name}' está desabilitada no catálogo.")

        server = self.get_server(tool.server_id)
        if not server or not server.enabled:
            raise McpSecurityError(f"Servidor '{tool.server_id}' associado à ferramenta está desabilitado.")

        if tool.requires_hitl and not confirmed_hitl:
            raise McpPermissionDeniedError(
                f"A ferramenta '{tool_name}' possui efeito '{tool.effect}' e exige confirmação humana (HITL) explícita."
            )

        return tool
