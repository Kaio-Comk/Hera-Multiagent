"""Módulo de serialização SSE (Server-Sent Events) e streaming resiliente para o Gateway.

Fornece geradores assíncronos no formato OpenAI-compatible, com suporte a pre-flight
de conexão e terminação limpa de erros pós-commit sem costura de respostas.
"""

import json
import time
import uuid
from typing import AsyncIterator, Optional, Dict, Any

from app.gateway.schemas import NormalizedDelta

def format_openai_sse_chunk(
    request_id: str,
    model: str,
    content: Optional[str] = None,
    finish_reason: Optional[str] = None,
    role: Optional[str] = None,
    tool_calls: Optional[list] = None
) -> str:
    """Serializa um fragmento de stream no formato OpenAI SSE."""
    delta: Dict[str, Any] = {}
    if role:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    if tool_calls:
        delta["tool_calls"] = tool_calls

    payload = {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta,
                "finish_reason": finish_reason
            }
        ]
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

def format_sse_error(message: str, code: int = 500) -> str:
    """Serializa uma mensagem de erro em evento SSE para terminação limpa."""
    payload = {
        "error": {
            "message": message,
            "type": "gateway_stream_error",
            "code": code
        }
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

def format_sse_done() -> str:
    """Retorna o evento finalizador padrão da OpenAI."""
    return "data: [DONE]\n\n"

async def sse_stream_generator(
    first_chunk_text: str,
    stream_iterator: AsyncIterator[str],
    request_id: str,
    model: str
) -> AsyncIterator[str]:
    """Gerador assíncrono que consome o upstream e emite eventos SSE normalizados.
    
    Inicia com o chunk de preflight já validado, continua transmitindo tokens e,
    em caso de falha após o início, encerra o stream com erro explícito sem misturar modelos.
    """
    # 1. Emitir o evento com a role
    yield format_openai_sse_chunk(request_id=request_id, model=model, role="assistant")

    # 2. Emitir o primeiro chunk pré-validado
    if first_chunk_text:
        yield format_openai_sse_chunk(request_id=request_id, model=model, content=first_chunk_text)

    # 3. Consumir o restante do stream upstream
    try:
        async for chunk in stream_iterator:
            if chunk:
                yield format_openai_sse_chunk(request_id=request_id, model=model, content=chunk)

        # 4. Finalização normal
        yield format_openai_sse_chunk(request_id=request_id, model=model, finish_reason="stop")
        yield format_sse_done()

    except Exception as e:
        # Regra de Ouro v3: Falha após o início do streaming NÃO faz fallback.
        # Encerra o stream de forma limpa e documentada sem costurar texto de outro modelo.
        yield format_sse_error(f"Interrupção na geração upstream: {str(e)}", code=500)
        yield format_sse_done()
