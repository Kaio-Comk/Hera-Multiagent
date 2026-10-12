"""Streaming de verdade: deltas chegam enquanto o provedor ainda gera; pré-voo com fallback; sem fallback
depois do primeiro byte; cancelamento fecha o upstream; keep-alive; telemetria; privacidade."""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

import app.server as server
from app.gateway.key_manager import KeyManager
from app.gateway.providers import (BaseGatewayAdapter, CloudOpenAIGatewayAdapter, GatewayProviderError,
                                   OllamaGatewayAdapter)
from app.gateway.schemas import ChatCompletionRequest, ChatMessage
from app.gateway.tier_router import TierRouter
from helpers import AUTH_HEADERS, TEST_BASE_URL


# Segredo FALSO para provar o DLP. Montado em tempo de execucao de proposito: o literal "sk-" + 32 hex e
# lido pelo scanner de segredos do GitHub como chave de verdade e bloqueia o push.
FAKE_SECRET = "sk-" + "abcdef0123456789" * 2


def req(model="default", text="oi", stream=True):
    return ChatCompletionRequest(model=model, stream=stream, messages=[ChatMessage(role="user", content=text)])


def nd(*objs):
    return ("\n".join(json.dumps(o) for o in objs) + "\n").encode()


def sse(*events, done=True):
    body = "".join(f"data: {json.dumps(e)}\n\n" for e in events)
    return (body + ("data: [DONE]\n\n" if done else "")).encode()


