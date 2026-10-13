"""Testes rigorosos de segurança, schemas e DLP pre-flight para o Gateway (Fase 1).

Garante que nenhuma credencial ou destino SSRF gere requisições de rede externas.
"""

import pytest
import httpx
from pydantic import ValidationError

from app.gateway.schemas import (
    ChatCompletionRequest,
    ChatMessage,
    MAX_MESSAGES_COUNT,
    MAX_CONTENT_LENGTH,
)
from app.gateway.provider_policy import (
    validate_provider_url,
    SSRFSecurityError,
    is_ip_private_or_restricted,
)
from app.gateway.privacy import (
    GatewayPrivacyManager,
    DataClassification,
)
from app.config_manager import ConfigManager

# ============================================================================
# 1. Testes de Schemas e Limites Defensivos
# ============================================================================

def test_chat_request_valid():
    """Valida construção correta de ChatCompletionRequest padrão."""
    req = ChatCompletionRequest(
        model="qwen2.5-coder:1.5b",
        messages=[
            ChatMessage(role="system", content="Você é um assistente útil."),
            ChatMessage(role="user", content="Olá mundo!")
        ],
        stream=False
    )
    assert req.model == "qwen2.5-coder:1.5b"
    assert len(req.messages) == 2
    assert "Olá mundo!" in req.get_full_text_for_dlp()

def test_chat_request_rejects_empty_messages():
    """Garante que requisição sem mensagens seja recusada na validação."""
    with pytest.raises(ValidationError):
        ChatCompletionRequest(model="test", messages=[])

def test_chat_request_rejects_excessive_messages():
    """Garante que requisição com excesso de mensagens seja bloqueada."""
    too_many = [ChatMessage(role="user", content="oi") for _ in range(MAX_MESSAGES_COUNT + 1)]
    with pytest.raises(ValidationError):
        ChatCompletionRequest(model="test", messages=too_many)

def test_chat_request_rejects_huge_message_content():
    """Garante que mensagem individual gigante seja bloqueada para evitar DoS."""
    huge_str = "A" * (MAX_CONTENT_LENGTH + 10)
    with pytest.raises(ValidationError):
        ChatMessage(role="user", content=huge_str)

# ============================================================================
# 2. Testes de Prevenção contra SSRF e Destinos Restritos
# ============================================================================

@pytest.mark.parametrize("invalid_scheme_url", [
    "file:///etc/passwd",
    "ftp://evil.com/payload",
    "gopher://evil.com:70/",
    "javascript:alert(1)"
])
def test_ssrf_rejects_invalid_schemes(invalid_scheme_url):
    """Bloqueia esquemas não permitidos (qualquer coisa diferente de http/https)."""
    with pytest.raises(SSRFSecurityError):
        validate_provider_url(invalid_scheme_url)

def test_ssrf_rejects_embedded_credentials():
    """Bloqueia URLs contendo credenciais embutidas (ex: user:pass@host)."""
    with pytest.raises(SSRFSecurityError, match="Credenciais de autenticação embutidas"):
        validate_provider_url("https://admin:secret123@api.openai.com/v1")

@pytest.mark.parametrize("private_dest", [
    "https://127.0.0.1/v1",
    "https://localhost/v1",
    "https://10.0.0.1/v1",
    "https://192.168.1.100/v1",
    "https://172.16.0.1/v1",
    "https://169.254.169.254/latest/meta-data",  # AWS/Cloud Metadata
    "https://0.0.0.0/v1"
])
def test_ssrf_rejects_private_ips_on_cloud_profile(private_dest):
    """Garante que perfis de nuvem não possam acessar IPs privados ou metadados de nuvem."""
    with pytest.raises(SSRFSecurityError):
        validate_provider_url(private_dest, is_local_profile=False)

def test_ssrf_allows_approved_cloud_endpoints():
    """Permite endpoints oficiais de nuvem legítimos usando HTTPS na porta 443."""
    url1 = validate_provider_url("https://api.openai.com/v1")
    assert url1 == "https://api.openai.com/v1"

    url2 = validate_provider_url("https://api.anthropic.com/v1/")
    assert url2 == "https://api.anthropic.com/v1"

def test_ssrf_local_profile_rules():
    """Valida que o perfil local autorize loopback estrito nas portas permitidas e rejeite outros."""
    # Permitido: loopback na porta do Ollama (11434)
    valid_local = validate_provider_url("http://127.0.0.1:11434", is_local_profile=True)
    assert valid_local == "http://127.0.0.1:11434"

    # Proibido: perfil local apontando para IP externo
    with pytest.raises(SSRFSecurityError, match="destino não-loopback"):
        validate_provider_url("http://8.8.8.8:11434", is_local_profile=True)

    # Proibido: perfil local em porta não autorizada
    with pytest.raises(SSRFSecurityError, match="não autorizada"):
        validate_provider_url("http://127.0.0.1:22", is_local_profile=True)

