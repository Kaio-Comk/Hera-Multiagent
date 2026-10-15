"""Tradução OpenAI <-> Claude (Messages API): requisição, resposta completa e stream de eventos SSE.

Port de `open-sse/translator/request/{openai-to-claude,claude-to-openai}.js` e
`response/{claude-to-openai,openai-to-claude}.js` (9router, MIT). Sem o cloaking do Claude Code.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from app.gateway.translator import schema as S
from app.gateway.translator.concerns import (
    adjust_max_tokens, collapse_text_parts, encode_data_uri, from_openai_finish, parse_data_uri,
    safe_parse_json, text_of, to_openai_finish, to_openai_usage,
)


# ───────────────────────────── requisição: OpenAI -> Claude ─────────────────────────────

def _claude_blocks(msg: dict) -> List[dict]:
    role, blocks = msg.get("role"), []
    content = msg.get("content")
    if role == S.ROLE_TOOL:
        return [{"type": "tool_result", "tool_use_id": msg.get("tool_call_id"), "content": content}]
    if role == S.ROLE_USER:
        if isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for part in content:
                t = part.get("type")
                if t == "text" and part.get("text"):
                    blocks.append({"type": "text", "text": part["text"]})
                elif t == "tool_result":
                    b = {"type": "tool_result", "tool_use_id": part.get("tool_use_id"), "content": part.get("content")}
                    if part.get("is_error"):
                        b["is_error"] = True
                    blocks.append(b)
                elif t == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    parsed = parse_data_uri(url)
                    if parsed:
                        blocks.append({"type": "image", "source": {"type": "base64", "media_type": parsed["mime"],
                                                                   "data": parsed["base64"]}})
                    elif url.startswith(("http://", "https://")):
                        blocks.append({"type": "image", "source": {"type": "url", "url": url}})
                elif t == "image" and part.get("source"):
                    blocks.append({"type": "image", "source": part["source"]})
                elif t == "file" and part.get("file"):
                    parsed = parse_data_uri(part["file"].get("file_data"))
                    if parsed and parsed["mime"] == "application/pdf":      # Claude só aceita PDF
                        blocks.append({"type": "document", "source": {"type": "base64", "media_type": parsed["mime"],
                                                                      "data": parsed["base64"]}})
    elif role == S.ROLE_ASSISTANT:
        if isinstance(content, list):
            for part in content:
                t = part.get("type")
                if t == "text" and part.get("text"):
                    blocks.append({"type": "text", "text": part["text"]})
                elif t == "tool_use":
                    blocks.append({"type": "tool_use", "id": part.get("id"), "name": part.get("name"),
                                   "input": part.get("input")})
                elif t == "thinking":
                    blocks.append({k: v for k, v in part.items() if k != "cache_control"})
        elif content:
            text = content if isinstance(content, str) else text_of(content)
            if text:
                blocks.append({"type": "text", "text": text})
        for tc in msg.get("tool_calls") or []:
            if tc.get("type") == "function":
                fn = tc.get("function") or {}
                blocks.append({"type": "tool_use", "id": tc.get("id"), "name": fn.get("name"),
                               "input": safe_parse_json(fn.get("arguments"), fn.get("arguments"))})
    return blocks


def _openai_tool_choice_to_claude(choice: Any) -> dict:
    if not choice:
        return {"type": "auto"}
    if isinstance(choice, str):
        return {"type": "any"} if choice == "required" else {"type": "auto"}
    if isinstance(choice, dict):
        if (choice.get("function") or {}).get("name"):          # forma OpenAI traz `.type == "function"`
            return {"type": "tool", "name": choice["function"]["name"]}
        if choice.get("type") in S.CLAUDE_TOOL_CHOICE_TYPES:
            return choice
    return {"type": "auto"}


def openai_to_claude_request(model: str, body: dict, stream: bool) -> dict:
    result: Dict[str, Any] = {"model": model, "max_tokens": adjust_max_tokens(body), "stream": stream, "messages": []}
    if body.get("temperature") is not None:
        result["temperature"] = body["temperature"]

    messages = body.get("messages") or []
    system_parts = [m["content"] if isinstance(m.get("content"), str) else text_of(m.get("content"))
                    for m in messages if m.get("role") == S.ROLE_SYSTEM]

    cur_role: Optional[str] = None
    cur_parts: List[dict] = []

    def flush() -> None:
        nonlocal cur_parts
        if cur_role and cur_parts:
            result["messages"].append({"role": cur_role, "content": cur_parts})
        cur_parts = []

    for msg in (m for m in messages if m.get("role") != S.ROLE_SYSTEM):
        new_role = S.ROLE_USER if msg.get("role") in (S.ROLE_USER, S.ROLE_TOOL) else S.ROLE_ASSISTANT
        blocks = _claude_blocks(msg)
        has_tool_use = any(b["type"] == "tool_use" for b in blocks)
        tool_results = [b for b in blocks if b["type"] == "tool_result"]
        if tool_results:   # tool_result precisa de mensagem própria logo após o tool_use
            flush()
            result["messages"].append({"role": S.ROLE_USER, "content": tool_results})
            others = [b for b in blocks if b["type"] != "tool_result"]
            if others:
                cur_role = new_role
                cur_parts.extend(others)
            continue
        if cur_role != new_role:
            flush()
            cur_role = new_role
        cur_parts.extend(blocks)
        if has_tool_use:
            flush()
    flush()

    rf = body.get("response_format") or {}
    if rf.get("type") == "json_schema" and (rf.get("json_schema") or {}).get("schema"):
        system_parts.append("You must respond with valid JSON that strictly follows this JSON schema:\n```json\n"
                            + json.dumps(rf["json_schema"]["schema"], indent=2)
                            + "\n```\nRespond ONLY with the JSON object, no other text.")
    elif rf.get("type") == "json_object":
        system_parts.append("You must respond with valid JSON. Respond ONLY with a JSON object, no other text.")
    if system_parts:
        result["system"] = [{"type": "text", "text": "\n".join(system_parts)}]

    if isinstance(body.get("tools"), list):
        tools = []
        for tool in body["tools"]:
            if tool.get("type") and tool["type"] != "function":
                tools.append(tool)           # ferramenta nativa (ex.: web_search_*): passa direto
                continue
            data = tool.get("function") or tool
            tools.append({"name": data.get("name"), "description": data.get("description") or "",
                          "input_schema": data.get("parameters") or data.get("input_schema")
                          or {"type": "object", "properties": {}, "required": []}})
        if tools:
            result["tools"] = tools
    if body.get("tool_choice"):
        result["tool_choice"] = _openai_tool_choice_to_claude(body["tool_choice"])
    return result


# ───────────────────────────── requisição: Claude -> OpenAI ─────────────────────────────

def _tool_result_text(block: dict):
    content, images = block.get("content"), []
    if isinstance(content, str):
        return content, images
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("type") == "image" and (c.get("source") or {}).get("type") == "base64":
                images.append({"type": "image_url", "image_url": {
                    "url": encode_data_uri(c["source"]["media_type"], c["source"]["data"])}})
        texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
        return ("\n".join(texts) or ("" if images else json.dumps(content))), images
    return (json.dumps(content) if content else ""), images


def _convert_claude_message(msg: dict):
    content = msg.get("content")
    if isinstance(content, dict):
        content = [content]
    if msg.get("role") == S.ROLE_SYSTEM:   # system no meio da conversa vira turno de usuário
        parts = ([c.get("text", "") for c in content if c.get("type") == "text"] if isinstance(content, list)
                 else [content if isinstance(content, str) else ""])
        text = "\n".join(p for p in parts if p)
        return {"role": S.ROLE_USER, "content": f"<instructions>\n{text}\n</instructions>"} if text.strip() else None
    role = S.ROLE_USER if msg.get("role") in (S.ROLE_USER, S.ROLE_TOOL) else S.ROLE_ASSISTANT
    if isinstance(content, str):
        return {"role": role, "content": content}
    if not isinstance(content, list):
        return None
    parts, tool_calls, tool_results = [], [], []
    for b in content:
        t = b.get("type")
        if t == "text":
            parts.append({"type": "text", "text": b.get("text", "")})
        elif t == "image" and (b.get("source") or {}).get("type") == "base64":
            parts.append({"type": "image_url", "image_url": {
                "url": encode_data_uri(b["source"]["media_type"], b["source"]["data"])}})
        elif t == "tool_use":
            tool_calls.append({"id": b.get("id"), "type": "function",
                               "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input") or {})}})
        elif t == "tool_result":
            text, images = _tool_result_text(b)
            tool_results.append({"role": S.ROLE_TOOL, "tool_call_id": b.get("tool_use_id"), "content": text})
            if images:   # o papel `tool` do OpenAI é só texto: a imagem segue no turno do usuário
                parts.append({"type": "text", "text": f"[Image from tool result {b.get('tool_use_id')}]"})
                parts.extend(images)
    if tool_results:
        return tool_results + ([{"role": S.ROLE_USER, "content": collapse_text_parts(parts)}] if parts else [])
    if tool_calls:
        out: Dict[str, Any] = {"role": S.ROLE_ASSISTANT, "tool_calls": tool_calls}
        if parts:
            out["content"] = collapse_text_parts(parts)
        return out
    if parts:
        return {"role": role, "content": collapse_text_parts(parts)}
    return {"role": role, "content": ""} if not content else None


def _fix_missing_tool_responses(messages: List[dict]) -> None:
    """O OpenAI exige resposta para todo tool_call: insere `[No response received]` nas que faltam."""
    i = 0
    while i < len(messages):
        m = messages[i]
        if m.get("role") == S.ROLE_ASSISTANT and m.get("tool_calls"):
            responded, pos = set(), i + 1
            for j in range(i + 1, len(messages)):
                if messages[j].get("role") == S.ROLE_TOOL and messages[j].get("tool_call_id"):
                    responded.add(messages[j]["tool_call_id"])
                    pos = j + 1
                else:
                    break
            missing = [tc["id"] for tc in m["tool_calls"] if tc["id"] not in responded]
            if missing:
                messages[pos:pos] = [{"role": S.ROLE_TOOL, "tool_call_id": mid, "content": "[No response received]"}
                                     for mid in missing]
                i = pos + len(missing) - 1
        i += 1


def claude_to_openai_request(model: str, body: dict, stream: bool) -> dict:
    result: Dict[str, Any] = {"model": model, "messages": [], "stream": stream}
    if body.get("max_tokens"):
        result["max_tokens"] = adjust_max_tokens(body)
    if body.get("temperature") is not None:
        result["temperature"] = body["temperature"]
    system = body.get("system")
    if system:
        text = ("\n".join(s.get("text", "") for s in system if s.get("text")) if isinstance(system, list) else system)
        if text:
            result["messages"].append({"role": S.ROLE_SYSTEM, "content": text})
    for msg in body.get("messages") or []:
        conv = _convert_claude_message(dict(msg))
        if isinstance(conv, list):
            result["messages"].extend(conv)
        elif conv:
            result["messages"].append(conv)
    _fix_missing_tool_responses(result["messages"])
    if isinstance(body.get("tools"), list):
        # ferramenta nativa do Claude (web_search_*, bash_*...) tem `type` próprio e roda no servidor da Anthropic:
        # virar função faria o modelo chamar algo que ninguém executa
        result["tools"] = [{"type": "function", "function": {
            "name": t.get("name"), "description": str(t.get("description") or ""),
            "parameters": t.get("input_schema") or {"type": "object", "properties": {}}}}
            for t in body["tools"] if t.get("type") in (None, "custom")]
        if not result["tools"]:
            del result["tools"]
    tc = body.get("tool_choice")
    if tc:
        if isinstance(tc, str):
            result["tool_choice"] = tc
        else:
            result["tool_choice"] = {"auto": "auto", "any": "required"}.get(tc.get("type")) or (
                {"type": "function", "function": {"name": tc.get("name")}} if tc.get("type") == "tool" else "auto")
    effort = body.get("reasoning_effort", (body.get("reasoning") or {}).get("effort"))
    if effort is not None:
        result["reasoning_effort"] = effort
    return result


# ───────────────────────────── resposta completa ─────────────────────────────

def claude_to_openai_response(body: dict) -> dict:
    """Resposta NÃO streaming do Claude -> `chat.completion`."""
    text, reasoning, tool_calls = [], [], []
    for b in body.get("content") or []:
        t = b.get("type")
        if t == "text":
            text.append(b.get("text", ""))
        elif t == "thinking":
            reasoning.append(b.get("thinking", ""))
        elif t == "tool_use":
            tool_calls.append({"id": b.get("id"), "type": "function",
                               "function": {"name": b.get("name"), "arguments": json.dumps(b.get("input") or {})}})
    message: Dict[str, Any] = {"role": S.ROLE_ASSISTANT, "content": "".join(text) or None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        message["tool_calls"] = tool_calls
    u = body.get("usage") or {}
    return {"id": f"chatcmpl-{body.get('id', '')}", "object": "chat.completion", "created": int(time.time()),
            "model": body.get("model"),
            "choices": [{"index": 0, "message": message,
                         "finish_reason": to_openai_finish(body.get("stop_reason"), "claude")}],
            "usage": to_openai_usage(u.get("input_tokens", 0), u.get("output_tokens", 0),
                                     u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0))}


# ───────────────────────────── stream: Claude -> OpenAI ─────────────────────────────

def new_claude_stream_state() -> dict:
    return {"message_id": None, "model": None, "tool_call_index": 0, "tool_calls": {}, "usage": None,
            "finish_reason": None, "finish_sent": False, "server_block": -1}


def _chunk(state: dict, delta: dict, finish: Optional[str] = None) -> dict:
    return {"id": f"chatcmpl-{state['message_id']}", "object": "chat.completion.chunk", "created": int(time.time()),
            "model": state["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def claude_stream_to_openai(event: dict, state: dict) -> Optional[List[dict]]:
    """Um evento SSE do Claude -> lista de chunks OpenAI (ou None)."""
    if not event:
        return None
    out: List[dict] = []
    kind = event.get("type")
    if kind == "message_start":
        msg = event.get("message") or {}
        state.update(message_id=msg.get("id") or f"msg_{int(time.time())}", model=msg.get("model"), tool_call_index=0)
        u = msg.get("usage")
        if isinstance(u, dict):
            state["usage"] = {"input": u.get("input_tokens", 0), "output": 0,
                              "cache_read": u.get("cache_read_input_tokens", 0),
                              "cache_creation": u.get("cache_creation_input_tokens", 0)}
        out.append(_chunk(state, {"role": S.ROLE_ASSISTANT}))
    elif kind == "content_block_start":
        block = event.get("content_block") or {}
        if block.get("type") == "server_tool_use":      # ferramenta nativa (web search): o Claude trata por dentro
            state["server_block"] = event.get("index")
        elif block.get("type") == "tool_use":
            idx = state["tool_call_index"]
            state["tool_call_index"] += 1
            tc = {"index": idx, "id": block.get("id"), "type": "function",
                  "function": {"name": block.get("name"), "arguments": ""}}
            state["tool_calls"][event.get("index")] = tc
            out.append(_chunk(state, {"tool_calls": [{**tc, "function": dict(tc["function"])}]}))   # cópia: tc é mutado depois
    elif kind == "content_block_delta":
        if event.get("index") != state["server_block"]:
            d = event.get("delta") or {}
            if d.get("type") == "text_delta" and d.get("text"):
                out.append(_chunk(state, {"content": d["text"]}))
            elif d.get("type") == "thinking_delta" and d.get("thinking"):
                out.append(_chunk(state, {"reasoning_content": d["thinking"]}))
            elif d.get("type") == "input_json_delta" and d.get("partial_json"):
                tc = state["tool_calls"].get(event.get("index"))
                if tc:
                    tc["function"]["arguments"] += d["partial_json"]
                    out.append(_chunk(state, {"tool_calls": [{"index": tc["index"], "id": tc["id"],
                                                              "function": {"arguments": d["partial_json"]}}]}))
    elif kind == "content_block_stop":
        if event.get("index") == state["server_block"]:
            state["server_block"] = -1
    elif kind == "message_delta":
        u, prev = event.get("usage"), state.get("usage") or {}
        if isinstance(u, dict):
            state["usage"] = {"input": u.get("input_tokens", prev.get("input", 0)),
                              "output": u.get("output_tokens", 0),
                              "cache_read": u.get("cache_read_input_tokens", prev.get("cache_read", 0)),
                              "cache_creation": u.get("cache_creation_input_tokens", prev.get("cache_creation", 0))}
        delta = event.get("delta") or {}
        if delta.get("stop_reason"):
            state["finish_reason"] = to_openai_finish(delta["stop_reason"], "claude")
            note = delta["stop_reason"] == "refusal" and (delta.get("stop_details") or {}).get("explanation")
            if note:
                out.append(_chunk(state, {"content": note}))
            final = _chunk(state, {}, state["finish_reason"])
            if state.get("usage"):
                us = state["usage"]
                final["usage"] = to_openai_usage(us["input"], us["output"], us["cache_read"], us["cache_creation"])
            out.append(final)
            state["finish_sent"] = True
    elif kind == "message_stop" and not state["finish_sent"]:
        reason = state["finish_reason"] or (S.FINISH_TOOL_CALLS if state["tool_calls"] else S.FINISH_STOP)
        out.append(_chunk(state, {}, reason))
        state["finish_sent"] = True
    return out or None


# ───────────────────────────── stream: OpenAI -> Claude ─────────────────────────────

def new_openai_stream_state() -> dict:
    return {"started": False, "next_index": 0, "text_index": None, "text_open": False, "think_index": None,
            "think_open": False, "tool_calls": {}, "tool_args": {}, "usage": None, "id": None, "model": None}


def _stop_thinking(state: dict, out: List[dict]) -> None:
    if state["think_open"]:
        out.append({"type": "content_block_stop", "index": state["think_index"]})
        state["think_open"] = False


def _stop_text(state: dict, out: List[dict]) -> None:
    if state["text_open"]:
        out.append({"type": "content_block_stop", "index": state["text_index"]})
        state["text_open"] = False


def openai_stream_to_claude(chunk: dict, state: dict) -> Optional[List[dict]]:
    """Um chunk `chat.completion.chunk` -> lista de eventos SSE no formato do Claude (ou None)."""
    if not chunk or not chunk.get("choices"):
        return None
    out: List[dict] = []
    choice = chunk["choices"][0]
    delta = choice.get("delta") or {}
    u = chunk.get("usage")
    if isinstance(u, dict):
        det = u.get("prompt_tokens_details") or {}
        cache_read, cache_create = det.get("cached_tokens") or 0, det.get("cache_creation_tokens") or 0
        usage = {"input_tokens": (u.get("prompt_tokens") or 0) - cache_read - cache_create,
                 "output_tokens": u.get("completion_tokens") or 0}
        if cache_read:
            usage["cache_read_input_tokens"] = cache_read
        if cache_create:
            usage["cache_creation_input_tokens"] = cache_create
        state["usage"] = usage
    if not state["started"]:
        state.update(started=True, id=(chunk.get("id") or "").replace("chatcmpl-", "") or f"msg_{int(time.time())}",
                     model=chunk.get("model") or "unknown")
        out.append({"type": "message_start", "message": {
            "id": state["id"], "type": "message", "role": S.ROLE_ASSISTANT, "model": state["model"], "content": [],
            "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
    if reasoning:
        _stop_text(state, out)
        if not state["think_open"]:
            state.update(think_index=state["next_index"], think_open=True)
            state["next_index"] += 1
            out.append({"type": "content_block_start", "index": state["think_index"],
                        "content_block": {"type": "thinking", "thinking": ""}})
        out.append({"type": "content_block_delta", "index": state["think_index"],
                    "delta": {"type": "thinking_delta", "thinking": reasoning}})
    if delta.get("content"):
        _stop_thinking(state, out)
        if not state["text_open"]:
            state.update(text_index=state["next_index"], text_open=True)
            state["next_index"] += 1
            out.append({"type": "content_block_start", "index": state["text_index"],
                        "content_block": {"type": "text", "text": ""}})
        out.append({"type": "content_block_delta", "index": state["text_index"],
                    "delta": {"type": "text_delta", "text": delta["content"]}})
    for tc in delta.get("tool_calls") or []:
        idx = tc.get("index", 0)
        if tc.get("id") and idx not in state["tool_calls"]:
            _stop_thinking(state, out)
            _stop_text(state, out)
            block = state["next_index"]
            state["next_index"] += 1
            state["tool_calls"][idx] = {"id": tc["id"], "name": (tc.get("function") or {}).get("name") or "",
                                        "block": block}
            out.append({"type": "content_block_start", "index": block,
                        "content_block": {"type": "tool_use", "id": tc["id"],
                                          "name": state["tool_calls"][idx]["name"], "input": {}}})
        args = (tc.get("function") or {}).get("arguments")
        if args and idx in state["tool_calls"]:
            state["tool_args"][idx] = state["tool_args"].get(idx, "") + args
    if choice.get("finish_reason"):
        _stop_thinking(state, out)
        _stop_text(state, out)
        for idx, info in state["tool_calls"].items():
            if state["tool_args"].get(idx):
                out.append({"type": "content_block_delta", "index": info["block"],
                            "delta": {"type": "input_json_delta", "partial_json": state["tool_args"][idx]}})
            out.append({"type": "content_block_stop", "index": info["block"]})
        out.append({"type": "message_delta", "delta": {"stop_reason": from_openai_finish(choice["finish_reason"], "claude")},
                    "usage": state["usage"] or {"input_tokens": 0, "output_tokens": 0}})
        out.append({"type": "message_stop"})
    return out or None
