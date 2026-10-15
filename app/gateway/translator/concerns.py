"""Lógica compartilhada entre tradutores (mapeamento de finish_reason, data URI, usage, max_tokens)."""
from __future__ import annotations

import json
import re
from typing import Any, Optional

from app.gateway.translator import schema as S

_DATA_URI = re.compile(r"^data:([^;]+);base64,([\s\S]+)$")


def safe_parse_json(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return fallback


def parse_data_uri(url: Any) -> Optional[dict]:
    if not isinstance(url, str):
        return None
    m = _DATA_URI.match(url)
    return {"mime": m.group(1), "base64": m.group(2)} if m else None


def encode_data_uri(mime: str, b64: str) -> str:
    return f"data:{mime};base64,{b64}"


def to_openai_finish(reason: Optional[str], fmt: str) -> str:
    if fmt == "claude":
        return {S.CLAUDE_END_TURN: S.FINISH_STOP, S.CLAUDE_MAX_TOKENS: S.FINISH_LENGTH,
                S.CLAUDE_TOOL_USE: S.FINISH_TOOL_CALLS, S.CLAUDE_STOP_SEQUENCE: S.FINISH_STOP,
                # recusa é turno bloqueado, não parada limpa: o cliente OpenAI precisa distinguir
                S.CLAUDE_REFUSAL: S.FINISH_CONTENT_FILTER}.get(reason or "", S.FINISH_STOP)
    return reason or S.FINISH_STOP


def from_openai_finish(reason: Optional[str], fmt: str) -> Optional[str]:
    if fmt == "claude":
        return {S.FINISH_STOP: S.CLAUDE_END_TURN, S.FINISH_LENGTH: S.CLAUDE_MAX_TOKENS,
                S.FINISH_TOOL_CALLS: S.CLAUDE_TOOL_USE,
                S.FINISH_CONTENT_FILTER: S.CLAUDE_REFUSAL}.get(reason or "", S.CLAUDE_END_TURN)
    return reason


def adjust_max_tokens(body: dict, ceiling: int = S.DEFAULT_MAX_TOKENS) -> int:
    n = body.get("max_tokens") or S.DEFAULT_MAX_TOKENS
    tools = body.get("tools")
    if isinstance(tools, list) and tools and n < S.DEFAULT_MIN_TOKENS:
        n = S.DEFAULT_MIN_TOKENS
    budget = (body.get("thinking") or {}).get("budget_tokens")
    if budget and n <= budget:
        n = budget + 1024          # a API do Claude exige max_tokens > budget_tokens
    return min(n, ceiling)


def to_openai_usage(input_tokens: int, output_tokens: int, cache_read: int = 0, cache_creation: int = 0) -> dict:
    prompt = input_tokens + cache_read + cache_creation
    usage = {"prompt_tokens": prompt, "completion_tokens": output_tokens, "total_tokens": prompt + output_tokens}
    if cache_read or cache_creation:
        usage["prompt_tokens_details"] = {"cached_tokens": cache_read}
        if cache_creation:
            usage["prompt_tokens_details"]["cache_creation_tokens"] = cache_creation
    return usage


def collapse_text_parts(parts: list) -> Any:
    """Lista só de texto vira string (formato mais compatível); com imagem mantém a lista."""
    if parts and all(p.get("type") == "text" for p in parts):
        return "\n".join(p.get("text", "") for p in parts)
    return parts


def text_of(content: Any, sep: str = "\n") -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return sep.join(str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("type") == "text")
    return ""
