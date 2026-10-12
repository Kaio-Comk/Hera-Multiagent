"""Transporte HTTP que valida o destino NO MOMENTO DA CONEXÃO (fase 5, 12/10/2026).

PROBLEMA: `validate_provider_url` resolvia o DNS uma vez (na criação do adaptador) e depois o
cliente HTTP resolvia o nome DE NOVO ao conectar. Um DNS malicioso/instável pode responder um IP
público na checagem e um IP interno (`127.0.0.1`, `169.254.169.254`, rede local) na conexão: DNS
rebinding / TOCTOU.

CORREÇÃO: o backend de rede do httpcore resolve o nome, valida TODOS os IPs e conecta no IP já
validado (o SNI e a verificação de certificado continuam usando o nome, pois o TLS é feito depois
pelo httpcore com o hostname original). IP restrito só é aceito quando o host é literalmente
`localhost`/`127.0.0.1`/`::1`, o IP é loopback e a porta é uma porta local autorizada (Ollama e
9Router); qualquer outro destino interno falha fechado.

Observação: o cliente deixa de usar proxies do ambiente (`trust_env=False`): um proxy corporativo
cairia na regra acima. Quem precisar de proxy deve configurá-lo explicitamente.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from typing import Iterable, Optional

import httpcore
import httpx

from app.gateway.provider_policy import ALLOWED_LOCAL_PORTS, is_ip_private_or_restricted

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class BlockedDestinationError(httpcore.ConnectError):
    """Destino recusado pela política anti-SSRF no momento da conexão."""


class SafeNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, inner: httpcore.AsyncNetworkBackend):
        self._inner = inner

    async def connect_tcp(self, host: str, port: int, timeout: Optional[float] = None,
                          local_address: Optional[str] = None,
                          socket_options: Optional[Iterable] = None):
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise BlockedDestinationError(f"Falha ao resolver {host!r}: {exc}") from exc
        ips = [info[4][0] for info in infos]
        if not ips:
            raise BlockedDestinationError(f"{host!r} não resolveu para nenhum endereço")

        local_ok = host.lower() in LOCAL_HOSTS and port in ALLOWED_LOCAL_PORTS
        for ip in ips:
            restricted = is_ip_private_or_restricted(ip)
            if restricted and not (local_ok and ipaddress.ip_address(ip).is_loopback):
                raise BlockedDestinationError(
                    f"Conexão bloqueada: {host!r}:{port} resolve para {ip} (destino interno/restrito)")
        return await self._inner.connect_tcp(ips[0], port, timeout=timeout,
                                             local_address=local_address, socket_options=socket_options)

    async def connect_unix_socket(self, path: str, timeout: Optional[float] = None,
                                  socket_options: Optional[Iterable] = None):
        raise BlockedDestinationError("Sockets unix não são permitidos pelo gateway")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class SafeAsyncTransport(httpx.AsyncHTTPTransport):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        pool = self._pool
        if not hasattr(pool, "_network_backend"):          # versão do httpcore mudou: falha FECHADA
            raise RuntimeError("httpcore incompatível: não foi possível instalar a guarda de destino")
        pool._network_backend = SafeNetworkBackend(pool._network_backend)


def make_safe_async_client(**kwargs) -> httpx.AsyncClient:
    """AsyncClient cuja conexão só vai para destinos validados (ver docstring do módulo)."""
    return httpx.AsyncClient(transport=SafeAsyncTransport(), trust_env=False, **kwargs)
