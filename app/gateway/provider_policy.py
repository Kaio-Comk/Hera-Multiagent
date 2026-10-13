"""Política de segurança de provedores e proteção contra SSRF / DNS Rebinding.

Garante que nenhuma URL externa ou destino de provedor possa acessar recursos
internos não autorizados, metadados de nuvem ou endereços proibidos.
"""

import ipaddress
import socket
from urllib.parse import urlparse
from typing import Dict, Any, Optional, Set, Tuple

# Portas padrão permitidas para provedores
ALLOWED_WEB_PORTS: Set[int] = {80, 443}
ALLOWED_LOCAL_PORTS: Set[int] = {11434}  # Ollama local

# Domínios oficiais de provedores de nuvem aprovados
APPROVED_CLOUD_DOMAINS: Set[str] = {
    "api.openai.com",
    "api.anthropic.com",
    "api.deepseek.com",
    "api.groq.com",
    "openrouter.ai",
    "generativelanguage.googleapis.com"
}

class SSRFSecurityError(ValueError):
    """Exceção levantada quando uma URL de provedor viola as políticas de SSRF."""
    pass

def is_ip_private_or_restricted(ip_str: str) -> bool:
    """Verifica se um endereço IP é privado, loopback, link-local ou reservado."""
    try:
        ip = ipaddress.ip_address(ip_str)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    except ValueError:
        return False

def resolve_and_verify_destination(hostname: str) -> Tuple[bool, Optional[str]]:
    """Resolve o hostname via DNS e verifica se algum dos IPs resultantes é restrito."""
    try:
        addrinfo = socket.getaddrinfo(hostname, None)
        for family, _, _, _, sockaddr in addrinfo:
            ip = sockaddr[0]
            if is_ip_private_or_restricted(ip):
                return False, f"Hostname '{hostname}' resolveu para IP restrito/privado: {ip}"
        return True, None
    except socket.gaierror as e:
        return False, f"Falha ao resolver hostname '{hostname}': {str(e)}"

def validate_provider_url(url: str, is_local_profile: bool = False) -> str:
    """Valida estritamente a URL de um provedor contra vetores de ataque SSRF.
    
    Args:
        url: A URL completa a ser validada.
        is_local_profile: Se True, permite loopback estrito (127.0.0.1 ou localhost)
                          somente nas portas locais autorizadas (ex: 11434 para Ollama).
    
    Returns:
        A URL sanitizada e normalizada.
    
    Raises:
        SSRFSecurityError: Se a URL violar qualquer regra de segurança.
    """
    if not url or not isinstance(url, str):
        raise SSRFSecurityError("URL do provedor não pode ser vazia")

    clean_url = url.strip()
    parsed = urlparse(clean_url)

    # 1. Esquema estrito
    if parsed.scheme not in ("http", "https"):
        raise SSRFSecurityError(f"Esquema de URL inválido '{parsed.scheme}'. Apenas http e https são permitidos.")

    # 2. Bloquear credenciais embutidas na URL (ex: https://user:pass@evil.com)
    if parsed.username or parsed.password:
        raise SSRFSecurityError("Credenciais de autenticação embutidas na URL são proibidas por segurança.")

    # 3. Validar hostname
    hostname = parsed.hostname
    if not hostname:
        raise SSRFSecurityError(f"Hostname inválido ou ausente na URL: {url}")

    hostname_lower = hostname.lower()

    # 4. Determinar porta
    port = parsed.port
    if port is None:
        port = 443 if parsed.scheme == "https" else 80

    # 5. Tratamento de perfil local (Ollama)
    if is_local_profile:
        is_loopback = hostname_lower in ("127.0.0.1", "localhost", "::1")
        if not is_loopback:
            raise SSRFSecurityError(
                f"Perfil local configurado para destino não-loopback: {hostname}. Proibido."
            )
        if port not in ALLOWED_LOCAL_PORTS:
            raise SSRFSecurityError(
                f"Porta local {port} não autorizada para perfil local. Permitidas: {ALLOWED_LOCAL_PORTS}"
            )
        return clean_url.rstrip("/")

    # 6. Para provedores de nuvem:
    if parsed.scheme != "https":
        raise SSRFSecurityError(f"Provedores de nuvem devem obrigatoriamente usar HTTPS: {clean_url}")

    # Bloquear nomes ou IPs de loopback em conexões cloud
    # Esta lista e de BLOQUEIO de destinos, nao um bind em todas as interfaces (B104 e falso positivo).
    if hostname_lower in ("127.0.0.1", "localhost", "::1", "0.0.0.0"):  # nosec B104
        raise SSRFSecurityError(f"Tentativa de conectar em loopback via perfil cloud: {clean_url}")

    # Verificar se o hostname é um IP direto restrito
    if is_ip_private_or_restricted(hostname_lower):
        raise SSRFSecurityError(f"Tentativa de acesso a endereço IP restrito/privado: {hostname}")

    # Verificar porta de nuvem
    if port not in ALLOWED_WEB_PORTS:
        raise SSRFSecurityError(f"Porta {port} não permitida para provedor de nuvem. Permitidas: {ALLOWED_WEB_PORTS}")

    return clean_url.rstrip("/")
