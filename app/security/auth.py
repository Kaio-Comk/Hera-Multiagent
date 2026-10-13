"""Autenticacao local da API do Hera (fase 1 do plano de correcoes de 12/10/2026).

POR QUE EXISTE: o servidor escuta so em 127.0.0.1, mas qualquer site aberto no navegador do
usuario consegue chamar http://127.0.0.1:8000 (o CORS era `*` e nada exigia credencial). Isso
permitia rodar tarefas, mudar a configuracao (inclusive o modo offline do DLP), gastar as chaves
de nuvem pelo /v1 e ler os logs pelo WebSocket. Agora, em ordem:

  1. **Host** precisa ser local (127.0.0.1 / localhost / ::1): fecha DNS rebinding;
  2. **Origin**, quando o navegador manda, precisa ser a propria origem do servidor: fecha CSRF
     e leitura cross-site (nao ha mais CORS para origens externas);
  3. **Token** (`Authorization: Bearer` ou `X-Hera-Token`; no WebSocket tambem `?token=`),
     comparado em tempo constante. Publicos: apenas `/` (a UI, que recebe o token embutido) e
     `/healthz` (sem dados);
  4. **Limite de taxa** por token nas rotas caras (/v1/chat/completions, tarefas, arena, pull).
  5. **Chaves de acesso de cliente** (opcionais, `client_keys`): servem SÓ em `/v1/chat/completions` e `/v1/models`
     e carregam os próprios limites; nunca abrem rotas de gestão.

Falha fechada: sem token valido nada executa.
"""
from __future__ import annotations

import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, Mapping, Optional, Set, Tuple
from urllib.parse import parse_qs

from starlette.responses import JSONResponse

TOKEN_ENV = "HERA_API_KEY"
ALLOWED_HOSTS_ENV = "HERA_ALLOWED_HOSTS"
_MIN_TOKEN_LEN = 24

DEFAULT_PUBLIC_PATHS = frozenset({"/", "/healthz"})
DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024                       # 1 MiB para a maioria das rotas
DEFAULT_BODY_LIMITS: Dict[str, int] = {"/v1/chat/completions": 8 * 1024 * 1024}   # conversas longas
DEFAULT_RATE_LIMITS: Dict[str, Tuple[int, float]] = {
    "/v1/chat/completions": (60, 60.0),
    "/api/tasks/run": (20, 60.0),
    "/api/arena/run": (10, 60.0),
    "/api/ollama/pull": (5, 60.0),
}
SECURITY_HEADERS = {
    b"x-frame-options": b"DENY",                    # a UI tem botoes de aprovar merge: sem clickjacking
    b"x-content-type-options": b"nosniff",
    b"referrer-policy": b"no-referrer",
    b"content-security-policy": b"frame-ancestors 'none'",
}


def load_or_create_token(path: Path, environ: Optional[Mapping[str, str]] = None) -> str:
    """Token da sessao: `HERA_API_KEY` (se definido) > arquivo `path` > novo token aleatorio."""
    env = os.environ if environ is None else environ
    fixed = (env.get(TOKEN_ENV) or "").strip()
    if len(fixed) >= _MIN_TOKEN_LEN:
        return fixed
    path = Path(path)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= _MIN_TOKEN_LEN:
            return existing
    except OSError:
        pass
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")
    try:
        os.chmod(path, 0o600)                       # no Windows e ignorado; em POSIX restringe ao dono
    except OSError:
        pass
    return token


@dataclass
class SecuritySettings:
    token: str
    allowed_hosts: Set[str] = field(default_factory=lambda: {"127.0.0.1", "localhost", "::1"})
    public_paths: frozenset = DEFAULT_PUBLIC_PATHS
    rate_limits: Dict[str, Tuple[int, float]] = field(default_factory=lambda: dict(DEFAULT_RATE_LIMITS))
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    body_limits: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_BODY_LIMITS))

    @classmethod
    def from_env(cls, token_file: Path, environ: Optional[Mapping[str, str]] = None) -> "SecuritySettings":
        env = os.environ if environ is None else environ
        settings = cls(token=load_or_create_token(token_file, env))
        extra = {h.strip().lower() for h in (env.get(ALLOWED_HOSTS_ENV) or "").split(",") if h.strip()}
        settings.allowed_hosts |= extra
        return settings


def _hostname(host_header: str) -> str:
    h = host_header.strip().lower()
    if h.startswith("["):                           # IPv6 literal: [::1]:8000
        return h[1:].split("]")[0]
    return h.rsplit(":", 1)[0] if ":" in h else h


