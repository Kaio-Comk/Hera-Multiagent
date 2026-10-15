"""Validação e DLP das rotas de modalidade (`/v1/embeddings`, `/v1/images/generations`, `/v1/audio/*`)."""
from __future__ import annotations

from typing import Any, Dict, Tuple

MAX_AUDIO_BYTES = 25 * 1024 * 1024          # mesmo teto da OpenAI para /audio/transcriptions
MAX_EMBED_ITEMS = 2048
MAX_SPEECH_CHARS = 4096
MAX_TEXT_CHARS = 1_000_000


def error_body(status: int, message: str) -> dict:
    kind = {400: "invalid_request_error", 401: "invalid_api_key", 403: "permission_denied", 404: "not_found",
            413: "request_too_large", 429: "rate_limit_exceeded"}.get(status, "server_error" if status >= 500
                                                                     else "invalid_request_error")
    return {"error": {"message": str(message), "type": kind, "param": None, "code": kind}}


def _need_model(body: Dict[str, Any]) -> str:
    model = body.get("model")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("'model' é obrigatório")
    return model


def prepare(kind: str, body: Any) -> Tuple[str, str]:
    """Valida o corpo JSON e devolve `(model, texto_para_dlp)`. Levanta ValueError se for inválido."""
    if not isinstance(body, dict):
        raise ValueError("o corpo precisa ser um objeto JSON")
    model = _need_model(body)
    if kind == "embeddings":
        value = body.get("input")
        items = [value] if isinstance(value, str) else value
        if not isinstance(items, list) or not items:
            raise ValueError("'input' é obrigatório (texto ou lista não vazia)")
        if len(items) > MAX_EMBED_ITEMS:
            raise ValueError(f"'input' aceita no máximo {MAX_EMBED_ITEMS} itens")
        if not all(isinstance(i, (str, int, list)) for i in items):
            raise ValueError("'input' só aceita textos ou listas de tokens")
        text = "\n".join(i for i in items if isinstance(i, str))
    elif kind == "images":
        text = body.get("prompt")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("'prompt' é obrigatório")
    elif kind == "video":
        text = body.get("prompt")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("'prompt' é obrigatório")
    elif kind == "speech":
        text = body.get("input")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("'input' é obrigatório")
        if len(text) > MAX_SPEECH_CHARS:
            raise ValueError(f"'input' aceita no máximo {MAX_SPEECH_CHARS} caracteres")
        if not isinstance(body.get("voice"), str) or not body["voice"].strip():
            raise ValueError("'voice' é obrigatório")
    else:
        raise ValueError(f"modalidade desconhecida: {kind}")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError("texto grande demais")
    return model, text


MAX_SEARCH_BODY = 64 * 1024


def prepare_search(body: Any, max_results: int, max_query: int) -> Tuple[str, int, str]:
    """Valida o corpo de `/v1/search` e devolve `(consulta, quantidade, provedor_ou_vazio)`."""
    if not isinstance(body, dict):
        raise ValueError("o corpo precisa ser um objeto JSON")
    query = body.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("'query' é obrigatório")
    if len(query) > max_query:
        raise ValueError(f"'query' aceita no máximo {max_query} caracteres")
    count = body.get("max_results", 5)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= max_results:
        raise ValueError(f"'max_results' deve ser um inteiro entre 1 e {max_results}")
    provider = body.get("provider") or ""
    if not isinstance(provider, str) or (provider and not provider.replace("-", "").replace("_", "").isalnum()):
        raise ValueError("'provider' inválido")
    return query.strip(), count, provider



VIDEO_ID_SEP = "~"
_VIDEO_ID_CHARS = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-.")


def tag_video_id(provider: str, video_id: str) -> str:
    """O id devolvido ao cliente leva o provedor (`provedor~id`): status e arquivo voltam a quem criou o vídeo."""
    return f"{provider}{VIDEO_ID_SEP}{video_id}"


def split_video_id(tagged: str) -> Tuple[str, str]:
    provider, sep, raw = str(tagged).partition(VIDEO_ID_SEP)
    if (not sep or not provider or not raw or len(raw) > 128
            or not set(raw) <= _VIDEO_ID_CHARS or not set(provider) <= _VIDEO_ID_CHARS):
        raise ValueError("id de vídeo inválido")
    return provider, raw
