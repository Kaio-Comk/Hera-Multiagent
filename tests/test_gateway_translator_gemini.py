"""Tradutores OpenAI <-> Gemini do gateway (port do 9router, MIT)."""
import json

from app.gateway.translator import new_stream_state, translate_request, translate_response, translate_stream_chunk
from app.gateway.translator.gemini import clean_json_schema, normalize_contents, sanitize_function_name


def _req(body):
    return translate_request("openai", "gemini", "gemini-x", body, False)


def test_request_basico_system_user_e_config():
    r = _req({"temperature": 0.2, "top_p": 0.9, "max_tokens": 50, "messages": [
        {"role": "system", "content": "S"}, {"role": "user", "content": "oi"}]})
    assert r["systemInstruction"]["parts"] == [{"text": "S"}]
    assert r["contents"] == [{"role": "user", "parts": [{"text": "oi"}]}]
    assert r["generationConfig"] == {"temperature": 0.2, "topP": 0.9, "maxOutputTokens": 50}
    assert "safetySettings" not in r                                    # não desliga o filtro de segurança


def test_so_uma_mensagem_system_vira_turno_de_usuario():
    r = _req({"messages": [{"role": "system", "content": "só isso"}]})
    assert r["contents"][0] == {"role": "user", "parts": [{"text": "só isso"}]}


def test_tool_call_e_resposta_com_ids_repetidos_ficam_unicos_e_pareados():
    msgs = [{"role": "user", "content": "a"}]
    for n, res in (("1", "r1"), ("2", "r2")):                           # o MESMO id em dois turnos
        msgs += [{"role": "assistant", "content": None, "tool_calls": [
                    {"id": "dup", "type": "function", "function": {"name": "f", "arguments": json.dumps({"n": n})}}]},
                 {"role": "tool", "tool_call_id": "dup", "content": res}]
    msgs.append({"role": "user", "content": "fim"})
    contents = _req({"messages": msgs})["contents"]
    calls = [p["functionCall"] for c in contents for p in c["parts"] if "functionCall" in p]
    resps = [p["functionResponse"] for c in contents for p in c["parts"] if "functionResponse" in p]
    assert [c["id"] for c in calls] == ["dup", "dup-2"]
    assert [r["id"] for r in resps] == ["dup", "dup-2"]
    assert [r["response"]["result"] for r in resps] == [{"result": "r1"}, {"result": "r2"}]   # cada turno com o seu


def test_assinatura_de_pensamento_volta_do_extra_content():
    r = _req({"messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": None, "tool_calls": [
        {"id": "t", "type": "function", "function": {"name": "f", "arguments": "{}"},
         "extra_content": {"google": {"thought_signature": "SIG"}}}]}, {"role": "tool", "tool_call_id": "t", "content": "ok"}]})
    call = [p for c in r["contents"] for p in c["parts"] if "functionCall" in p][0]
    assert call["thoughtSignature"] == "SIG"
    assert "thoughtSignature" not in json.dumps(_req({"messages": [{"role": "assistant", "content": "olá"}]}))


def test_nome_de_funcao_e_sanitizado():
    assert sanitize_function_name("mcp/ler arquivo") == "mcp_ler_arquivo"
    assert sanitize_function_name("9abc").startswith("_9")
    assert len(sanitize_function_name("a" * 100)) == 64 and sanitize_function_name(None) == "_unknown"


def test_schema_removido_o_que_o_gemini_rejeita():
    s = clean_json_schema({"type": "object", "additionalProperties": False, "$schema": "x", "properties": {
        "a": {"type": ["string", "null"], "minLength": 1, "format": "uri"},
        "b": {"anyOf": [{"type": "null"}, {"type": "object", "properties": {"z": {"type": "integer"}}}]},
        "c": {"const": 5}, "d": {"type": "array"}, "title": {"type": "string", "title": "Título"}},
        "required": ["a", "fantasma"], "x-custom": 1})
    p = s["properties"]
    assert "additionalProperties" not in s and "$schema" not in s and "x-custom" not in s
    assert p["a"] == {"type": "string"}
    assert p["b"]["type"] == "object" and "z" in p["b"]["properties"]
    assert p["c"] == {"enum": ["5"], "type": "string"}
    assert p["d"]["items"] == {"type": "string"}
    assert "title" in p and "title" not in p["title"]                   # propriedade chamada "title" é preservada
    assert s["required"] == ["a"]


def test_schema_vazio_ganha_campo_marcador_e_original_nao_muda():
    original = {"type": "object", "properties": {}}
    out = clean_json_schema(original)
    assert out["required"] == ["reason"] and original == {"type": "object", "properties": {}}


def test_normalize_contents_funde_e_garante_turnos():
    out = normalize_contents([{"role": "model", "parts": [{"text": "a"}]}, {"role": "model", "parts": [{"text": "b"}]}])
    assert out[0] == {"role": "user", "parts": [{"text": "..."}]}
    assert out[1]["parts"] == [{"text": "a"}, {"text": "b"}]
    assert out[-1] == {"role": "user", "parts": [{"text": "Continue."}]}
    fim = normalize_contents([{"role": "user", "parts": [{"text": "x"}]},
                              {"role": "model", "parts": [{"functionCall": {"id": "i", "name": "f", "args": {}}}]}])
    assert fim[-1]["parts"][0]["functionResponse"]["id"] == "i"


