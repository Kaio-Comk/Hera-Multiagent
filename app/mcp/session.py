"""Sessão MCP: handshake (`initialize`), descoberta (`tools/list`) e chamada (`tools/call`)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from app.mcp.transport import McpTransportError

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
MAX_TOOLS = 500
MAX_PAGES = 20
MAX_OUTPUT_CHARS = 100_000
MAX_SCHEMA_BYTES = 20_000
MAX_DESCRIPTION = 1000
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


@dataclass
class RemoteTool:
    name: str
    description: str
    input_schema: Dict[str, Any]
    read_only_hint: bool = False


def clean_text(value: Any, limit: int) -> str:
    """Texto vindo do servidor é NÃO CONFIÁVEL: sem caracteres de controle e com tamanho limitado
    (descrições são vetor clássico de prompt injection)."""
    text = _CTRL.sub("", str(value or ""))
    return text[:limit]


class McpSession:
    def __init__(self, transport):
        self.transport = transport
        self.server_name = ""
        self.protocol_version = ""

    async def initialize(self, timeout: float) -> None:
        result = await self.transport.request("initialize", {
            "protocolVersion": PROTOCOL_VERSIONS[0],
            "capabilities": {},                                   # não oferecemos sampling/roots/elicitation
            "clientInfo": {"name": "hera-multiagent", "version": "1.0"},
        }, timeout)
        if not isinstance(result, dict) or result.get("protocolVersion") not in PROTOCOL_VERSIONS:
            raise McpTransportError(
                f"Versão de protocolo MCP não suportada: {(result or {}).get('protocolVersion')!r}")
        self.protocol_version = result["protocolVersion"]
        self.server_name = clean_text((result.get("serverInfo") or {}).get("name"), 80)
        if hasattr(self.transport, "set_protocol_version"):
            self.transport.set_protocol_version(self.protocol_version)
        await self.transport.notify("notifications/initialized")

    async def list_tools(self, timeout: float) -> List[RemoteTool]:
        tools: List[RemoteTool] = []
        cursor: Optional[str] = None
        for _ in range(MAX_PAGES):
            result = await self.transport.request("tools/list", {"cursor": cursor} if cursor else {}, timeout)
            if not isinstance(result, dict):
                raise McpTransportError("Resposta inválida de tools/list")
            for item in result.get("tools") or []:
                if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                    continue
                name = item["name"]
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name):
                    continue                                       # nome estranho: ignora (não registra)
                schema = item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else {}
                if len(json.dumps(schema)) > MAX_SCHEMA_BYTES:
                    schema = {}                                    # schema gigante: descarta
                hint = bool((item.get("annotations") or {}).get("readOnlyHint"))
                tools.append(RemoteTool(name, clean_text(item.get("description"), MAX_DESCRIPTION), schema, hint))
                if len(tools) >= MAX_TOOLS:
                    return tools
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: Dict[str, Any], timeout: float) -> Tuple[str, bool]:
        """(texto, is_error). Só conteúdo de texto é repassado; imagem/áudio/recurso viram um marcador."""
        result = await self.transport.request("tools/call", {"name": name, "arguments": arguments}, timeout)
        if not isinstance(result, dict):
            raise McpTransportError("Resposta inválida de tools/call")
        parts: List[str] = []
        for item in result.get("content") or []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(f"[conteúdo '{clean_text(item.get('type'), 20)}' omitido]")
        if not parts and result.get("structuredContent") is not None:
            parts.append(json.dumps(result["structuredContent"], ensure_ascii=False))
        text = "\n".join(parts)
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[:MAX_OUTPUT_CHARS] + f"\n[saída truncada em {MAX_OUTPUT_CHARS} caracteres]"
        return text, bool(result.get("isError"))
