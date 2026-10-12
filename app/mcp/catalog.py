import hashlib
import json
import os
import re
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlsplit

from app.mcp.schemas import McpServerConfig, McpToolDefinition
from app.gateway.provider_policy import validate_provider_url, SSRFSecurityError

FORBIDDEN_COMMANDS = frozenset({"npx", "npm", "cmd.exe", "cmd", "powershell.exe", "powershell", "bash", "sh"})
# Fase 5: lista de PERMITIDOS (a lista negra acima fica só para dar a mensagem certa). Um servidor
# MCP stdio só pode ser um destes executáveis, citado pelo NOME (sem caminho nem argumentos).
DEFAULT_ALLOWED_STDIO_COMMANDS = frozenset({"python", "python3", "py"})


class McpSecurityError(RuntimeError):
    """Erro lançado para violações de segurança no ecossistema MCP."""
    pass


class McpPermissionDeniedError(McpSecurityError):
    """Erro lançado quando uma ferramenta exige autorização explícita ou HITL."""
    pass


class McpCatalog:
    """Catálogo central de servidores e ferramentas MCP com controle de acesso."""

    def __init__(self, allowed_commands: Optional[Iterable[str]] = None):
        self._servers: Dict[str, McpServerConfig] = {}
        self._tools: Dict[str, McpToolDefinition] = {}
        self.allowed_commands = frozenset(c.strip().lower() for c in (allowed_commands or DEFAULT_ALLOWED_STDIO_COMMANDS)
                                          if isinstance(c, str) and c.strip())

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
            command = cfg.command.strip()
            if any(ch in command for ch in ("/", "\\", " ", ";", "|", "&", "\"", "'")) or command.startswith("-"):
                raise McpSecurityError(
                    "O comando de um servidor MCP stdio deve ser só o NOME do executável "
                    "(sem caminho, argumentos nem metacaracteres)."
                )
            name = command.lower()
            if name.endswith(".exe"):
                name = name[:-4]
            if name not in self.allowed_commands:
                raise McpSecurityError(
                    f"Comando '{command}' fora da lista de permitidos para servidores MCP "
                    f"stdio: {sorted(self.allowed_commands)}."
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

    def register_discovered_tools(self, server_id: str, remote_tools) -> List[McpToolDefinition]:
        """Registra as ferramentas descobertas via `tools/list`. O servidor é NÃO CONFIÁVEL, então toda
        ferramenta nova nasce DESLIGADA, com efeito `write` e exigindo HITL, até um humano liberá-la
        (`set_tool_policy`). Se a descrição ou o schema mudarem depois, a aprovação é invalidada."""
        if server_id not in self._servers:
            raise McpSecurityError(f"Servidor '{server_id}' não está registrado no catálogo.")
        registered: List[McpToolDefinition] = []
        for rt in remote_tools:
            name = f"{server_id}.{rt.name}"
            fp = hashlib.sha256(json.dumps([rt.description, rt.input_schema], sort_keys=True).encode()).hexdigest()
            previous = self._tools.get(name)
            if previous is not None and previous.fingerprint == fp:
                registered.append(previous)                        # nada mudou: mantém a decisão humana
                continue
            tool = McpToolDefinition(
                name=name, description=rt.description, server_id=server_id, parameters_schema=rt.input_schema,
                effect="write", requires_hitl=True, enabled=False, remote_name=rt.name, fingerprint=fp)
            self._tools[name] = tool
            registered.append(tool)
        return registered

    def set_tool_policy(self, tool_name: str, *, enabled: bool, effect: Optional[str] = None,
                        requires_hitl: Optional[bool] = None) -> McpToolDefinition:
        """Decisão HUMANA sobre uma ferramenta (liberar/bloquear, efeito, exigir confirmação)."""
        tool = self._tools.get(tool_name)
        if tool is None:
            raise McpSecurityError(f"Ferramenta MCP desconhecida: '{tool_name}'")
        data = tool.model_dump()
        data["enabled"] = enabled
        if effect is not None:
            data["effect"] = effect
        if requires_hitl is not None:
            data["requires_hitl"] = requires_hitl
        updated = McpToolDefinition(**data)
        if updated.effect in ("write", "network", "destructive") and not updated.requires_hitl:
            raise McpSecurityError("Ferramentas com efeito write/network/destructive precisam exigir HITL.")
        self._tools[tool_name] = updated
        return updated

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
