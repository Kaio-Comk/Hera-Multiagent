import time
import json
import asyncio
import re
from typing import Dict, Any, Optional, Callable

from app.mcp.schemas import McpToolCallRequest, McpToolCallResult, McpToolDefinition
from app.mcp.catalog import McpCatalog, McpSecurityError, McpPermissionDeniedError
from app.gateway.privacy import GatewayPrivacyManager

# Padrões para mascaramento de segredos na saída da ferramenta
SECRET_REDACT_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9_-]{20,}"), "[REDACTED_API_KEY]"),
    (re.compile(r"ghp_[a-zA-Z0-9]{30,}"), "[REDACTED_GITHUB_TOKEN]"),
    (re.compile(r"AIza[0-9A-Za-z-_]{35}"), "[REDACTED_GOOGLE_KEY]"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"), "[REDACTED_PRIVATE_KEY]"),
]


class McpClientManager:
    """Gerenciador de execução segura de ferramentas MCP sob controle de segurança e DLP."""

    def __init__(
        self,
        catalog: Optional[McpCatalog] = None,
        privacy_manager: Optional[GatewayPrivacyManager] = None,
        enabled: bool = True,
    ):
        self.catalog = catalog or McpCatalog()
        self.privacy_manager = privacy_manager or GatewayPrivacyManager()
        self.enabled = enabled
        self._handlers: Dict[str, Callable] = {}

    def register_tool_handler(self, tool_name: str, handler: Callable):
        """Registra a implementação executável de uma ferramenta local confiável."""
        self._handlers[tool_name] = handler

    def _redact_output(self, raw_output: str) -> str:
        """Aplica sanitização DLP na saída da ferramenta antes de devolvê-la."""
        cleaned = raw_output
        for pat, replacement in SECRET_REDACT_PATTERNS:
            cleaned = pat.sub(replacement, cleaned)
        return cleaned

    def _validate_schema(self, schema: Dict[str, Any], args: Dict[str, Any]) -> None:
        """Validação local preliminar de JSON Schema para argumentos da ferramenta."""
        if not schema:
            return
        required = schema.get("required", [])
        for req_field in required:
            if req_field not in args:
                raise McpSecurityError(f"Argumento obrigatório ausente: '{req_field}'")

        properties = schema.get("properties", {})
        for key, val in args.items():
            if key in properties:
                prop_type = properties[key].get("type")
                if prop_type == "string" and not isinstance(val, str):
                    raise McpSecurityError(f"Tipo inválido para '{key}': esperado string, recebido {type(val).__name__}")
                elif prop_type == "integer" and not isinstance(val, int):
                    raise McpSecurityError(f"Tipo inválido para '{key}': esperado inteiro, recebido {type(val).__name__}")
                elif prop_type == "boolean" and not isinstance(val, bool):
                    raise McpSecurityError(f"Tipo inválido para '{key}': esperado boolean, recebido {type(val).__name__}")

    async def execute_tool(self, req: McpToolCallRequest) -> McpToolCallResult:
        """Executa a ferramenta MCP sob verificações de feature flag, permissões, schema e DLP."""
        if not self.enabled:
            raise McpSecurityError("Integração com MCP está desabilitada por Feature Flag.")

        # 1. Permissões e catálogo
        tool = self.catalog.check_permission(req.tool_name, confirmed_hitl=req.confirmed_hitl)
        server = self.catalog.get_server(tool.server_id)
        timeout = server.timeout_seconds if server else 30.0

        # 2. Validação de Schema
        self._validate_schema(tool.parameters_schema, req.arguments)

        # 3. Execução
        handler = self._handlers.get(req.tool_name)
        if not handler:
            return McpToolCallResult(
                tool_name=req.tool_name,
                success=False,
                output="",
                error=f"Nenhum handler registrado para a ferramenta '{req.tool_name}'."
            )

        start_time = time.perf_counter()
        try:
            if asyncio.iscoroutinefunction(handler):
                raw_res = await asyncio.wait_for(handler(**req.arguments), timeout=timeout)
            else:
                raw_res = await asyncio.to_thread(handler, **req.arguments)

            duration_ms = (time.perf_counter() - start_time) * 1000.0

            # 4. Formatação e DLP
            output_str = str(raw_res) if not isinstance(raw_res, str) else raw_res
            sanitized_output = self._redact_output(output_str)

            return McpToolCallResult(
                tool_name=req.tool_name,
                success=True,
                output=sanitized_output,
                duration_ms=duration_ms,
            )
        except asyncio.TimeoutError:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            return McpToolCallResult(
                tool_name=req.tool_name,
                success=False,
                output="",
                duration_ms=duration_ms,
                error=f"Execução da ferramenta '{req.tool_name}' excedeu o timeout de {timeout}s."
            )
        except Exception as e:
            duration_ms = (time.perf_counter() - start_time) * 1000.0
            return McpToolCallResult(
                tool_name=req.tool_name,
                success=False,
                output="",
                duration_ms=duration_ms,
                error=f"Erro na execução da ferramenta: {e}"
            )
