"""Roteador (ideias do 9Router 0.5.99): várias contas por provedor, cabeçalhos extras por provedor e cota informada pelo provedor."""
import httpx
import pytest

from app.gateway.provider_policy import sanitize_extra_headers
from app.gateway.providers import CloudOpenAIGatewayAdapter, GatewayProviderError, parse_rate_limit_headers, parse_reset_seconds
from app.gateway.schemas import ChatCompletionRequest, ChatMessage
from app.gateway.secret_store import SecretStore, SecretStoreError
from test_router_completo import api, client, code  # noqa: F401  (fixture `api`)

K1, K2, K3 = "sk-conta-um-1234567", "sk-conta-dois-7654321", "sk-conta-tres-0000111"


# --------------------------------------------------------------------------- cofre: várias contas
def test_cofre_acrescenta_sem_apagar_e_sem_repetir(tmp_path):
    s = SecretStore(tmp_path / "k.json", use_dpapi=False)
    assert s.add("groq", [K1]) == 1
    assert s.add("groq", [K2, K1]) == 2
    assert s.get("groq") == [K1, K2]
    assert s.hints("groq") == ["…4567", "…4321"]


def test_cofre_tira_uma_chave_e_limpa_o_provedor_quando_acaba(tmp_path):
    s = SecretStore(tmp_path / "k.json", use_dpapi=False)
    s.set("groq", [K1, K2, K3])
    assert s.remove_at("groq", 1) == 2 and s.get("groq") == [K1, K3]
    with pytest.raises(SecretStoreError):
        s.remove_at("groq", 5)
    s.remove_at("groq", 0)
    s.remove_at("groq", 0)
    assert s.names() == []


def test_cofre_respeita_o_limite_de_contas(tmp_path):
    s = SecretStore(tmp_path / "k.json", use_dpapi=False)
    s.set("groq", [f"sk-chave-numero-{i:04d}" for i in range(20)])
    with pytest.raises(SecretStoreError):
        s.add("groq", ["sk-mais-uma-chave-0001"])


# --------------------------------------------------------------------------- cabeçalhos extras
def test_cabecalhos_extras_validos_e_proibidos():
    assert sanitize_extra_headers({"HTTP-Referer": "https://meusite.com", "X-Title": "Hera"}) == {
        "HTTP-Referer": "https://meusite.com", "X-Title": "Hera"}
    assert sanitize_extra_headers(None) == {} and sanitize_extra_headers({}) == {}
    for bad in ({"Authorization": "Bearer x"}, {"content-type": "text/plain"}, {"Host": "evil"}, {"Cookie": "a=b"},
                {"X-Ok": "quebra\r\nInjetado: 1"}, {"com espaco": "v"}, {"X-Vazio": " "}, {"X-Longo": "a" * 501}):
        with pytest.raises(ValueError):
            sanitize_extra_headers(bad)
    with pytest.raises(ValueError):
        sanitize_extra_headers({f"X-H{i}": "v" for i in range(11)})


def _req():
    return ChatCompletionRequest(model="m", messages=[ChatMessage(role="user", content="oi")])


def _ok_body():
    return {"id": "x", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}


@pytest.mark.asyncio
async def test_adaptador_envia_cabecalhos_extras_mas_a_chave_e_sempre_a_do_hera():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(200, json=_ok_body())

    ad = CloudOpenAIGatewayAdapter("p", "https://api.groq.com/openai/v1", api_key=K1, default_model="m",
                                   extra_headers={"HTTP-Referer": "https://meusite.com"})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await ad.chat_complete(_req(), http)
    assert seen["http-referer"] == "https://meusite.com" and seen["authorization"] == f"Bearer {K1}"


# --------------------------------------------------------------------------- cota
def test_leitura_de_cota_e_duracoes():
    assert parse_reset_seconds("6m0s") == 360 and parse_reset_seconds("1h2m") == 3720
    assert parse_reset_seconds("120ms") == pytest.approx(0.12) and parse_reset_seconds("42") == 42
    assert parse_reset_seconds("amanha") is None and parse_reset_seconds(None) is None
    q = parse_rate_limit_headers({"x-ratelimit-limit-requests": "30", "x-ratelimit-remaining-requests": "29",
                                  "x-ratelimit-remaining-tokens": "5000", "x-ratelimit-reset-tokens": "2s"}, now=100)
    assert q["limit_requests"] == 30 and q["remaining_requests"] == 29 and q["remaining_tokens"] == 5000
    assert q["reset_tokens_s"] == 2 and q["updated_at"] == 100
    prev = {"remaining_requests": 1}
    assert parse_rate_limit_headers({}, previous=prev) is prev          # sem cabeçalhos: mantém o último valor