class LocalSecurityMiddleware:
    """Middleware ASGI puro (cobre `http` e `websocket`, o que o BaseHTTPMiddleware nao faz)."""

    def __init__(self, app, settings: SecuritySettings, client_keys=None):
        self.app = app
        self.settings = settings
        self.client_keys = client_keys
        self._hits: Dict[Tuple[str, str], Deque[float]] = defaultdict(deque)

    # ── helpers ────────────────────────────────────────────────────────────────
    @staticmethod
    def client_keys_paths():
        from app.gateway.client_keys import CLIENT_PATHS
        return CLIENT_PATHS

    def _token_ok(self, supplied: Optional[str]) -> bool:
        return bool(supplied) and hmac.compare_digest(supplied.encode("utf-8"),
                                                      self.settings.token.encode("utf-8"))

    def _rate_limited(self, token: str, path: str) -> Optional[int]:
        rule = self.settings.rate_limits.get(path)
        if not rule:
            return None
        limit, window = rule
        now = time.monotonic()
        dq = self._hits[(token, path)]
        while dq and dq[0] <= now - window:
            dq.popleft()
        if len(dq) >= limit:
            return max(1, int(window - (now - dq[0])))
        dq.append(now)
        return None

    @staticmethod
    def _supplied_token(headers: Mapping[str, str], scope) -> Optional[str]:
        auth = headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            return auth[7:].strip()
        if headers.get("x-hera-token"):
            return headers["x-hera-token"].strip()
        if scope["type"] == "websocket":            # navegadores nao enviam cabecalhos no WebSocket
            qs = parse_qs(scope.get("query_string", b"").decode("latin-1"))
            return (qs.get("token") or [None])[0]
        return None

    async def _deny(self, scope, receive, send, status: int, detail: str, extra: Optional[dict] = None):
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008, "reason": detail})
            return
        resp = JSONResponse({"detail": detail}, status_code=status, headers=extra or {})
        await resp(scope, receive, self._with_security_headers(send))

    @staticmethod
    def _with_security_headers(send):
        async def wrapped(message):
            if message["type"] == "http.response.start":
                present = {k.lower() for k, _ in message.get("headers", [])}
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (k, v) for k, v in SECURITY_HEADERS.items() if k not in present]
            await send(message)
        return wrapped

    # ── ASGI ───────────────────────────────────────────────────────────────────
    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        host = headers.get("host", "")
        if _hostname(host) not in self.settings.allowed_hosts:
            return await self._deny(scope, receive, send, 400, "Host nao permitido")

        origin = headers.get("origin")
        if origin and origin.rstrip("/").lower() not in (f"http://{host.lower()}", f"https://{host.lower()}"):
            return await self._deny(scope, receive, send, 403, "Origin nao permitido")

        path = scope.get("path", "")
        supplied = None
        if path not in self.settings.public_paths:
            supplied = self._supplied_token(headers, scope)
            client_key = None
            if not self._token_ok(supplied):
                if self.client_keys is not None and scope["type"] == "http" and path in self.client_keys_paths():
                    client_key = self.client_keys.authenticate(supplied)
                if client_key is None:
                    return await self._deny(scope, receive, send, 401, "Token ausente ou invalido",
                                            {"WWW-Authenticate": "Bearer"})
                retry_key = self.client_keys.over_rpm(client_key)           # limite proprio da chave (req/min)
                if retry_key is not None:
                    return await self._deny(scope, receive, send, 429, "Limite de requisicoes da chave excedido",
                                            {"Retry-After": str(retry_key)})
                scope.setdefault("state", {})["client_key"] = client_key
            retry = self._rate_limited(client_key.id if client_key else (supplied or ""), path) if scope["type"] == "http" else None
            if retry is not None:
                return await self._deny(scope, receive, send, 429, "Limite de requisicoes excedido",
                                        {"Retry-After": str(retry)})

        if scope["type"] == "http":
            if scope.get("method") in ("POST", "PUT", "PATCH", "DELETE"):
                limit = self.settings.body_limits.get(path, self.settings.max_body_bytes)
                length = headers.get("content-length", "")
                if length.isdigit() and int(length) > limit:
                    return await self._deny(scope, receive, send, 413, f"Corpo maior que o limite de {limit} bytes")
                if not length:                                  # chunked: sem Content-Length, bufferiza até o limite
                    body = await self._read_limited(receive, limit)
                    if body is None:
                        return await self._deny(scope, receive, send, 413,
                                                f"Corpo maior que o limite de {limit} bytes")
                    receive = self._replay(body, receive)
            return await self.app(scope, receive, self._with_security_headers(send))
        return await self.app(scope, receive, send)

    @staticmethod
    async def _read_limited(receive, limit: int):
        """Lê o corpo inteiro até `limit` bytes; devolve None se passar (e para de ler)."""
        chunks, total = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return b"".join(chunks)
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > limit:
                return None
            chunks.append(chunk)
            if not message.get("more_body", False):
                return b"".join(chunks)

    @staticmethod
    def _replay(body: bytes, original_receive):
        sent = False

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await original_receive()
        return receive
