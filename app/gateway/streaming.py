"""Streaming de verdade do gateway (plano mestre v3, seções 2.2 e 2.3).

Antes, `route_and_stream` executava a resposta COMPLETA e depois a fatiava em eventos SSE. Agora cada
adaptador abre a conexão com o provedor e entrega deltas normalizados à medida que chegam:

  * PRÉ-VOO: o adaptador lê até o primeiro dado útil ANTES de devolver o controle. Falha aqui ainda
    pode fazer fallback para outro provedor e vira um erro HTTP de verdade;
  * DEPOIS do primeiro byte enviado ao cliente NUNCA se troca de provedor (misturaria modelos): uma
    falha vira evento de erro SSE + [DONE];
  * se o cliente desconecta, o gerador é cancelado e a conexão com o provedor é FECHADA (o provedor
    para de gerar);
  * keep-alive (comentário SSE) enquanto o provedor está quieto e erro 504 se ficar mudo demais.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable, Deque, Dict, List, Optional

import httpx

from app.gateway.schemas import NormalizedDelta

KEEPALIVE_S = 15.0
IDLE_TIMEOUT_S = 60.0
_FINISH = {"stop", "length", "tool_calls", "content_filter"}


class ProviderStreamError(RuntimeError):
    """Erro no corpo do stream do provedor (evento de erro, JSON inválido...)."""


def norm_finish(reason: Optional[str]) -> Optional[str]:
    if reason is None:
        return None
    return reason if reason in _FINISH else "stop"


# ── parsers por protocolo ──────────────────────────────────────────────────────
async def ndjson_deltas(lines: AsyncIterator[str], provider: str, model: str, request_id: str
                        ) -> AsyncIterator[NormalizedDelta]:
    """Ollama `/api/chat` com stream=true: uma linha JSON por fragmento."""
    async for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError as exc:
            raise ProviderStreamError(f"linha inválida do Ollama: {line[:80]!r}") from exc
        if data.get("error"):
            raise ProviderStreamError(str(data["error"])[:200])
        text = (data.get("message") or {}).get("content") or ""
        if text:
            yield NormalizedDelta(request_id=request_id, provider=provider, model=model, content=text)
        if data.get("done"):
            yield NormalizedDelta(request_id=request_id, provider=provider, model=model,
                                  finish_reason=norm_finish(data.get("done_reason") or "stop"),
                                  usage_input_tokens=data.get("prompt_eval_count"),
                                  usage_output_tokens=data.get("eval_count"))
            return


async def openai_sse_deltas(lines: AsyncIterator[str], provider: str, model: str, request_id: str
                            ) -> AsyncIterator[NormalizedDelta]:
    """Provedores compatíveis com OpenAI: `data: {...}` por evento, `data: [DONE]` no fim. Fragmentos
    de tool_calls chegam em partes: NÃO se assume que cada um traz um JSON completo."""
    async for line in lines:
        line = line.strip()
        if not line or line.startswith(":") or not line.startswith("data:"):
            continue                                       # comentários/keep-alive/linhas estranhas
        payload = line[5:].strip()
        if payload == "[DONE]":
            return
        try:
            data = json.loads(payload)
        except ValueError as exc:
            raise ProviderStreamError(f"evento SSE inválido: {payload[:80]!r}") from exc
        if data.get("error"):
            err = data["error"]
            raise ProviderStreamError(str(err.get("message", err) if isinstance(err, dict) else err)[:200])
        usage = data.get("usage") or {}
        cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", usage.get("prompt_cache_hit_tokens"))
        for choice in data.get("choices") or []:
            delta = choice.get("delta") or {}
            text = delta.get("content") or ""
            tool_calls = delta.get("tool_calls") or []
            finish = choice.get("finish_reason")
            if text:
                yield NormalizedDelta(request_id=request_id, provider=provider, model=model, content=text)
            for tc in tool_calls:
                fn = tc.get("function") or {}
                yield NormalizedDelta(request_id=request_id, provider=provider, model=model,
                                      tool_call_index=tc.get("index", 0), tool_call_id=tc.get("id"),
                                      tool_name_fragment=fn.get("name"),
                                      tool_arguments_fragment=fn.get("arguments"))
            if finish:
                yield NormalizedDelta(request_id=request_id, provider=provider, model=model,
                                      finish_reason=norm_finish(finish),
                                      usage_input_tokens=usage.get("prompt_tokens"),
                                      usage_output_tokens=usage.get("completion_tokens"),
                                      usage_cached_tokens=cached)
        if not (data.get("choices")) and usage:            # evento final só com usage (include_usage)
            yield NormalizedDelta(request_id=request_id, provider=provider, model=model,
                                  usage_input_tokens=usage.get("prompt_tokens"),
                                  usage_output_tokens=usage.get("completion_tokens"),
                                  usage_cached_tokens=cached)


async def simulated_deltas(text: str, provider: str, model: str, request_id: str, finish: str = "stop"
                           ) -> AsyncIterator[NormalizedDelta]:
    """Fallback para adaptadores sem streaming nativo: fatia a resposta já pronta (native=False)."""
    for i in range(0, len(text), 30):
        yield NormalizedDelta(request_id=request_id, provider=provider, model=model, content=text[i:i + 30])
    yield NormalizedDelta(request_id=request_id, provider=provider, model=model, finish_reason=norm_finish(finish))


# ── handle de pré-voo ──────────────────────────────────────────────────────────
@dataclass
class StreamHandle:
    provider: str
    model: str
    request_id: str
    first: Optional[NormalizedDelta]
    rest: AsyncIterator[NormalizedDelta]
    aclose: Callable[[], Awaitable[None]]
    native: bool = True


async def preflight(provider: str, model: str, request_id: str, deltas: AsyncIterator[NormalizedDelta],
                    aclose: Callable[[], Awaitable[None]], native: bool = True) -> StreamHandle:
    """Lê o primeiro delta útil. Erro aqui (rede, JSON ruim, evento de erro) é pré-resposta: o chamador
    ainda pode fazer fallback. Sempre fecha a conexão se falhar."""
    from app.gateway.providers import GatewayProviderError
    it = deltas.__aiter__()
    try:
        first = await it.__anext__()
    except StopAsyncIteration:
        first = None
    except httpx.TimeoutException as exc:
        await aclose()
        raise GatewayProviderError(f"Timeout aguardando o primeiro dado de {provider}: {type(exc).__name__}",
                                   status_code=504, retryable=True)
    except (httpx.HTTPError, ProviderStreamError) as exc:
        await aclose()
        raise GatewayProviderError(f"Falha no início do stream de {provider}: {type(exc).__name__}: {exc}"[:240],
                                   status_code=502, retryable=True)
    except BaseException:
        await aclose()
        raise
    return StreamHandle(provider, model, request_id, first, it, aclose, native)


# ── consumo com keep-alive ─────────────────────────────────────────────────────
async def next_or_keepalive(pending: "asyncio.Future", timeout: float):
    """Espera o próximo delta sem CANCELAR a leitura (cancelar um `__anext__` mata o gerador): devolve
    (True, resultado) quando chega ou (False, None) se estourou `timeout`."""
    done, _ = await asyncio.wait({pending}, timeout=timeout)
    if not done:
        return False, None
    return True, pending.result()


# ── telemetria ─────────────────────────────────────────────────────────────────
@dataclass
class StreamRecord:
    request_id: str
    provider: str = ""
    model: str = ""
    native: bool = True
    attempts: int = 1                      # provedores tentados até abrir o stream (fallbacks = attempts-1)
    started: float = field(default_factory=time.monotonic)
    ttfb_ms: Optional[int] = None
    total_ms: Optional[int] = None
    chunks: int = 0
    chars: int = 0
    finish_reason: Optional[str] = None
    outcome: str = "started"               # completed | error | cancelled
    error: Optional[str] = None
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> Dict:
        return {k: getattr(self, k) for k in (
            "ts", "request_id", "provider", "model", "native", "attempts", "ttfb_ms", "total_ms", "chunks",
            "chars", "finish_reason", "outcome", "error", "prompt_tokens", "completion_tokens", "cached_tokens")}


class StreamTelemetry:
    """Buffer circular em memória (últimos N streams) + agregados simples. Nunca guarda conteúdo."""

    def __init__(self, maxlen: int = 200):
        self._records: Deque[StreamRecord] = deque(maxlen=maxlen)

    def add(self, rec: StreamRecord) -> None:
        self._records.append(rec)

    def recent(self, limit: int = 50) -> List[Dict]:
        return [r.as_dict() for r in list(self._records)[-limit:]][::-1]

    def summary(self) -> Dict:
        recs = list(self._records)
        ttfb = sorted(r.ttfb_ms for r in recs if r.ttfb_ms is not None)

        def pct(p: float) -> Optional[int]:
            return ttfb[min(len(ttfb) - 1, int(len(ttfb) * p))] if ttfb else None
        outcomes: Dict[str, int] = {}
        for r in recs:
            outcomes[r.outcome] = outcomes.get(r.outcome, 0) + 1
        return {"streams": len(recs), "outcomes": outcomes, "ttfb_ms_p50": pct(0.5), "ttfb_ms_p95": pct(0.95),
                "fallbacks": sum(max(0, r.attempts - 1) for r in recs),
                "simulated": sum(1 for r in recs if not r.native)}
