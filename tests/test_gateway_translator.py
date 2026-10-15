"""Tradutores OpenAI <-> Claude do gateway (port do 9router, MIT)."""
import json

import pytest

from app.gateway.translator import (
    new_stream_state, translate_request, translate_response, translate_stream_chunk,
)
from app.gateway.translator.concerns import adjust_max_tokens, to_openai_finish


def _o2c(body, stream=False):
    return translate_request("openai", "claude", "claude-x", body, stream)


def _c2o(body, stream=False):
    return translate_request("claude", "openai", "gpt-x", body, stream)


# ── OpenAI -> Claude (requisição) ──

def test_system_vira_campo_system_e_nao_mensagem():
    r = _o2c({"messages": [{"role": "system", "content": "seja breve"}, {"role": "user", "content": "oi"}]})
    assert r["system"] == [{"type": "text", "text": "seja breve"}]
    assert r["messages"] == [{"role": "user", "content": [{"type": "text", "text": "oi"}]}]
    assert "You are Claude Code" not in json.dumps(r)             # sem cloaking do Claude Code


def test_tool_calls_e_tool_result_ficam_em_mensagens_separadas():
    r = _o2c({"messages": [
        {"role": "user", "content": "clima?"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": "clima", "arguments": '{"cidade":"SP"}'}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "sol"},
        {"role": "user", "content": "obrigado"}]})
    roles = [m["role"] for m in r["messages"]]
    assert roles == ["user", "assistant", "user", "user"]
    assert r["messages"][1]["content"][0] == {"type": "tool_use", "id": "t1", "name": "clima", "input": {"cidade": "SP"}}
    assert r["messages"][2]["content"][0]["type"] == "tool_result"


def test_tool_choice_openai_nao_vaza_type_function():
    assert _o2c({"messages": [], "tool_choice": {"type": "function", "function": {"name": "f"}}})["tool_choice"] \
        == {"type": "tool", "name": "f"}
    assert _o2c({"messages": [], "tool_choice": "required"})["tool_choice"] == {"type": "any"}
    assert _o2c({"messages": [], "tool_choice": {"type": "qualquer"}})["tool_choice"] == {"type": "auto"}


def test_ferramenta_sem_type_function_tambem_converte():
    r = _o2c({"messages": [], "tools": [{"function": {"name": "echo", "parameters": {"type": "object"}}}]})
    assert r["tools"][0]["name"] == "echo" and r["tools"][0]["input_schema"] == {"type": "object"}


def test_imagem_data_uri_e_url_e_pdf():
    png = "data:image/png;base64,AAAA"
    r = _o2c({"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": png}},
        {"type": "image_url", "image_url": {"url": "https://x.test/a.jpg"}},
        {"type": "file", "file": {"file_data": "data:application/pdf;base64,BBBB"}},
        {"type": "file", "file": {"file_data": "data:text/plain;base64,CCCC"}}]}]})
    blocks = r["messages"][0]["content"]
    assert [b["type"] for b in blocks] == ["image", "image", "document"]      # só PDF vira document
    assert blocks[0]["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}
    assert blocks[1]["source"] == {"type": "url", "url": "https://x.test/a.jpg"}


def test_json_schema_vira_instrucao_no_system():
    r = _o2c({"messages": [{"role": "user", "content": "x"}],
              "response_format": {"type": "json_schema", "json_schema": {"schema": {"type": "object"}}}})
    assert "valid JSON that strictly follows this JSON schema" in r["system"][0]["text"]


def test_max_tokens_regras():
    assert adjust_max_tokens({"max_tokens": 100}) == 100
    assert adjust_max_tokens({"max_tokens": 100, "tools": [{}]}) == 32000          # ferramentas elevam o mínimo
    assert adjust_max_tokens({"max_tokens": 2000, "thinking": {"budget_tokens": 5000}}) == 6024
    assert adjust_max_tokens({"max_tokens": 999999}) == 64000                      # teto


# ── Claude -> OpenAI (requisição) ──

def test_claude_para_openai_system_tool_use_e_tool_result():
    r = _c2o({"max_tokens": 50, "system": [{"type": "text", "text": "S"}], "messages": [
        {"role": "user", "content": "oi"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "f", "input": {"k": 1}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "ok"}]}],
        "tools": [{"name": "f", "description": "d", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "any"}})
    assert r["messages"][0] == {"role": "system", "content": "S"}
    assert r["messages"][2]["tool_calls"][0]["function"] == {"name": "f", "arguments": '{"k": 1}'}
    assert r["messages"][3] == {"role": "tool", "tool_call_id": "a", "content": "ok"}
    assert r["tools"][0]["function"]["parameters"] == {"type": "object"}
    assert r["tool_choice"] == "required"


def test_tool_call_sem_resposta_ganha_resposta_vazia():
    r = _c2o({"messages": [
        {"role": "assistant", "content": [{"type": "tool_use", "id": "x", "name": "f", "input": {}}]},
        {"role": "user", "content": "continua"}]})
    assert r["messages"][1] == {"role": "tool", "tool_call_id": "x", "content": "[No response received]"}