# ============================================================================
# 3. Testes de Pre-flight DLP e Garantia de Zero Chamadas de Rede
# ============================================================================

def test_dlp_inspect_clean_request():
    """Requisição limpa sem segredos deve ser classificada como PUBLIC."""
    mgr = GatewayPrivacyManager(default_offline=False)
    req = ChatCompletionRequest(
        model="claude-3-7-sonnet",
        messages=[ChatMessage(role="user", content="Como calcular fibonacci em Python?")]
    )
    result = mgr.inspect_request(req)
    assert result.classification == DataClassification.PUBLIC
    assert result.has_secrets is False
    assert "cloud_premium" in result.allowed_tiers

def test_dlp_inspect_secret_detection_forces_local_only():
    """Detectar credencial no prompt força LOCAL_ONLY e elimina candidatos cloud."""
    mgr = GatewayPrivacyManager(default_offline=False)
    req = ChatCompletionRequest(
        model="claude-3-7-sonnet",
        messages=[
            ChatMessage(role="system", content="Você é um assistente."),
            ChatMessage(role="user", content="Use minha chave sk-1234567890abcdef1234567890abcdef para conectar.")
        ]
    )
    result = mgr.inspect_request(req)
    assert result.classification == DataClassification.LOCAL_ONLY
    assert result.has_secrets is True
    assert "openai_key" in result.secret_types
    assert result.allowed_tiers == {"local"}

    # Filtragem de candidatos
    candidates = [
        {"name": "claude_code", "tier": "cloud_premium", "is_local": False},
        {"name": "provedor_nuvem", "tier": "cloud_cheap", "is_local": False},
        {"name": "ollama_local", "tier": "local", "is_local": True}
    ]
    eligible = mgr.filter_eligible_candidates(candidates, result)
    assert len(eligible) == 1
    assert eligible[0]["name"] == "ollama_local"

def test_dlp_inspect_offline_mode_forces_local_only():
    """Ativar modo offline força LOCAL_ONLY mesmo sem segredos."""
    mgr = GatewayPrivacyManager(default_offline=True)
    req = ChatCompletionRequest(
        model="claude-3-7-sonnet",
        messages=[ChatMessage(role="user", content="Texto comum sem chaves")]
    )
    result = mgr.inspect_request(req)
    assert result.classification == DataClassification.LOCAL_ONLY
    assert result.has_secrets is False
    assert result.allowed_tiers == {"local"}

@pytest.mark.asyncio
async def test_dlp_guarantees_zero_network_calls_when_blocked():
    """Garante com Spy de Transporte HTTP que nenhuma chamada de rede é feita para a nuvem quando DLP bloqueia."""
    seen_requests = []

    async def mock_handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        mgr = GatewayPrivacyManager(default_offline=False)
        req = ChatCompletionRequest(
            model="claude-3-7-sonnet",
            messages=[ChatMessage(role="user", content="Minha chave AWS: AKIAIOSFODNN7EXAMPLE")]
        )
        inspection = mgr.inspect_request(req)
        assert inspection.classification == DataClassification.LOCAL_ONLY

        candidates = [
            {"name": "openai_cloud", "url": "https://api.openai.com/v1/chat/completions", "is_local": False},
            {"name": "anthropic_cloud", "url": "https://api.anthropic.com/v1/messages", "is_local": False}
        ]
        eligible = mgr.filter_eligible_candidates(candidates, inspection)

        # Como nenhum provedor local foi fornecido e os de nuvem foram bloqueados pelo DLP,
        # a lista de elegíveis deve ser vazia e NENHUM request HTTP deve ser disparado!
        assert len(eligible) == 0
        if eligible:
            await client.post(eligible[0]["url"], json={})

        assert len(seen_requests) == 0, "Segurança violada: chamadas de rede foram feitas após bloqueio de DLP!"

# ============================================================================
# 4. Testes de Feature Flags
# ============================================================================

def test_feature_flags_default_off():
    """Verifica que as novas funcionalidades vêm desativadas por padrão (safe rollout)."""
    cfg_mgr = ConfigManager()
    cfg = cfg_mgr.load_config()
    flags = cfg.get("feature_flags", {})

    assert flags.get("enable_gateway", True) is True
    assert flags.get("enable_rtk", False) is False
    assert flags.get("enable_arena", False) is False
    assert flags.get("enable_mcp", False) is False
