"""Cola da rota `/v1/responses` (Responses API da OpenAI) sobre o roteador de `chat/completions` do Hera."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, List, Optional

from app.gateway.translator.responses_api import (
    chat_stream_to_responses, flush_responses_stream, new_responses_stream_state, responses_to_chat_request,
)


def error_body(status: int, message: str) -> dict:
    kind = {400: "invalid_request_error", 401: "invalid_api_key", 403: "permission_denied", 404: "not_found",
            429: "rate_limit_exceeded"}.get(status, "server_error" if status >= 500 else "invalid_request_error")
    return {"error": {"message": str(message), "type": kind, "param": None, "code": kind}}


def responses_body_to_chat(body: Any) -> dict:
    """Corpo `/v1/responses` -> dict de `ChatCompletionRequest` (+ `_custom_tool_names`, que o chamador retira)."""
    if not isinstance(body, dict):
        raise ValueError("o corpo precisa ser um objeto JSON")
    if not isinstance(body.get("model"), str) or not body["model"].strip():
        raise ValueError("'model' é obrigatório")
    chat = responses_to_chat_request(body["model"], body, bool(body.get("stream")))
    if chat.get("temperature") is not None:
        chat["temperature"] = min(max(float(chat["temperature"]), 0.0), 2.0)
    # o schema do gateway só entende estes campos; o resto (ex.: truncation, parallel_tool_calls) é descartado
    allowed = {"model", "messages", "stream", "temperature", "top_p", "max_tokens", "stop", "tools", "tool_choice",
               "user", "_custom_tool_names"}
    return {k: v for k, v in chat.items() if k in allowed and v is not None}


def _event(ev: dict) -> str:
    return f"event: {ev['event']}\ndata: {json.dumps(ev['data'], ensure_ascii=False)}\n\n"


async def openai_sse_to_responses_sse(chunks: AsyncIterator[Any], custom_names: Optional[List[str]] = None,
                                      model: Optional[str] = None) -> AsyncIterator[str]:
    """Consome o SSE do `/v1/chat/completions` e emite o SSE da Responses API (`event:` + `data:`)."""
    state = new_responses_stream_state(custom_names)
    state["model"] = model
    failed = False
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
                state["seq"] += 1
                yield _event({"event": "response.failed", "data": {
                    "type": "response.failed", "sequence_number": state["seq"], "response": {
                        "id": state["response_id"], "object": "response", "status": "failed",
                        "error": {"code": "server_error", "message": str(err.get("message") or "erro no stream")}}}})
                failed = True
                continue
            for ev in chat_stream_to_responses(data, state):
                yield _event(ev)
    if not failed:
        for ev in flush_responses_stream(state):
            yield _event(ev)