def test_imagem_dentro_de_tool_result_segue_no_turno_do_usuario():
    r = _c2o({"messages": [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "z", "content": [
        {"type": "text", "text": "print"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QQ=="}}]}]}]})
    assert r["messages"][0] == {"role": "tool", "tool_call_id": "z", "content": "print"}
    assert r["messages"][1]["content"][1]["image_url"]["url"] == "data:image/png;base64,QQ=="


def test_system_no_meio_da_conversa_vira_instructions():
    r = _c2o({"messages": [{"role": "user", "content": "a"}, {"role": "system", "content": "regra"}]})
    assert r["messages"][1] == {"role": "user", "content": "<instructions>\nregra\n</instructions>"}


# ── resposta completa ──

def test_resposta_completa_claude_para_openai():
    r = translate_response("claude", "openai", {
        "id": "msg_1", "model": "claude-x", "stop_reason": "tool_use",
        "content": [{"type": "thinking", "thinking": "hm"}, {"type": "text", "text": "vou chamar"},
                    {"type": "tool_use", "id": "t", "name": "f", "input": {"a": 1}}],
        "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 4}})
    ch = r["choices"][0]
    assert ch["finish_reason"] == "tool_calls" and ch["message"]["reasoning_content"] == "hm"
    assert ch["message"]["tool_calls"][0]["function"]["arguments"] == '{"a": 1}'
    assert r["usage"] == {"prompt_tokens": 14, "completion_tokens": 5, "total_tokens": 19,
                          "prompt_tokens_details": {"cached_tokens": 4}}


def test_recusa_vira_content_filter():
    assert to_openai_finish("refusal", "claude") == "content_filter"
    assert to_openai_finish("stop_sequence", "claude") == "stop"
    assert to_openai_finish("algo_novo", "claude") == "stop"


# ── stream ──

def _run(upstream, client, events):
    st, out = new_stream_state(upstream, client), []
    for ev in events:
        out.extend(translate_stream_chunk(upstream, client, ev, st) or [])
    return out


def test_stream_claude_para_openai_texto_ferramenta_e_usage():
    chunks = _run("claude", "openai", [
        {"type": "message_start", "message": {"id": "m1", "model": "c", "usage": {"input_tokens": 7}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "olá"}},
        {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "t1", "name": "f"}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"a"'}},
        {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": ":1}"}},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"}])
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant"}
    assert chunks[1]["choices"][0]["delta"] == {"content": "olá"}
    assert "".join(c["choices"][0]["delta"]["tool_calls"][0]["function"].get("arguments", "")
                   for c in chunks if c["choices"][0]["delta"].get("tool_calls")) == '{"a":1}'
    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "tool_calls"
    assert final["usage"] == {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    assert sum(1 for c in chunks if c["choices"][0]["finish_reason"]) == 1        # finish só uma vez


def test_stream_ignora_server_tool_use():
    chunks = _run("claude", "openai", [
        {"type": "message_start", "message": {"id": "m"}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "server_tool_use"}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
        {"type": "content_block_stop", "index": 0}])
    assert len(chunks) == 1                                                      # só o role inicial


def test_stream_recusa_mostra_explicacao():
    chunks = _run("claude", "openai", [
        {"type": "message_start", "message": {"id": "m"}},
        {"type": "message_delta", "delta": {"stop_reason": "refusal", "stop_details": {"explanation": "bloqueado"}},
         "usage": {"output_tokens": 0}}])
    assert chunks[1]["choices"][0]["delta"] == {"content": "bloqueado"}
    assert chunks[-1]["choices"][0]["finish_reason"] == "content_filter"


def _oai(delta, finish=None, **extra):
    return {"id": "chatcmpl-abc12345", "model": "m", "choices": [{"delta": delta, "finish_reason": finish}], **extra}


def test_stream_openai_para_claude_blocos_na_ordem_certa():
    ev = _run("openai", "claude", [
        _oai({"reasoning_content": "pensando"}),
        _oai({"content": "resp"}),
        _oai({"tool_calls": [{"index": 0, "id": "t", "function": {"name": "f", "arguments": '{"a":'}}]}),
        _oai({"tool_calls": [{"index": 0, "function": {"arguments": "1}"}}]}),
        _oai({}, "tool_calls", usage={"prompt_tokens": 9, "completion_tokens": 2})])
    kinds = [(e["type"], e.get("content_block", {}).get("type") or e.get("delta", {}).get("type")) for e in ev]
    assert kinds[0][0] == "message_start"
    assert [k for k in kinds if k[0] == "content_block_start"] == [
        ("content_block_start", "thinking"), ("content_block_start", "text"), ("content_block_start", "tool_use")]
    delta_json = [e for e in ev if e.get("delta", {}).get("type") == "input_json_delta"]
    assert len(delta_json) == 1 and delta_json[0]["delta"]["partial_json"] == '{"a":1}'   # args em bufer único
    md = next(e for e in ev if e["type"] == "message_delta")
    assert md["delta"]["stop_reason"] == "tool_use" and md["usage"] == {"input_tokens": 9, "output_tokens": 2}
    assert ev[-1]["type"] == "message_stop"
    starts = [e["index"] for e in ev if e["type"] == "content_block_start"]
    stops = [e["index"] for e in ev if e["type"] == "content_block_stop"]
    assert starts == [0, 1, 2] and sorted(stops) == [0, 1, 2]                       # todo bloco aberto é fechado


def test_mesmo_formato_passa_direto_e_formato_desconhecido_falha():
    body = {"messages": [{"role": "user", "content": "x"}]}
    assert translate_request("openai", "openai", "m", body, True)["stream"] is True
    with pytest.raises(ValueError):
        translate_request("openai", "xyz", "m", body, False)
