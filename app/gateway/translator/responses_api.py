"""Tradução Responses API da OpenAI <-> Chat Completions (lado do CLIENTE: quem fala `/v1/responses`).

Port de `open-sse/translator/{request,response}/openai-responses.js` e `formats/responsesApi.js` (9router, MIT).
Sentido suportado: o cliente fala Responses, o upstream fala Chat Completions (o caso do Hera, que roteia por
`chat/completions`). O sentido inverso (upstream só-Responses, como o Codex) fica de fora.
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

MAX_CALL_ID_LEN = 64
_seq = 0


def _call_id(value: Any) -> str:
    global _seq
    if not isinstance(value, str) or not value:
        _seq += 1
        return f"call_{int(time.time() * 1000)}_{_seq}"
    return value[:MAX_CALL_ID_LEN]


def normalize_input(value: Any) -> Optional[List[dict]]:
    """`input` aceita string ou lista; vazio vira um turno de usuário placeholder (provedores rejeitam messages=[])."""
    if isinstance(value, str):
        return [{"type": "message", "role": "user",
                 "content": [{"type": "input_text", "text": value if value.strip() else "..."}]}]
    if isinstance(value, list):
        return value or [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "..."}]}]
    return None


def _norm_params(params: Any) -> dict:
    if not isinstance(params, dict) or not params:
        return {"type": "object", "properties": {}}
    if params.get("type") == "object" and "properties" not in params:
        return {**params, "properties": {}}
    return params


def _content(items: Any) -> Any:
    if not isinstance(items, list):
        return items
    out = []
    for c in items:
        t = c.get("type") if isinstance(c, dict) else None
        if t in ("input_text", "output_text"):
            out.append({"type": "text", "text": c.get("text", "")})
        elif t == "input_image":
            out.append({"type": "image_url", "image_url": {"url": c.get("image_url") or c.get("file_id") or "",
                                                           "detail": c.get("detail") or "auto"}})
        else:
            out.append(c)
    return out


def _reasoning_text(item: dict) -> str:
    for key in ("summary", "content"):
        if isinstance(item.get(key), list):
            txt = "\n".join(str(s.get("text") or "") for s in item[key] if isinstance(s, dict) and s.get("text"))
            if txt:
                return txt
    return ""


def responses_to_chat_request(model: str, body: dict, stream: bool) -> dict:
    """Corpo de `/v1/responses` -> corpo de `chat/completions`. Devolve também `_custom_tool_names` (uso interno)."""
    items = normalize_input(body.get("input"))
    if items is None:
        raise ValueError("'input' é obrigatório (texto ou lista de itens)")
    result: Dict[str, Any] = {k: v for k, v in body.items()
                              if k not in ("input", "instructions", "include", "prompt_cache_key", "store",
                                           "reasoning", "client_metadata", "max_output_tokens", "previous_response_id")}
    result.update(model=model, stream=stream, messages=[])
    if body.get("instructions"):
        result["messages"].append({"role": "system", "content": body["instructions"]})
    msgs: List[dict] = result["messages"]
    assistant: Optional[dict] = None
    pending_reasoning = ""
    extra_tools: List[dict] = []
    custom: set = set()

    def flush() -> None:
        nonlocal assistant
        if assistant is not None:
            msgs.append(assistant)
            assistant = None

    for item in items:
        t = item.get("type") or ("message" if item.get("role") else None)
        if t == "message":
            flush()
            role = "system" if item.get("role") == "developer" else item.get("role")
            msg = {"role": role, "content": _content(item.get("content"))}
            if role == "assistant" and pending_reasoning:
                msg["reasoning_content"] = pending_reasoning
            pending_reasoning = ""
            msgs.append(msg)
        elif t in ("function_call", "custom_tool_call"):
            if not isinstance(item.get("name"), str) or not item["name"].strip():
                continue                                       # chamada sem nome é recusada pelos provedores
            if assistant is None:
                assistant = {"role": "assistant", "content": None, "tool_calls": []}
                if pending_reasoning:
                    assistant["reasoning_content"], pending_reasoning = pending_reasoning, ""
            if t == "custom_tool_call":
                custom.add(item["name"])
                args = {"input": item.get("input") if isinstance(item.get("input"), str)
                        else json.dumps(item.get("input") or "")}
            else:
                args = item.get("arguments")
            assistant["tool_calls"].append({"id": _call_id(item.get("call_id")), "type": "function", "function": {
                "name": item["name"], "arguments": args if isinstance(args, str) else json.dumps(args or {})}})
        elif t in ("function_call_output", "custom_tool_call_output"):
            flush()
            out = item.get("output")
            msgs.append({"role": "tool", "tool_call_id": _call_id(item.get("call_id")),
                         "content": out if isinstance(out, str) else json.dumps(out)})
        elif t == "additional_tools":
            if isinstance(item.get("tools"), list):
                extra_tools.extend(item["tools"])
        elif t == "reasoning":
            txt = _reasoning_text(item)
            if txt:
                pending_reasoning = f"{pending_reasoning}\n{txt}" if pending_reasoning else txt
    flush()

    tools = []
    for tool in [*(body.get("tools") if isinstance(body.get("tools"), list) else []), *extra_tools]:
        if tool.get("function"):
            tools.append(tool)
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name.strip():     # ferramenta hospedada (sem nome): sem equivalente
            continue
        if tool.get("type") == "custom":
            custom.add(name)
            hint = "\n".join(x for x in ((tool.get("format") or {}).get("syntax"),
                                         (tool.get("format") or {}).get("definition")) if x)
            tools.append({"type": "function", "function": {
                "name": name, "description": "\n\n".join(x for x in (str(tool.get("description") or ""), hint) if x),
                "parameters": {"type": "object", "properties": {"input": {
                    "type": "string", "description": "Raw freeform input for this custom tool"}},
                    "required": ["input"]}}})
        else:
            tools.append({"type": "function", "function": {
                "name": name, "description": str(tool.get("description") or ""),
                "parameters": _norm_params(tool.get("parameters"))}})
    if tools:
        result["tools"] = tools
    else:
        result.pop("tools", None)
    if custom:
        result["_custom_tool_names"] = sorted(custom)
    if body.get("max_output_tokens") is not None and result.get("max_tokens") is None:
        result["max_tokens"] = body["max_output_tokens"]
    effort = (body.get("reasoning") or {}).get("effort")
    if isinstance(effort, str):
        result["reasoning_effort"] = effort
    return result


# ───────────────────────────── resposta completa ─────────────────────────────

def _usage(u: Any) -> Optional[dict]:
    if not isinstance(u, dict):
        return None
    inp = next((u[k] for k in ("input_tokens", "prompt_tokens") if isinstance(u.get(k), int)), None)
    out = next((u[k] for k in ("output_tokens", "completion_tokens") if isinstance(u.get(k), int)), None)
    if inp is None or out is None or inp + out <= 0:          # zerado é placeholder: espera o número real
        return None
    res = {"input_tokens": inp, "output_tokens": out, "total_tokens": inp + out}
    cached = (u.get("prompt_tokens_details") or u.get("input_tokens_details") or {}).get("cached_tokens")
    reasoning = (u.get("completion_tokens_details") or u.get("output_tokens_details") or {}).get("reasoning_tokens")
    if isinstance(cached, int):
        res["input_tokens_details"] = {"cached_tokens": cached}
    if isinstance(reasoning, int):
        res["output_tokens_details"] = {"reasoning_tokens": reasoning}
    return res


def _extract_custom_input(args: str) -> str:
    try:
        parsed = json.loads(args)
        if isinstance(parsed, dict) and isinstance(parsed.get("input"), str):
            return parsed["input"]
    except (ValueError, TypeError):
        pass
    return args


def chat_to_responses_response(resp: dict, custom_names: Optional[List[str]] = None) -> dict:
    """`chat.completion` -> objeto `response` (NÃO streaming)."""
    custom = set(custom_names or [])
    rid = f"resp_{resp.get('id') or int(time.time())}"
    msg = ((resp.get("choices") or [{}])[0].get("message")) or {}
    output: List[dict] = []
    if msg.get("reasoning_content"):
        output.append({"id": f"rs_{rid}_0", "type": "reasoning",
                       "summary": [{"type": "summary_text", "text": msg["reasoning_content"]}]})
    text = msg.get("content")
    if isinstance(text, list):
        text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
    if text:
        output.append({"id": f"msg_{rid}_{len(output)}", "type": "message", "role": "assistant", "content": [
            {"type": "output_text", "annotations": [], "logprobs": [], "text": text}]})
    for tc in msg.get("tool_calls") or []:
        fn, cid = tc.get("function") or {}, tc.get("id") or _call_id(None)
        args = fn.get("arguments") or "{}"
        if fn.get("name") in custom:
            output.append({"id": f"ctc_{cid}", "type": "custom_tool_call", "call_id": cid, "name": fn["name"],
                           "input": _extract_custom_input(args)})
        else:
            output.append({"id": f"fc_{cid}", "type": "function_call", "call_id": cid, "name": fn.get("name"),
                           "arguments": args})
    out = {"id": rid, "object": "response", "created_at": resp.get("created") or int(time.time()),
           "status": "completed", "background": False, "error": None, "model": resp.get("model"), "output": output}
    usage = _usage(resp.get("usage"))
    if usage:
        out["usage"] = usage
    return out


# ───────────────────────────── stream: Chat -> Responses ─────────────────────────────

def new_responses_stream_state(custom_names: Optional[List[str]] = None) -> dict:
    return {"seq": 0, "started": False, "response_id": f"resp_{int(time.time() * 1000)}", "created": int(time.time()),
            "model": None, "custom": set(custom_names or []), "usage": None, "completion_pending": False,
            "completed": False, "completed_items": {}, "reasoning_id": None, "reasoning_index": 0,
            "reasoning_buf": "", "reasoning_done": False, "in_thinking": False,
            "msg_added": {}, "msg_content_added": {}, "msg_done": {}, "msg_text": {},
            "func_ids": {}, "func_names": {}, "func_added": {}, "func_args": {}, "func_done": {}}


def _emitter(state: dict, events: List[dict]):
    def emit(name: str, data: dict) -> None:
        state["seq"] += 1
        data["sequence_number"] = state["seq"]
        events.append({"event": name, "data": data})
    return emit


def _record(state: dict, index: int, item: dict) -> None:
    state["completed_items"][int(index)] = item


def _start_reasoning(state: dict, emit, idx: int) -> None:
    if state["reasoning_id"]:
        return
    state["reasoning_id"], state["reasoning_index"] = f"rs_{state['response_id']}_{idx}", idx
    emit("response.output_item.added", {"type": "response.output_item.added", "output_index": idx,
                                        "item": {"id": state["reasoning_id"], "type": "reasoning", "summary": []}})
    emit("response.reasoning_summary_part.added", {
        "type": "response.reasoning_summary_part.added", "item_id": state["reasoning_id"], "output_index": idx,
        "summary_index": 0, "part": {"type": "summary_text", "text": ""}})


def _reasoning_delta(state: dict, emit, text: str) -> None:
    if text:
        state["reasoning_buf"] += text
        emit("response.reasoning_summary_text.delta", {
            "type": "response.reasoning_summary_text.delta", "item_id": state["reasoning_id"],
            "output_index": state["reasoning_index"], "summary_index": 0, "delta": text})


def _close_reasoning(state: dict, emit) -> None:
    if not state["reasoning_id"] or state["reasoning_done"]:
        return
    state["reasoning_done"] = True
    rid, idx, text = state["reasoning_id"], state["reasoning_index"], state["reasoning_buf"]
    emit("response.reasoning_summary_text.done", {"type": "response.reasoning_summary_text.done", "item_id": rid,
                                                  "output_index": idx, "summary_index": 0, "text": text})
    emit("response.reasoning_summary_part.done", {"type": "response.reasoning_summary_part.done", "item_id": rid,
                                                  "output_index": idx, "summary_index": 0,
                                                  "part": {"type": "summary_text", "text": text}})
    item = {"id": rid, "type": "reasoning", "summary": [{"type": "summary_text", "text": text}]}
    emit("response.output_item.done", {"type": "response.output_item.done", "output_index": idx, "item": item})
    _record(state, idx, item)


def _emit_text(state: dict, emit, idx: int, content: str) -> None:
    msg_id = f"msg_{state['response_id']}_{idx}"
    if not state["msg_added"].get(idx):
        state["msg_added"][idx] = True
        emit("response.output_item.added", {"type": "response.output_item.added", "output_index": idx,
                                            "item": {"id": msg_id, "type": "message", "content": [], "role": "assistant"}})
    if not state["msg_content_added"].get(idx):
        state["msg_content_added"][idx] = True
        emit("response.content_part.added", {
            "type": "response.content_part.added", "item_id": msg_id, "output_index": idx, "content_index": 0,
            "part": {"type": "output_text", "annotations": [], "logprobs": [], "text": ""}})
    emit("response.output_text.delta", {"type": "response.output_text.delta", "item_id": msg_id, "output_index": idx,
                                        "content_index": 0, "delta": content, "logprobs": []})
    state["msg_text"][idx] = state["msg_text"].get(idx, "") + content


def _close_message(state: dict, emit, idx: int) -> None:
    if not state["msg_added"].get(idx) or state["msg_done"].get(idx):
        return
    state["msg_done"][idx] = True
    text, msg_id = state["msg_text"].get(idx, ""), f"msg_{state['response_id']}_{idx}"
    part = {"type": "output_text", "annotations": [], "logprobs": [], "text": text}
    emit("response.output_text.done", {"type": "response.output_text.done", "item_id": msg_id, "output_index": idx,
                                       "content_index": 0, "text": text, "logprobs": []})
    emit("response.content_part.done", {"type": "response.content_part.done", "item_id": msg_id, "output_index": idx,
                                        "content_index": 0, "part": part})
    item = {"id": msg_id, "type": "message", "content": [part], "role": "assistant"}
    emit("response.output_item.done", {"type": "response.output_item.done", "output_index": idx, "item": item})
    _record(state, idx, item)


def _emit_tool_call(state: dict, emit, tc: dict) -> None:
    ti = tc.get("index") or 0
    fn = tc.get("function") or {}
    if fn.get("name"):
        state["func_names"][ti] = fn["name"]
    if tc.get("id"):
        state["func_ids"][ti] = tc["id"]
    call_id, name = state["func_ids"].get(ti), state["func_names"].get(ti)
    if not state["func_added"].get(ti) and call_id and name:       # espera id E nome (alguns provedores os separam)
        state["func_added"][ti] = True
        custom = name in state["custom"]
        emit("response.output_item.added", {"type": "response.output_item.added", "output_index": ti, "item": {
            "id": f"{'ctc' if custom else 'fc'}_{call_id}",
            "type": "custom_tool_call" if custom else "function_call",
            **({"input": ""} if custom else {"arguments": ""}), "call_id": call_id, "name": name}})
    state["func_args"].setdefault(ti, "")
    if fn.get("arguments"):
        if state["func_added"].get(ti) and call_id and name not in state["custom"]:
            emit("response.function_call_arguments.delta", {
                "type": "response.function_call_arguments.delta", "item_id": f"fc_{call_id}", "output_index": ti,
                "delta": fn["arguments"]})
        state["func_args"][ti] += fn["arguments"]                  # custom: emitido uma vez, ao fechar


def _close_tool_call(state: dict, emit, idx: int) -> None:
    call_id = state["func_ids"].get(idx)
    if not call_id or state["func_done"].get(idx):
        return
    args, name = state["func_args"].get(idx) or "{}", state["func_names"].get(idx) or ""
    custom = name in state["custom"]
    if custom:
        inp = _extract_custom_input(args)
        emit("response.custom_tool_call_input.delta", {"type": "response.custom_tool_call_input.delta",
                                                       "item_id": f"ctc_{call_id}", "output_index": idx, "delta": inp})
        emit("response.custom_tool_call_input.done", {"type": "response.custom_tool_call_input.done",
                                                      "item_id": f"ctc_{call_id}", "output_index": idx, "input": inp})
        item = {"id": f"ctc_{call_id}", "type": "custom_tool_call", "input": inp, "call_id": call_id, "name": name}
    else:
        emit("response.function_call_arguments.done", {"type": "response.function_call_arguments.done",
                                                       "item_id": f"fc_{call_id}", "output_index": idx,
                                                       "arguments": args})
        item = {"id": f"fc_{call_id}", "type": "function_call", "arguments": args, "call_id": call_id, "name": name}
    emit("response.output_item.done", {"type": "response.output_item.done", "output_index": idx, "item": item})
    _record(state, idx, item)
    state["func_done"][idx] = True


def _send_completed(state: dict, emit) -> None:
    if state["completed"]:
        return
    state["completed"] = True
    # `output` repete os itens já entregues: clientes que montam o resultado final pelo evento terminal
    # (Copilot CLI, helpers do SDK) tratariam o turno como vazio sem isso.
    resp = {"id": state["response_id"], "object": "response", "created_at": state["created"], "status": "completed",
            "background": False, "error": None,
            "output": [state["completed_items"][i] for i in sorted(state["completed_items"])]}
    if state["usage"]:
        resp["usage"] = state["usage"]
    emit("response.completed", {"type": "response.completed", "response": resp})


def flush_responses_stream(state: dict) -> List[dict]:
    """Fim do upstream: fecha o que ficou aberto e manda `response.completed` (se ainda não foi)."""
    if state["completed"]:
        return []
    events: List[dict] = []
    emit = _emitter(state, events)
    for i in list(state["msg_added"]):
        _close_message(state, emit, i)
    _close_reasoning(state, emit)
    for i in list(state["func_ids"]):
        _close_tool_call(state, emit, i)
    _send_completed(state, emit)
    return events


def chat_stream_to_responses(chunk: Optional[dict], state: dict) -> List[dict]:
    """Um chunk de `chat.completion.chunk` -> eventos `{event, data}` da Responses API (`None` = fim do stream)."""
    if chunk is None:
        return flush_responses_stream(state)
    usage = _usage(chunk.get("usage"))
    if usage:
        state["usage"] = usage
    if not chunk.get("choices"):                                   # trailer de usage (choices vazio)
        return flush_responses_stream(state) if state["completion_pending"] and state["usage"] else []
    events: List[dict] = []
    emit = _emitter(state, events)
    choice = chunk["choices"][0]
    idx, delta = choice.get("index") or 0, choice.get("delta") or {}
    if not state["started"]:
        state["started"] = True
        if chunk.get("id"):
            state["response_id"] = f"resp_{chunk['id']}"
        base = {"id": state["response_id"], "object": "response", "created_at": state["created"],
                "status": "in_progress"}
        emit("response.created", {"type": "response.created",
                                  "response": {**base, "background": False, "error": None, "output": []}})
        emit("response.in_progress", {"type": "response.in_progress", "response": base})
    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        _start_reasoning(state, emit, idx)
        _reasoning_delta(state, emit, reasoning)
    content = delta.get("content")
    if content:
        if "<think>" in content:
            state["in_thinking"] = True
            content = content.replace("<think>", "")
            _start_reasoning(state, emit, idx)
        if "</think>" in content:
            think, _, content = content.partition("</think>")
            if think:
                _reasoning_delta(state, emit, think)
            _close_reasoning(state, emit)
            state["in_thinking"] = False
        if state["in_thinking"] and content:
            _reasoning_delta(state, emit, content)
            return events
        if content:
            _close_reasoning(state, emit)                          # a resposta começou: o raciocínio acabou
            _emit_text(state, emit, idx, content)
    if delta.get("tool_calls"):
        _close_reasoning(state, emit)
        _close_message(state, emit, idx)
        for tc in delta["tool_calls"]:
            _emit_tool_call(state, emit, tc)
    if choice.get("finish_reason"):
        for i in list(state["msg_added"]):
            _close_message(state, emit, i)
        _close_reasoning(state, emit)
        for i in list(state["func_ids"]):
            _close_tool_call(state, emit, i)
        if state["usage"]:
            _send_completed(state, emit)
        else:                    # o usage pode vir num chunk depois: adia a conclusão até ele (ou até o fim do stream)
            state["completion_pending"] = True
    return events
