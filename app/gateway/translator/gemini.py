"""Tradução OpenAI <-> Gemini (API padrão `generateContent` / `streamGenerateContent`).

Port de `open-sse/translator/{request/openai-to-gemini,response/gemini-to-openai,formats/gemini}.js` (9router, MIT).

Diferenças deliberadas em relação ao original:
  * NÃO há os envelopes Cloud Code do Gemini CLI / Antigravity (eles imitam esses clientes para usar o OAuth deles).
  * NÃO há `thoughtSignature` forjada. A assinatura que o Gemini devolve viaja na própria chamada de ferramenta
    (`extra_content.google.thought_signature`, convenção da camada OpenAI-compatível do Google) e volta na requisição.
  * NÃO se desliga o filtro de segurança (`safetySettings` OFF): fica o padrão do Gemini; quem quiser, passa no corpo.
"""
from __future__ import annotations

import copy
import json
import re
import time
from typing import Any, Dict, List, Optional

from app.gateway.translator import schema as S
from app.gateway.translator.concerns import (
    encode_data_uri, safe_parse_json, text_of,
)

GEMINI_FINISH_SAFETY = frozenset({"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT"})

UNSUPPORTED_SCHEMA_KEYS = frozenset({
    "minLength", "maxLength", "exclusiveMinimum", "exclusiveMaximum", "minItems", "maxItems", "format", "multipleOf",
    "uniqueItems", "contains", "unevaluatedProperties", "unevaluatedItems", "contentSchema", "prefixItems",
    "additionalItems", "default", "examples", "$schema", "$defs", "definitions", "const", "$ref", "$comment",
    "deprecated", "readOnly", "writeOnly", "additionalProperties", "propertyNames", "patternProperties",
    "enumDescriptions", "anyOf", "oneOf", "allOf", "not", "dependencies", "dependentSchemas", "dependentRequired",
    "title", "optional", "if", "then", "else", "contentMediaType", "contentEncoding", "errorMessage", "errorMessages",
    "markdownDescription", "doNotSuggest", "suggestSortText", "minProperties", "maxProperties",
})
_PLACEHOLDER = {"reason": {"type": "string", "description": "Brief explanation of why you are calling this tool"}}


# ───────────────────────────── limpeza de JSON Schema (o Gemini rejeita o pedido inteiro) ─────────────────────────────

def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in list(obj.values()):
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _best_schema(items: List[dict]) -> dict:
    def score(s: dict) -> int:
        if s.get("type") == "object" or s.get("properties"):
            return 3
        if s.get("type") == "array" or s.get("items"):
            return 2
        return 1 if s.get("type") and s.get("type") != "null" else 0
    return max(items, key=score)