@pytest.mark.asyncio
async def test_cota_zerada_poe_a_conta_para_descansar_e_usa_a_proxima():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.headers["authorization"])
        if len(calls) == 1:       # 1a chamada: ok, mas avisa que a cota de requisições acabou e volta em 60 s
            return httpx.Response(200, json=_ok_body(), headers={"x-ratelimit-remaining-requests": "0",
                                                                 "x-ratelimit-reset-requests": "1m0s"})
        return httpx.Response(200, json=_ok_body())

    from app.gateway.key_manager import KeyManager
    ad = CloudOpenAIGatewayAdapter("p", "https://api.groq.com/openai/v1", default_model="m", key_manager=KeyManager([K1, K2], label="p"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await ad.chat_complete(_req(), http)
        await ad.chat_complete(_req(), http)
        await ad.chat_complete(_req(), http)
    assert calls[0] == f"Bearer {K1}" and calls[1] == f"Bearer {K2}" and calls[2] == f"Bearer {K2}"   # K1 descansa
    assert ad.quota["remaining_requests"] == 0 and ad.quota["reset_requests_s"] == 60      # a cota fica visível no painel


# --------------------------------------------------------------------------- API
@pytest.mark.asyncio
async def test_api_varias_contas_cabecalhos_e_remocao(api):  # noqa: F811
    async with client() as c:
        await c.post("/api/router/providers", json={"preset": "groq"})
        r = await c.post("/api/router/providers/groq/keys", json={"keys": [K1]})
        assert r.status_code == 200 and r.json()["keys_stored"] == 1
        r = await c.post("/api/router/providers/groq/keys", json={"keys": [K2], "mode": "append"})
        assert r.json()["keys_stored"] == 2                               # acrescentou, não trocou
        state = (await c.get("/api/router/state")).json()
        prov = next(p for p in state["providers"] if p["name"] == "groq")
        assert prov["stored_hints"] == ["…4567", "…4321"] and K1 not in str(state) and K2 not in str(state)

        r = await c.post("/api/router/providers/groq/keys", json={"keys": [K3]})          # padrão = substitui tudo
        assert r.json()["keys_stored"] == 1
        await c.post("/api/router/providers/groq/keys", json={"keys": [K1], "mode": "append"})
        assert (await c.delete("/api/router/providers/groq/keys/0")).json()["keys_stored"] == 1
        assert (await c.delete("/api/router/providers/groq/keys/9")).status_code == 404

        ok = await c.put("/api/router/providers/groq/headers", json={"headers": {"X-Title": "Hera"}})
        assert ok.status_code == 200 and ok.json()["headers"] == {"X-Title": "Hera"}
        bad = await c.put("/api/router/providers/groq/headers", json={"headers": {"Authorization": "Bearer x"}})
        assert bad.status_code == 400
        state = (await c.get("/api/router/state")).json()
        assert next(p for p in state["providers"] if p["name"] == "groq")["headers"] == {"X-Title": "Hera"}
        assert (await c.put("/api/router/providers/inexistente/headers", json={"headers": {}})).status_code == 404


# --------------------------------------------------------------------------- endereço com parâmetro (ex.: ID da conta Cloudflare)
@pytest.mark.asyncio
async def test_preset_com_parametro_monta_o_endereco_ou_pede_o_que_falta(api):  # noqa: F811
    async with client() as c:
        presets = {p["id"]: p for p in (await c.get("/api/router/state")).json()["presets"]}
        assert presets["cloudflare-ai"]["params"][0]["key"] == "account_id"
        faltando = await c.post("/api/router/providers", json={"preset": "cloudflare-ai"})
        assert faltando.status_code == 400 and "ID da conta" in faltando.json()["detail"]["message"]
        ruim = await c.post("/api/router/providers", json={"preset": "cloudflare-ai", "params": {"account_id": "../x"}})
        assert ruim.status_code == 400
        ok = await c.post("/api/router/providers", json={"preset": "cloudflare-ai", "params": {"account_id": "abc123def456"}})
        assert ok.status_code == 200
        assert ok.json()["provider"]["base_url"] == "https://api.cloudflare.com/client/v4/accounts/abc123def456/ai/v1"
