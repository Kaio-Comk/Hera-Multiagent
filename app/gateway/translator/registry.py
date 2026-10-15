"""Registro de tradutores por par `origem:destino`. OpenAI é o pivô: o par exato roda direto; os demais passam por
ele (claude -> openai -> gemini). Espelha `translator/index.js`."""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from app.gateway.translator import claude_openai as co
from app.gateway.translator import gemini as gm

FORMATS = ("openai", "claude", "gemini")
PIVOT = "openai"

_REQUEST: Dict[Tuple[str, str], Callable] = {
    ("openai", "claude"): co.openai_to_claude_request,
    ("claude", "openai"): co.claude_to_openai_request,
    ("openai", "gemini"): gm.openai_to_gemini_request,
}
# stream: (formato do upstream, formato do cliente) -> (novo_estado, tradutor de evento)
_STREAM: Dict[Tuple[str, str], Tuple[Callable, Callable]] = {
    ("claude", "openai"): (co.new_claude_stream_state, co.claude_stream_to_openai),
    ("openai", "claude"): (co.new_openai_stream_state, co.openai_stream_to_claude),
    ("gemini", "openai"): (gm.new_gemini_stream_state, gm.gemini_stream_to_openai),
}
_RESPONSE: Dict[Tuple[str, str], Callable] = {
    ("claude", "openai"): co.claude_to_openai_response,
    ("gemini", "openai"): gm.gemini_to_openai_response,
}


def _check(*fmts: str) -> None:
    for f in fmts:
        if f not in FORMATS:
            raise ValueError(f"formato desconhecido: {f!r} (suportados: {', '.join(FORMATS)})")


def translate_request(source: str, target: str, model: str, body: dict, stream: bool) -> dict:
    _check(source, target)
    if source == target:
        return {**body, "model": model, "stream": stream}
    fn = _REQUEST.get((source, target))
    if fn:
        return fn(model, body, stream)
    if source != PIVOT and target != PIVOT and (source, PIVOT) in _REQUEST and (PIVOT, target) in _REQUEST:
        return _REQUEST[(PIVOT, target)](model, _REQUEST[(source, PIVOT)](model, body, stream), stream)
    raise ValueError(f"sem tradutor de requisição {source} -> {target}")


def new_stream_state(upstream: str, client: str) -> dict:
    """Estado de um stream. Via pivô (ex.: gemini -> claude) guarda os dois estados."""
    _check(upstream, client)
    if upstream == client:
        return {}
    if (upstream, client) in _STREAM:
        return _STREAM[(upstream, client)][0]()
    if (upstream, PIVOT) in _STREAM and (PIVOT, client) in _STREAM:
        return {"_hop1": _STREAM[(upstream, PIVOT)][0](), "_hop2": _STREAM[(PIVOT, client)][0]()}
    raise ValueError(f"sem tradutor de stream {upstream} -> {client}")


def translate_stream_chunk(upstream: str, client: str, chunk: dict, state: dict) -> Optional[List[dict]]:
    _check(upstream, client)
    if upstream == client:
        return [chunk]
    if (upstream, client) in _STREAM:
        return _STREAM[(upstream, client)][1](chunk, state)
    if "_hop1" in state:
        out: List[dict] = []
        for mid in _STREAM[(upstream, PIVOT)][1](chunk, state["_hop1"]) or []:
            out.extend(_STREAM[(PIVOT, client)][1](mid, state["_hop2"]) or [])
        return out or None
    raise ValueError(f"sem tradutor de stream {upstream} -> {client}")


def translate_response(upstream: str, client: str, body: dict) -> dict:
    _check(upstream, client)
    if upstream == client:
        return body
    fn = _RESPONSE.get((upstream, client))
    if fn:
        return fn(body)
    raise ValueError(f"sem tradutor de resposta {upstream} -> {client}")
