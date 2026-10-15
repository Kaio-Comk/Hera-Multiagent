"""Cola da rota `/v1/messages` (formato Anthropic) sobre o roteador OpenAI do Hera.

O corpo do Claude é traduzido para `chat/completions`, passa pelo MESMO caminho de `/v1/chat/completions`
(chaves de cliente, limites, DLP, fallback, telemetria) e a resposta volta no formato do Claude.
"""
from __future__ import annotations

import json
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from app.gateway.translator import new_stream_state, translate_request, translate_stream_chunk
from app.gateway.translator.concerns import from_openai_finish

_ERROR_TYPES = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error",
                404: "not_found_error", 413: "request_too_large", 429: "rate_limit_error", 529: "overloaded_error"}


def error_body(status: int, message: str) -> dict:
    return {"type": "error", "error": {"type": _ERROR_TYPES.get(status, "api_error" if status >= 500 else
                                                                  "invalid_request_error"), "message": str(message)}}


def claude_body_to_chat(body: Any) -> dict:
    """Corpo `/v1/messages` -> dict de `ChatCompletionRequest`. Levanta ValueError se o corpo não for válido."""
    if not isinstance(body, dict):
        raise ValueError("o corpo precisa ser um objeto JSON")
    if not isinstance(body.get("messages"), list) or not body["messages"]:
        raise ValueError("'messages' é obrigatório e não pode ser vazio")
    if not isinstance(body.get("model"), str) or not body["model"].strip():
        raise ValueError("'model' é obrigatório")
    if not isinstance(body.get("max_tokens"), int) or body["max_tokens"] < 1:
        raise ValueError("'max_tokens' é obrigatório (inteiro maior que 0)")
    chat = translate_request("claude", "openai", body["model"], body, bool(body.get("stream")))
    if body.get("top_p") is not None:
        chat["top_p"] = body["top_p"]
    if body.get("stop_sequences"):
        chat["stop"] = body["stop_sequences"]
    chat.pop("reasoning", None)
    chat.pop("reasoning_effort", None)
    # o schema do gateway só aceita ferramentas `function`; ferramentas nativas do Claude não têm equivalente
    if chat.get("tools"):
        chat["tools"] = [t for t in chat["tools"] if t.get("type") == "function"] or None
    if body.get("temperature") is not None:
        chat["temperature"] = min(float(body["temperature"]), 2.0)
    return {k: v for k, v in chat.items() if v is not None}


def chat_response_to_claude(resp: Dict[str, Any], model: Optional[str] = None) -> dict:
    """`chat.completion` (dict) -> mensagem do Claude (resposta NÃO streaming)."""
    choice = (resp.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    content: List[dict] = []
    if msg.get("reasoning_content"):
        content.append({"type": "thinking", "thinking": msg["reasoning_content"], "signature": ""})
    text = msg.get("content")
    if isinstance(text, list):
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    if text:
        content.append({"type": "text", "text": text})
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except (ValueError, TypeError):
            args = {}
        content.append({"type": "tool_use", "id": tc.get("id") or f"toolu_{int(time.time() * 1000)}",
                        "name": fn.get("name"), "input": args if isinstance(args, dict) else {}})
    usage = resp.get("usage") or {}
    cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    out_usage = {"input_tokens": max((usage.get("prompt_tokens") or 0) - cached, 0),
                 "output_tokens": usage.get("completion_tokens") or 0}
    if cached:
        out_usage["cache_read_input_tokens"] = cached
    return {"id": f"msg_{str(resp.get('id', '')).replace('chatcmpl-', '') or int(time.time())}", "type": "message",
            "role": "assistant", "model": model or resp.get("model"), "content": content or [{"type": "text", "text": ""}],
            "stop_reason": from_openai_finish(choice.get("finish_reason"), "claude"), "stop_sequence": None,
            "usage": out_usage}


def _event(ev: dict) -> str:
    return f"event: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"


async def openai_sse_to_claude_sse(chunks: AsyncIterator[Any], model: Optional[str] = None) -> AsyncIterator[str]:
    """Consome o SSE do `/v1/chat/completions` (`data: {...}`) e emite o SSE do Claude (`event:` + `data:`)."""
    state = new_stream_state("openai", "claude")
    closed = False
    async for raw in chunks:
        text = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                continue
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("error"):
                err = data["error"]
                yield _event(error_body(int(err.get("code") or 500), err.get("message") or "erro no stream"))
                closed = True
                continue
            if model and isinstance(data, dict):
                data["model"] = model
            for ev in translate_stream_chunk("openai", "claude", data, state) or []:
                if ev["type"] == "message_stop":
                    closed = True
                yield _event(ev)
    if not closed and state.get("started"):          # upstream terminou sem finish_reason: fecha o que ficou aberto
        for ev in translate_stream_chunk("openai", "claude", {
                "choices": [{"delta": {}, "finish_reason": "stop"}]}, state) or []:
            yield _event(ev)