def test_function_response_com_ref_e_renomeado():
    out = normalize_contents([{"role": "user", "parts": [{"functionResponse": {"name": "f", "response": {"$ref": "#/x"}}}]}])
    assert out[0]["parts"][0]["functionResponse"]["response"] == {"_ref": "#/x"}


def test_imagens_audio_e_arquivo():
    r = _req({"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
        {"type": "image_url", "image_url": {"url": "https://x.test/i.jpg"}},
        {"type": "input_audio", "input_audio": {"data": "QQ", "format": "mp3"}},
        {"type": "file", "file": {"file_data": "data:application/pdf;base64,PP"}}]}]})
    parts = r["contents"][0]["parts"]
    assert parts[0] == {"inlineData": {"mime_type": "image/png", "data": "AA"}}
    assert parts[1] == {"fileData": {"fileUri": "https://x.test/i.jpg", "mimeType": "image/*"}}
    assert parts[2]["inlineData"]["mime_type"] == "audio/mpeg"
    assert parts[3]["inlineData"]["mime_type"] == "application/pdf"


# ── resposta ──

def test_resposta_completa_com_ferramenta_pensamento_e_uso():
    r = translate_response("gemini", "openai", {"responseId": "r1", "modelVersion": "g", "candidates": [{
        "finishReason": "STOP", "content": {"parts": [
            {"thought": True, "text": "hm"}, {"text": "ok"},
            {"functionCall": {"id": "c1", "name": "f", "args": {"a": 1}}, "thoughtSignature": "S"}]}}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 4, "thoughtsTokenCount": 2,
                          "totalTokenCount": 16, "cachedContentTokenCount": 3}})
    ch = r["choices"][0]
    assert ch["finish_reason"] == "tool_calls"                           # STOP com ferramenta vira tool_calls
    assert ch["message"]["reasoning_content"] == "hm" and ch["message"]["content"] == "ok"
    assert ch["message"]["tool_calls"][0]["extra_content"] == {"google": {"thought_signature": "S"}}
    assert r["usage"] == {"prompt_tokens": 10, "completion_tokens": 6, "total_tokens": 16,
                          "prompt_tokens_details": {"cached_tokens": 3}, "completion_tokens_details": {"reasoning_tokens": 2}}


def test_finish_reasons_do_gemini():
    for gem, oai in (("MAX_TOKENS", "length"), ("SAFETY", "content_filter"), ("RECITATION", "content_filter"),
                     ("STOP", "stop"), ("OUTRO", "stop")):
        r = translate_response("gemini", "openai", {"candidates": [{"finishReason": gem, "content": {"parts": [{"text": "x"}]}}]})
        assert r["choices"][0]["finish_reason"] == oai


def test_stream_gemini_para_openai_e_assinatura_solta():
    st, out = new_stream_state("gemini", "openai"), []
    for ev in [
        {"responseId": "s1", "candidates": [{"content": {"parts": [{"text": "olá"}]}}]},
        {"candidates": [{"content": {"parts": [{"thoughtSignature": "SIG", "text": ""}]}}]},          # solta
        {"candidates": [{"content": {"parts": [{"functionCall": {"name": "f", "args": {"k": 1}}}]}}]},
        {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}],
         "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 2, "totalTokenCount": 7}}]:
        out.extend(translate_stream_chunk("gemini", "openai", ev, st) or [])
    deltas = [c["choices"][0]["delta"] for c in out]
    assert deltas[0] == {"role": "assistant"} and deltas[1] == {"content": "olá"}
    tc = deltas[2]["tool_calls"][0]
    assert tc["function"] == {"name": "f", "arguments": '{"k": 1}'}
    assert tc["extra_content"] == {"google": {"thought_signature": "SIG"}}          # assinatura grudou na chamada
    final = out[-1]
    assert final["choices"][0]["finish_reason"] == "tool_calls"
    assert final["usage"] == {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}


def test_via_pivo_claude_para_gemini_e_gemini_para_claude():
    body = {"max_tokens": 10, "system": "S", "messages": [{"role": "user", "content": "oi"}]}
    g = translate_request("claude", "gemini", "g", body, False)
    assert g["systemInstruction"]["parts"] == [{"text": "S"}] and g["contents"][0]["parts"] == [{"text": "oi"}]
    st, out = new_stream_state("gemini", "claude"), []
    for ev in [{"responseId": "s", "candidates": [{"content": {"parts": [{"text": "oi"}]}}]},
               {"candidates": [{"finishReason": "STOP", "content": {"parts": []}}]}]:
        out.extend(translate_stream_chunk("gemini", "claude", ev, st) or [])
    types = [e["type"] for e in out]
    assert types[0] == "message_start" and types[-1] == "message_stop"
    assert any(e.get("delta", {}).get("text") == "oi" for e in out)