def clean_json_schema(schema: Any) -> Any:
    """Deixa um JSON Schema aceitável pelo Gemini (copia; não altera o original)."""
    if not isinstance(schema, dict):
        return schema
    s = copy.deepcopy(schema)
    for n in _walk(s):                                       # const -> enum (valores sempre string)
        if "const" in n and "enum" not in n:
            n["enum"] = [n.pop("const")]
        if isinstance(n.get("enum"), list):
            n["enum"] = [str(v) for v in n["enum"]]
            n.setdefault("type", "string")
    for n in _walk(s):                                       # allOf: funde properties/required
        if isinstance(n.get("allOf"), list):
            for item in n.pop("allOf"):
                if isinstance(item, dict):
                    if item.get("properties"):
                        n["properties"] = {**n.get("properties", {}), **item["properties"]}
                    for r in item.get("required") or []:
                        n.setdefault("required", [])
                        if r not in n["required"]:
                            n["required"].append(r)
    for n in _walk(s):                                       # prefixItems (tupla) -> items
        pi = n.pop("prefixItems", None)
        if isinstance(pi, list) and pi and "items" not in n:
            variants = [v for v in pi if isinstance(v, dict) and v.get("type") != "null"]
            if len(variants) == 1:
                n["items"] = variants[0]
            elif variants:
                n["items"] = {"anyOf": variants}
    for key in ("anyOf", "oneOf"):                           # anyOf/oneOf: escolhe a melhor alternativa não nula
        for n in _walk(s):
            alts = n.get(key)
            if isinstance(alts, list) and alts:
                non_null = [a for a in alts if isinstance(a, dict) and a.get("type") != "null"]
                if non_null:
                    del n[key]
                    n.update(_best_schema(non_null))
    for n in _walk(s):                                       # type: ["string","null"] -> "string"
        if isinstance(n.get("type"), list):
            non_null = [t for t in n["type"] if t != "null"]
            n["type"] = non_null[0] if non_null else "string"
        if "properties" in n and "type" not in n:
            n["type"] = "object"
        if n.get("type") == "array" and "items" not in n:
            n["items"] = {"type": "string"}

    # `properties` guarda NOMES de campo: uma propriedade chamada "title"/"format" não pode ser apagada
    def strip_schema(o: Any) -> None:
        if isinstance(o, dict):
            for k in [k for k in o if k in UNSUPPORTED_SCHEMA_KEYS or k.startswith("x-")]:
                del o[k]
            for k, v in o.items():
                if k == "properties" and isinstance(v, dict):
                    for sub in v.values():
                        strip_schema(sub)
                else:
                    strip_schema(v)
        elif isinstance(o, list):
            for v in o:
                strip_schema(v)
    strip_schema(s)
    for n in _walk(s):                                       # required só com campos que existem
        if isinstance(n.get("required"), list) and isinstance(n.get("properties"), dict):
            valid = [r for r in n["required"] if r in n["properties"]]
            if valid:
                n["required"] = valid
            else:
                del n["required"]
    for n in _walk(s):                                       # objeto vazio ganha um campo marcador
        if n is s and not n:
            n.update(type="object", properties=copy.deepcopy(_PLACEHOLDER), required=["reason"])
        elif n.get("type") == "object" and not n.get("properties"):
            n["properties"] = copy.deepcopy(_PLACEHOLDER)
            n["required"] = ["reason"]
    return s


# ───────────────────────────── requisição: OpenAI -> Gemini ─────────────────────────────

def sanitize_function_name(name: Optional[str]) -> str:
    """Gemini: começa com [a-zA-Z_], depois [a-zA-Z0-9_.:-], até 64 caracteres."""
    if not name:
        return "_unknown"
    out = re.sub(r"[^a-zA-Z0-9_.:\-]", "_", name)
    if not re.match(r"[a-zA-Z_]", out):
        out = "_" + out
    return out[:64]


def _inline(url: str) -> Optional[dict]:
    comma = url.find(",")
    if not url.startswith("data:") or comma == -1:
        return None
    return {"inlineData": {"mime_type": url[5:comma].split(";")[0], "data": url[comma + 1:]}}


def content_to_parts(content: Any) -> List[dict]:
    if isinstance(content, str):
        return [{"text": content}]
    parts: List[dict] = []
    for item in content if isinstance(content, list) else []:
        t = item.get("type")
        if t == "text":
            parts.append({"text": item.get("text", "")})
        elif t == "image_url":
            url = (item.get("image_url") or {}).get("url", "")
            if url.startswith("data:"):
                p = _inline(url)
                if p:
                    parts.append(p)
            elif url.startswith(("http://", "https://")):
                parts.append({"fileData": {"fileUri": url, "mimeType": "image/*"}})
        elif t == "input_audio" and (item.get("input_audio") or {}).get("data"):
            fmt = item["input_audio"].get("format") or "wav"
            parts.append({"inlineData": {"mime_type": "audio/mpeg" if fmt == "mp3" else f"audio/{fmt}",
                                         "data": item["input_audio"]["data"]}})
        elif t == "audio_url" and str((item.get("audio_url") or {}).get("url", "")).startswith("data:"):
            p = _inline(item["audio_url"]["url"])
            if p:
                parts.append(p)
        elif t == "file" and str((item.get("file") or {}).get("file_data", "")).startswith("data:"):
            p = _inline(item["file"]["file_data"])
            if p:
                parts.append(p)
    return parts