def oa(content=None, finish=None, tool_calls=None, usage=None):
    delta = {}
    if content is not None:
        delta["content"] = content
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    ev = {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage:
        ev["usage"] = usage
    return ev


def ollama_line(text, done=False, **extra):
    return {"message": {"role": "assistant", "content": text}, "done": done, **extra}


async def collect(gen):
    return [e async for e in gen]


def text_of(events):
    out = ""
    for e in events:
        if e.startswith("data: {"):
            d = json.loads(e[6:])
            for c in d.get("choices", []):
                out += c.get("delta", {}).get("content") or ""
    return out


def cloud(**kw):
    return CloudOpenAIGatewayAdapter(name="deep", base_url="https://api.deepseek.com/v1",
                                     key_manager=KeyManager(["sk-aaaaaaaaaaaaaaaaaaaa"]),
                                     default_model="deepseek-chat", tier="cloud_cheap", **kw)


class Tracked(httpx.AsyncByteStream):
    """Corpo de resposta que registra se a CONEXAO foi fechada (`aclose`): e isso que para o provedor."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    async def __aiter__(self):
        async for c in self._chunks():
            yield c

    async def aclose(self):
        self.closed = True


def client_for(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ── streaming de verdade ───────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_o_primeiro_pedaco_chega_ANTES_do_provedor_terminar():
    gate = asyncio.Event()

    async def body():
        yield nd(ollama_line("Olá"))
        await gate.wait()                                   # o provedor ainda não terminou...
        yield nd(ollama_line(" mundo"), ollama_line("", done=True, done_reason="stop", prompt_eval_count=3, eval_count=2))

    async def handler(r):
        return httpx.Response(200, content=body())
    router = TierRouter(adapters=[OllamaGatewayAdapter()])
    async with client_for(handler) as http:
        gen = await router.route_and_stream(req(), http)
        first = await asyncio.wait_for(gen.__anext__(), 2)            # role
        second = await asyncio.wait_for(gen.__anext__(), 2)           # "Olá" JÁ chegou, sem esperar o resto
        assert '"role": "assistant"' in first and '"content": "Olá"' in second
        gate.set()
        rest = await collect(gen)
    assert text_of(rest) == " mundo" and rest[-1] == "data: [DONE]\n\n" and '"finish_reason": "stop"' in rest[-2]


@pytest.mark.asyncio
async def test_nuvem_sse_com_tool_calls_fragmentados_finish_e_usage_em_cache():
    frags = [oa(tool_calls=[{"index": 0, "id": "call_1", "function": {"name": "busca", "arguments": '{"q":'}}]),
             oa(tool_calls=[{"index": 0, "function": {"arguments": ' "x"}'}}]),
             oa(finish="tool_calls", usage={"prompt_tokens": 10, "completion_tokens": 4,
                                            "prompt_tokens_details": {"cached_tokens": 8}})]

    async def handler(r):
        return httpx.Response(200, content=sse(oa("Vou "), oa("buscar"), *frags))
    router = TierRouter(adapters=[cloud()])
    async with client_for(handler) as http:
        events = await collect(await router.route_and_stream(req("deepseek-chat"), http))
    assert text_of(events) == "Vou buscar"
    deltas = [json.loads(e[6:])["choices"][0]["delta"] for e in events if e.startswith("data: {")]
    calls = [d["tool_calls"][0] for d in deltas if "tool_calls" in d]
    assert calls[0]["id"] == "call_1" and calls[0]["function"] == {"name": "busca", "arguments": '{"q":'}
    assert calls[1]["function"] == {"arguments": ' "x"}'}                    # fragmento mantido como veio
    assert '"finish_reason": "tool_calls"' in events[-2]
    rec = router.telemetry.recent(1)[0]
    assert (rec["prompt_tokens"], rec["completion_tokens"], rec["cached_tokens"]) == (10, 4, 8)
    assert rec["outcome"] == "completed" and rec["native"] is True and rec["chars"] == 10


@pytest.mark.asyncio
async def test_linhas_de_comentario_e_lixo_do_sse_sao_ignoradas():
    async def handler(r):
        return httpx.Response(200, content=b": keep-alive\n\nevent: ping\n\n" + sse(oa("ok"), oa(finish="stop")))
    router = TierRouter(adapters=[cloud()])
    async with client_for(handler) as http:
        events = await collect(await router.route_and_stream(req("deepseek-chat"), http))
    assert text_of(events) == "ok"


# ── pré-voo e fallback ─────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_falha_antes_do_primeiro_dado_faz_fallback_para_o_proximo_provedor():
    log = []

    async def handler(r):
        if "deepseek" in str(r.url):
            log.append("cloud")
            return httpx.Response(503, json={})
        log.append("ollama")
        return httpx.Response(200, content=nd(ollama_line("do local"), ollama_line("", done=True)))
    router = TierRouter(adapters=[cloud(), OllamaGatewayAdapter()])
    async with client_for(handler) as http:
        events = await collect(await router.route_and_stream(req("default"), http))
    assert log == ["cloud", "ollama"] and text_of(events) == "do local"
    assert router.telemetry.recent(1)[0]["attempts"] == 2 and router.telemetry.summary()["fallbacks"] == 1


@pytest.mark.asyncio
async def test_evento_de_erro_no_pre_voo_tambem_faz_fallback():
    async def handler(r):
        if "deepseek" in str(r.url):
            return httpx.Response(200, content=sse({"error": {"message": "overloaded"}}, done=False))
        return httpx.Response(200, content=nd(ollama_line("ok"), ollama_line("", done=True)))
    router = TierRouter(adapters=[cloud(), OllamaGatewayAdapter()])
    async with client_for(handler) as http:
        events = await collect(await router.route_and_stream(req("default"), http))
    assert text_of(events) == "ok"


@pytest.mark.asyncio
async def test_erro_nao_retryable_antes_do_stream_vira_excecao_http_sem_fallback():
    log = []

    async def handler(r):
        log.append(str(r.url))
        return httpx.Response(400, json={})
    router = TierRouter(adapters=[cloud(), OllamaGatewayAdapter()])
    async with client_for(handler) as http:
        with pytest.raises(GatewayProviderError) as exc:
            await router.route_and_stream(req("default"), http)
    assert exc.value.status_code == 400 and len(log) == 1
    assert router.semaphore._value == 10                                  # liberou o semáforo


@pytest.mark.asyncio
async def test_todos_os_provedores_falham_no_pre_voo_levanta_o_ultimo_erro_e_libera_o_semaforo():
    async def handler(r):
        return httpx.Response(503, json={})
    router = TierRouter(adapters=[cloud(), OllamaGatewayAdapter()], max_concurrent_requests=3)
    async with client_for(handler) as http:
        with pytest.raises(GatewayProviderError) as exc:
            await router.route_and_stream(req("default"), http)
    assert exc.value.status_code == 503 and router.semaphore._value == 3


# ── depois do primeiro byte: sem fallback ──────────────────────────────────────
@pytest.mark.asyncio
async def test_falha_no_meio_do_stream_encerra_com_erro_e_NAO_troca_de_provedor():
    log = []

    async def body():
        yield sse(oa("parte 1"), done=False)
        raise httpx.ReadError("conexão caiu")

    async def handler(r):
        log.append("cloud" if "deepseek" in str(r.url) else "ollama")
        if "deepseek" in str(r.url):
            return httpx.Response(200, content=body())
        return httpx.Response(200, content=nd(ollama_line("NÃO DEVIA"), ollama_line("", done=True)))
    router = TierRouter(adapters=[cloud(), OllamaGatewayAdapter()])
    async with client_for(handler) as http:
        events = await collect(await router.route_and_stream(req("default"), http))
    assert log == ["cloud"]                                                  # nunca tentou o Ollama
    assert text_of(events) == "parte 1" and "gateway_stream_error" in events[-2] and events[-1] == "data: [DONE]\n\n"
    assert "NÃO DEVIA" not in "".join(events)
    rec = router.telemetry.recent(1)[0]
    assert rec["outcome"] == "error" and "ReadError" in rec["error"]


# ── cancelamento ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_cliente_desconecta_fecha_o_upstream_libera_o_semaforo_e_registra_cancelado():
    async def chunks():
        yield nd(ollama_line("a"))
        await asyncio.sleep(30)                                               # provedor ainda gerando
        yield nd(ollama_line("b"))
    stream = Tracked(chunks)

    async def handler(r):
        return httpx.Response(200, stream=stream)
    router = TierRouter(adapters=[OllamaGatewayAdapter()], max_concurrent_requests=2)
    async with client_for(handler) as http:
        gen = await router.route_and_stream(req(), http)
        await gen.__anext__()
        await gen.__anext__()                                                 # recebeu "a"
        assert router.semaphore._value == 1 and stream.closed is False
        await gen.aclose()                                                    # o cliente foi embora
    assert stream.closed is True                                              # a conexão com o provedor foi fechada
    assert router.semaphore._value == 2
    assert router.telemetry.recent(1)[0]["outcome"] == "cancelled"


# ── keep-alive e inatividade ───────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_provedor_quieto_gera_keepalive_e_o_stream_continua():
    async def body():
        yield nd(ollama_line("a"))
        await asyncio.sleep(0.25)
        yield nd(ollama_line("b"), ollama_line("", done=True))

    async def handler(r):
        return httpx.Response(200, content=body())
    router = TierRouter(adapters=[OllamaGatewayAdapter()], keepalive_seconds=0.05, stream_idle_timeout_seconds=5)
    async with client_for(handler) as http:
        events = await collect(await router.route_and_stream(req(), http))
    assert any(e.startswith(": keep-alive") for e in events) and text_of(events) == "ab"


@pytest.mark.asyncio
async def test_provedor_mudo_alem_do_limite_vira_erro_504_e_fecha_o_upstream():
    async def chunks():
        yield nd(ollama_line("a"))
        await asyncio.sleep(30)
    stream = Tracked(chunks)

    async def handler(r):
        return httpx.Response(200, stream=stream)
    router = TierRouter(adapters=[OllamaGatewayAdapter()], keepalive_seconds=0.05, stream_idle_timeout_seconds=0.2)
    async with client_for(handler) as http:
        events = await collect(await router.route_and_stream(req(), http))
    assert "gateway_stream_error" in events[-2] and '"code": 504' in events[-2] and stream.closed is True


@pytest.mark.asyncio
async def test_prazo_maximo_do_stream():
    async def body():
        for _ in range(100):
            yield nd(ollama_line("x"))
            await asyncio.sleep(0.03)

    async def handler(r):
        return httpx.Response(200, content=body())
    router = TierRouter(adapters=[OllamaGatewayAdapter()], stream_total_timeout_seconds=0.2)
    async with client_for(handler) as http:
        events = await collect(await router.route_and_stream(req(), http))
    assert "prazo" in events[-2] or "gateway_stream_error" in events[-2]
    assert router.telemetry.recent(1)[0]["outcome"] == "error"


# ── privacidade no stream ──────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_prompt_com_segredo_no_stream_so_abre_o_ollama():
    log = []

    async def handler(r):
        log.append("cloud" if "deepseek" in str(r.url) else "ollama")
        return httpx.Response(200, content=nd(ollama_line("ok"), ollama_line("", done=True)))
    router = TierRouter(adapters=[cloud(), OllamaGatewayAdapter()])
    async with client_for(handler) as http:
        await collect(await router.route_and_stream(
            req("default", f"minha chave {FAKE_SECRET}"), http))
    assert log == ["ollama"]


@pytest.mark.asyncio
async def test_segredo_sem_provedor_local_bloqueia_com_403_antes_de_abrir_qualquer_conexao():
    log = []

    async def handler(r):
        log.append(1)
        return httpx.Response(200, content=b"")
    router = TierRouter(adapters=[cloud()])
    async with client_for(handler) as http:
        with pytest.raises(GatewayProviderError) as exc:
            await router.route_and_stream(req("deepseek-chat", FAKE_SECRET), http)
    assert exc.value.status_code == 403 and log == [] and router.semaphore._value == 10


# ── compatibilidade: adaptador sem streaming nativo ────────────────────────────
@pytest.mark.asyncio
async def test_adaptador_sem_streaming_nativo_cai_no_modo_simulado_e_a_telemetria_marca():
    class Antigo(BaseGatewayAdapter):
        name, tier, is_local = "antigo", "local", True

        async def chat_complete(self, request, client):
            from app.gateway.schemas import ChatChoice, ChatCompletionResponse
            return ChatCompletionResponse(id="c", created=1, model="m", choices=[ChatChoice(
                message=ChatMessage(role="assistant", content="x" * 70), finish_reason="stop")])
    router = TierRouter(adapters=[Antigo()])
    async with httpx.AsyncClient() as http:
        events = await collect(await router.route_and_stream(req(), http))
    assert text_of(events) == "x" * 70 and events[-1] == "data: [DONE]\n\n"
    assert router.telemetry.recent(1)[0]["native"] is False and router.telemetry.summary()["simulated"] == 1


# ── concorrência ───────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_limite_de_concorrencia_segura_o_segundo_stream_ate_o_primeiro_terminar():
    async def handler(r):
        return httpx.Response(200, content=nd(ollama_line("a"), ollama_line("", done=True)))
    router = TierRouter(adapters=[OllamaGatewayAdapter()], max_concurrent_requests=1)
    async with client_for(handler) as http:
        first = await router.route_and_stream(req(), http)
        second = asyncio.ensure_future(router.route_and_stream(req(), http))
        await asyncio.sleep(0.1)
        assert not second.done()                                              # esperando vaga
        await collect(first)                                                  # primeiro termina
        gen2 = await asyncio.wait_for(second, 2)
        assert text_of(await collect(gen2)) == "a"


# ── endpoint ───────────────────────────────────────────────────────────────────
def test_endpoint_v1_stream_e_telemetria_sem_conteudo(monkeypatch):
    async def handler(r):
        return httpx.Response(200, content=nd(ollama_line("Olá"), ollama_line(" mundo"),
                                              ollama_line("", done=True, prompt_eval_count=2, eval_count=2)))
    monkeypatch.setattr(server, "make_safe_async_client", lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(server, "gateway_http_client", None)
    monkeypatch.setattr(server.gateway_tier_router, "adapters", [OllamaGatewayAdapter()])
    monkeypatch.setattr(server.router, "default_offline", False)
    monkeypatch.setattr(server.gateway_privacy_mgr, "default_offline", False)
    c = TestClient(server.app, base_url=TEST_BASE_URL, headers=AUTH_HEADERS)
    r = c.post("/v1/chat/completions", json={"model": "default", "stream": True,
                                             "messages": [{"role": "user", "content": "segredo-do-usuario-123"}]})
    assert r.status_code == 200 and "text/event-stream" in r.headers["content-type"]
    events = [e + "\n\n" for e in r.text.split("\n\n") if e]
    assert text_of(events) == "Olá mundo" and r.text.endswith("data: [DONE]\n\n")
    t = c.get("/api/gateway/telemetry").json()
    assert t["summary"]["streams"] >= 1 and t["recent"][0]["outcome"] == "completed"
    assert "segredo-do-usuario-123" not in json.dumps(t) and "Olá" not in json.dumps(t)     # sem conteúdo
    assert TestClient(server.app, base_url=TEST_BASE_URL).get("/api/gateway/telemetry").status_code == 401
