"""Tradutores de formato do Hera Gateway (OpenAI <-> Claude, ...).

Port em Python do motor de tradução do 9router (MIT, github.com/decolua/9router, `open-sse/translator`).
Princípio herdado: o formato OpenAI é o PIVÔ; o par exato `origem:destino` registrado roda direto.
NÃO portado de propósito: o "cloaking" que se passa pelo Claude Code (prompt de sistema forjado e prefixo de
ferramenta), porque é falsificação de cliente para usar credencial de assinatura.
"""
from app.gateway.translator.registry import (  # noqa: F401
    FORMATS, translate_request, new_stream_state, translate_stream_chunk, translate_response,
)