def _sanitize_response_payload(value: Any) -> Any:
    """`$ref` dentro de functionResponse.response é lido como ponteiro para `parts` e derruba o pedido."""
    if isinstance(value, list):
        return [_sanitize_response_payload(v) for v in value]
    if isinstance(value, dict):
        return {("_ref" if k == "$ref" else k): _sanitize_response_payload(v) for k, v in value.items()}
    return value


def normalize_contents(contents: List[dict]) -> List[dict]:
    """Funde turnos vizinhos do mesmo papel, tira partes vazias e garante turno inicial e final do usuário."""
    out: List[dict] = []
    for c in contents or []:
        if not c.get("role") or not isinstance(c.get("parts"), list):
            continue
        parts = []
        for p in c["parts"]:
            if not p:
                continue
            fr = p.get("functionResponse")
            if isinstance(fr, dict) and isinstance(fr.get("response"), dict):
                p = {**p, "functionResponse": {**fr, "response": _sanitize_response_payload(fr["response"])}}
            parts.append(p)
        if not parts:
            continue
        if out and out[-1]["role"] == c["role"]:
            out[-1]["parts"].extend(parts)
        else:
            out.append({**c, "parts": list(parts)})
    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "parts": [{"text": "..."}]})
    if out and out[-1]["role"] == "model":
        calls = [p["functionCall"] for p in out[-1]["parts"] if p.get("functionCall")]
        if calls:
            out.append({"role": "user", "parts": [{"functionResponse": {
                **({"id": c["id"]} if c.get("id") else {}), "name": c.get("name") or "tool",
                "response": {"result": "Continue."}}} for c in calls]})
        else:
            out.append({"role": "user", "parts": [{"text": "Continue."}]})
    return out


def _unique_ids():
    used: set = set()

    def nxt(i):
        if not i:
            return i
        if i not in used:
            used.add(i)
            return i
        n = 2
        cand = f"{i}-{n}"
        while cand in used:
            n += 1
            cand = f"{i}-{n}"
        used.add(cand)
        return cand
    return nxt


