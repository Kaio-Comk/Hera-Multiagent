import time
import json
import asyncio
import re
from pathlib import Path
from typing import Dict, Any, Optional, Callable

from app.mcp.schemas import McpToolCallRequest, McpToolCallResult, McpToolDefinition
from app.mcp.catalog import McpCatalog, McpSecurityError, McpPermissionDeniedError
from app.gateway.privacy import GatewayPrivacyManager
from app.privacy_guard import PrivacyGuard
from app.mcp.launch import McpLaunchError, build_argv, build_env, validate_stdio_server
from app.mcp.session import McpSession
from app.mcp.transport import HttpTransport, McpTransportError, StdioTransport

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
        roots_provider: Optional[Callable[[], list]] = None,
        transport_factory: Optional[Callable[[Any], Any]] = None,
    ):
        self.catalog = catalog or McpCatalog()
        self.privacy_manager = privacy_manager or GatewayPrivacyManager()
        self.enabled = enabled
        self._handlers: Dict[str, Callable] = {}
        # Cliente MCP real: sessões por servidor (iniciadas sob demanda), raízes permitidas p/ scripts.
        self._roots_provider = roots_provider or (lambda: [Path.cwd()])
        self._transport_factory = transport_factory            # injeção para testes (HTTP simulado)
        self._sessions: Dict[str, McpSession] = {}
        self._session_locks: Dict[str, asyncio.Lock] = {}

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
        """Validação local de JSON Schema (subconjunto): required, type, enum, maxLength, additionalProperties."""
        if not schema:
            return
        for req_field in schema.get("required", []):
            if req_field not in args:
                raise McpSecurityError(f"Argumento obrigatório ausente: '{req_field}'")
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = [k for k in args if k not in properties]
            if extra:
                raise McpSecurityError(f"Argumentos não permitidos: {extra[:3]}")
        py_types = {"string": str, "integer": int, "number": (int, float), "boolean": bool,
                    "array": list, "object": dict}
        for key, val in args.items():
            prop = properties.get(key)
            if not isinstance(prop, dict):
                continue
            prop_type = prop.get("type")
            expected = py_types.get(prop_type)
            if expected is not None:
                wrong = not isinstance(val, expected) or (prop_type in ("integer", "number") and isinstance(val, bool))
                if wrong:
                    nome = {"integer": "inteiro", "number": "número", "array": "lista", "object": "objeto"}.get(prop_type, prop_type)
                    raise McpSecurityError(f"Tipo inválido para '{key}': esperado {nome}, recebido {type(val).__name__}")
            if "enum" in prop and val not in prop["enum"]:
                raise McpSecurityError(f"Valor inválido para '{key}': fora das opções permitidas")
            if prop_type == "string" and isinstance(prop.get("maxLength"), int) and len(val) > prop["maxLength"]:
                raise McpSecurityError(f"'{key}' excede o tamanho máximo de {prop['maxLength']}")

    # ── sessões MCP reais ──────────────────────────────────────────────────────
    def _make_transport(self, server):
        if self._transport_factory is not None:
            return self._transport_factory(server)
        if server.transport == "streamable_http":
            return HttpTransport(server.url)
        validate_stdio_server(server, self._roots_provider())
        roots = self._roots_provider()
        return StdioTransport(build_argv(server), build_env(server), cwd=str(roots[0]) if roots else None)

    async def _session_for(self, server) -> McpSession:
        lock = self._session_locks.setdefault(server.id, asyncio.Lock())
        async with lock:
            session = self._sessions.get(server.id)
            if session is not None and getattr(session.transport, "alive", False):
                return session
            if session is not None:                                  # morreu: limpa e recria
                await self._drop_session(server.id)
            try:
                transport = self._make_transport(server)
            except McpLaunchError as exc:
                raise McpTransportError(f"Não foi possível iniciar o servidor MCP '{server.id}': {exc}") from exc
            try:
                if hasattr(transport, "start"):
                    await transport.start()
                session = McpSession(transport)
                await session.initialize(server.timeout_seconds)
            except (McpTransportError, McpLaunchError, OSError) as exc:
                await transport.close()
                raise McpTransportError(f"Não foi possível iniciar o servidor MCP '{server.id}': {exc}") from exc
            except BaseException:
                await transport.close()
                raise
            self._sessions[server.id] = session
            return session

    async def _drop_session(self, server_id: str) -> None:
        session = self._sessions.pop(server_id, None)
        if session is not None:
            try:
                await session.transport.close()
            except Exception:                                        # noqa: BLE001
                pass

    async def discover(self, server_id: str):
        """Lança/abre o servidor, lista as ferramentas (tools/list) e as registra DESLIGADAS."""
        server = self.catalog.get_server(server_id)
        if server is None or not server.enabled:
            raise McpSecurityError(f"Servidor '{server_id}' não está registrado ou está desabilitado.")
        session = await self._session_for(server)
        try:
            remote = await session.list_tools(server.timeout_seconds)
        except McpTransportError:
            await self._drop_session(server_id)
            raise
        return self.catalog.register_discovered_tools(server_id, remote)

    async def close_all(self) -> None:
        for server_id in list(self._sessions):
            await self._drop_session(server_id)

    async def _execute_remote(self, tool, server, req) -> McpToolCallResult:
        start = time.perf_counter()
        if server.transport == "streamable_http":
            has_secret, kinds = PrivacyGuard.scan_content(json.dumps(req.arguments, ensure_ascii=False))
            if has_secret:
                raise McpSecurityError(
                    f"Argumentos com segredo ({', '.join(kinds)}) não podem ser enviados a um servidor MCP remoto.")
        try:
            session = await self._session_for(server)
            text, is_error = await session.call_tool(tool.remote_name, req.arguments, server.timeout_seconds)
        except McpTransportError as exc:
            # Qualquer erro de transporte deixa a sessao em estado desconhecido (o processo pode ainda
            # parecer vivo por instantes): descarta, e a proxima chamada abre uma nova.
            await self._drop_session(server.id)
            return McpToolCallResult(tool_name=req.tool_name, success=False, output="",
                                     duration_ms=(time.perf_counter() - start) * 1000.0,
                                     error=self._redact_output(str(exc)))
        clean = self._redact_output(text)                            # a saída é NÃO CONFIÁVEL: DLP sempre
        return McpToolCallResult(tool_name=req.tool_name, success=not is_error,
                                 output="" if is_error else clean, error=clean if is_error else None,
                                 duration_ms=(time.perf_counter() - start) * 1000.0)

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
        if not handler and tool.remote_name and server is not None:
            return await self._execute_remote(tool, server, req)
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