def openai_to_gemini_request(model: str, body: dict, stream: bool) -> dict:
    result: Dict[str, Any] = {"model": model, "contents": [], "generationConfig": {}}
    gc = result["generationConfig"]
    for src, dst in (("temperature", "temperature"), ("top_p", "topP"), ("top_k", "topK"),
                     ("max_tokens", "maxOutputTokens")):
        if body.get(src) is not None:
            gc[dst] = body[src]
    messages = body.get("messages") or []
    id_to_name = {tc["id"]: tc["function"]["name"] for m in messages if m.get("role") == S.ROLE_ASSISTANT
                  for tc in (m.get("tool_calls") or [])
                  if tc.get("type") == "function" and tc.get("id") and (tc.get("function") or {}).get("name")}
    responses: Dict[str, List[Any]] = {}
    for m in messages:                                       # fila por id: o mesmo id pode repetir em turnos distintos
        if m.get("role") == S.ROLE_TOOL and m.get("tool_call_id"):
            responses.setdefault(m["tool_call_id"], []).append(m.get("content"))
    cursor: Dict[str, int] = {}
    uniq = _unique_ids()                                     # o Gemini exige functionCall.id único no histórico todo

    for i, msg in enumerate(messages):
        role, content = msg.get("role"), msg.get("content")
        if role == S.ROLE_SYSTEM and len(messages) > 1:
            result["systemInstruction"] = {"role": "user", "parts": [{"text": content if isinstance(content, str)
                                                                    else text_of(content)}]}
        elif role == S.ROLE_USER or (role == S.ROLE_SYSTEM and len(messages) == 1):
            parts = content_to_parts(content)
            if parts:
                result["contents"].append({"role": "user", "parts": parts})
        elif role == S.ROLE_ASSISTANT:
            parts: List[dict] = []
            if msg.get("reasoning_content"):
                parts.append({"thought": True, "text": msg["reasoning_content"]})
            if content:
                text = content if isinstance(content, str) else text_of(content)
                if text:
                    parts.append({"text": text})
            calls = []
            for tc in msg.get("tool_calls") or []:
                if tc.get("type") != "function":
                    continue
                fn = tc.get("function") or {}
                emit_id = uniq(tc.get("id"))
                part: Dict[str, Any] = {"functionCall": {"id": emit_id, "name": sanitize_function_name(fn.get("name")),
                                                          "args": safe_parse_json(fn.get("arguments") or "{}", None) or {}}}
                sig = ((tc.get("extra_content") or {}).get("google") or {}).get("thought_signature")
                if sig:
                    part["thoughtSignature"] = sig
                parts.append(part)
                calls.append((tc.get("id"), emit_id, fn.get("name")))
            if calls:
                if parts:
                    result["contents"].append({"role": "model", "parts": parts})
                intermediate = i < len(messages) - 1
                has_resp = any(cursor.get(o, 0) < len(responses.get(o, [])) for o, _, _ in calls)
                if has_resp or intermediate:
                    tool_parts = []
                    for orig, emit_id, call_name in calls:
                        queue, cur = responses.get(orig), cursor.get(orig, 0)
                        resp = ""
                        if queue and cur < len(queue):
                            resp = queue[cur]
                            cursor[orig] = cur + 1
                        name = call_name or id_to_name.get(orig) or str(orig)
                        parsed = safe_parse_json(resp, None) if isinstance(resp, str) else resp
                        if parsed is None or not isinstance(parsed, (dict, list)):
                            parsed = {"result": resp if parsed is None else parsed}
                        tool_parts.append({"functionResponse": {"id": emit_id, "name": sanitize_function_name(name),
                                                                "response": {"result": parsed}}})
                    if tool_parts:
                        result["contents"].append({"role": "user", "parts": tool_parts})
            elif parts:
                result["contents"].append({"role": "model", "parts": parts})

    decls = []
    for t in body.get("tools") or []:
        if t.get("name") and t.get("input_schema"):          # já no formato do Claude
            decls.append({"name": sanitize_function_name(t["name"]), "description": t.get("description") or "",
                          "parameters": clean_json_schema(t["input_schema"])})
        elif t.get("type") == "function" and t.get("function"):
            fn = t["function"]
            decls.append({"name": sanitize_function_name(fn.get("name")), "description": fn.get("description") or "",
                          "parameters": clean_json_schema(fn.get("parameters") or {"type": "object", "properties": {}})})
    if decls:
        result["tools"] = [{"functionDeclarations": decls}]
    result["contents"] = normalize_contents(result["contents"])
    if isinstance(body.get("safety_settings"), list):         # opt-in explícito; o padrão é o do Gemini
        result["safetySettings"] = body["safety_settings"]
    return result


# ───────────────────────────── resposta: Gemini -> OpenAI ─────────────────────────────

def gemini_usage_to_openai(meta: Optional[dict]) -> Optional[dict]:
    if not isinstance(meta, dict):
        return None
    n = lambda k: meta.get(k) if isinstance(meta.get(k), int) else 0     # noqa: E731
    prompt, thoughts, total, cached = n("promptTokenCount"), n("thoughtsTokenCount"), n("totalTokenCount"), n("cachedContentTokenCount")
    cand = n("candidatesTokenCount")
    if cand == 0 and total > 0:
        cand = max(total - prompt - thoughts, 0)
    usage = {"prompt_tokens": prompt, "completion_tokens": cand + thoughts, "total_tokens": total}
    if cached:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    if thoughts:
        usage["completion_tokens_details"] = {"reasoning_tokens": thoughts}
    return usage


def _tool_call(fc: dict, index: int, signature: Optional[str]) -> dict:
    name = fc.get("name")
    tc = {"id": fc.get("id") or f"{name}-{int(time.time() * 1000)}-{index}", "index": index, "type": "function",
          "function": {"name": name, "arguments": json.dumps(fc.get("args") or {})}}
    if signature:
        tc["extra_content"] = {"google": {"thought_signature": signature}}
    return tc


def new_gemini_stream_state() -> dict:
    return {"message_id": None, "model": None, "function_index": 0, "tool_count": 0, "pending_sig": None,
            "usage": None, "finish_reason": None}


def _chunk(state: dict, delta: dict, finish: Optional[str] = None) -> dict:
    return {"id": f"chatcmpl-{state['message_id']}", "object": "chat.completion.chunk", "created": int(time.time()),
            "model": state["model"], "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def gemini_stream_to_openai(chunk: dict, state: dict) -> Optional[List[dict]]:
    """Um pedaço de `streamGenerateContent` -> chunks OpenAI."""
    if not chunk:
        return None
    response = chunk.get("response") or chunk
    candidates = response.get("candidates") or []
    if not candidates:
        return None
    candidate, out = candidates[0], []
    if not state["message_id"]:
        state["message_id"] = response.get("responseId") or f"msg_{int(time.time())}"
        state["model"] = response.get("modelVersion") or state["model"] or "gemini"
        out.append(_chunk(state, {"role": S.ROLE_ASSISTANT}))
    for part in (candidate.get("content") or {}).get("parts") or []:
        sig = part.get("thoughtSignature") or part.get("thought_signature")
        if isinstance(sig, str) and sig:
            state["pending_sig"] = sig
        is_thought = part.get("thought") is True
        text = part.get("text")
        has_fc = bool(part.get("functionCall"))
        if sig and not text and not has_fc:                  # assinatura solta: fica pendente para a próxima chamada
            continue
        if text:
            out.append(_chunk(state, {"reasoning_content": text} if is_thought else {"content": text}))
        if has_fc:
            tc = _tool_call(part["functionCall"], state["function_index"], sig or state["pending_sig"])
            state["function_index"] += 1
            state["tool_count"] += 1
            state["pending_sig"] = None
            out.append(_chunk(state, {"tool_calls": [tc]}))
        inline = part.get("inlineData") or part.get("inline_data")
        if isinstance(inline, dict) and inline.get("data"):
            mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
            out.append(_chunk(state, {"images": [{"type": "image_url",
                                                  "image_url": {"url": encode_data_uri(mime, inline["data"])}}]}))
    usage = gemini_usage_to_openai(response.get("usageMetadata") or chunk.get("usageMetadata"))
    if usage:
        state["usage"] = usage
    if candidate.get("finishReason"):
        reason = _gemini_finish(candidate["finishReason"])
        if reason == S.FINISH_STOP and state["tool_count"] > 0:
            reason = S.FINISH_TOOL_CALLS
        final = _chunk(state, {}, reason)
        if state["usage"]:
            final["usage"] = state["usage"]
        state["finish_reason"] = reason
        out.append(final)
    return out or None


def _gemini_finish(reason: str) -> str:
    r = str(reason).upper()
    if r == "MAX_TOKENS":
        return S.FINISH_LENGTH
    if r in GEMINI_FINISH_SAFETY:
        return S.FINISH_CONTENT_FILTER
    return S.FINISH_STOP


def gemini_to_openai_response(body: dict) -> dict:
    """Resposta NÃO streaming do Gemini -> `chat.completion`."""
    response = body.get("response") or body
    cands = response.get("candidates") or []
    text, reasoning, tool_calls = [], [], []
    candidate = cands[0] if cands else {}
    pending: Optional[str] = None
    for part in (candidate.get("content") or {}).get("parts") or []:
        sig = part.get("thoughtSignature") or part.get("thought_signature")
        if isinstance(sig, str) and sig:
            pending = sig
        if part.get("text"):
            (reasoning if part.get("thought") is True else text).append(part["text"])
        if part.get("functionCall"):
            tool_calls.append(_tool_call(part["functionCall"], len(tool_calls), sig or pending))
            pending = None
    message: Dict[str, Any] = {"role": S.ROLE_ASSISTANT, "content": "".join(text) or None}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        message["tool_calls"] = tool_calls
    finish = _gemini_finish(candidate.get("finishReason") or "STOP")
    if finish == S.FINISH_STOP and tool_calls:
        finish = S.FINISH_TOOL_CALLS
    out = {"id": f"chatcmpl-{response.get('responseId') or int(time.time())}", "object": "chat.completion",
           "created": int(time.time()), "model": response.get("modelVersion") or "gemini",
           "choices": [{"index": 0, "message": message, "finish_reason": finish}]}
    usage = gemini_usage_to_openai(response.get("usageMetadata"))
    if usage:
        out["usage"] = usage
    return out
