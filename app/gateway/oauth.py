"""OAuth 2.0 por provedor (você registra o SEU app OAuth; o Hera guarda e renova o token).

Fluxos: `authorization_code` (com PKCE S256 e `state` de uso único), `device_code` e `client_credentials`.
Config (`oauth:` no config.yaml; endpoints em HTTPS, validados contra SSRF; o SEGREDO do app fica em variável de ambiente):

    oauth:
      meu-provedor:
        grant: authorization_code            # | device_code | client_credentials
        client_id: abc123
        client_secret_env: MEU_CLIENT_SECRET # opcional (app público com PKCE não precisa)
        auth_url: https://idp.exemplo.com/oauth/authorize
        token_url: https://idp.exemplo.com/oauth/token
        device_url: https://idp.exemplo.com/oauth/device/code    # só para device_code
        scopes: [chat, offline_access]
        redirect_uri: http://127.0.0.1:8000/api/oauth/callback   # opcional (padrão: o endereço do próprio Hera)
        extra: {audience: https://api.exemplo.com}                # opcional: parâmetros extras (auth e token)

    providers:
      meu-provedor:
        oauth: meu-provedor                  # usa o token OAuth no lugar de `api_key_env`

NÃO há client IDs de terceiros embutidos: logar "como" o app oficial de outra ferramenta (Gemini CLI, Antigravity...)
é se passar por ela e o Hera não faz isso. Os tokens ficam no cofre local (DPAPI no Windows) e nunca saem pela API.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from app.gateway.key_manager import KeyLease, NoKeyAvailableError
from app.gateway.provider_policy import SSRFSecurityError, validate_provider_url

log = logging.getLogger("hera.gateway.oauth")

GRANTS = ("authorization_code", "device_code", "client_credentials")
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
# Login de um clique que termina numa CHAVE de API do proprio usuario (sem registrar app): o servico leva o usuario a
# autorizar e devolve um `code` que, trocado com o PKCE, vira uma chave. Fonte: documentacao oficial do OpenRouter
# (https://openrouter.ai/docs/guides/overview/auth/oauth); callbacks em localhost aceitam qualquer porta.
KEY_LOGINS: Dict[str, Dict[str, str]] = {
    "openrouter": {"label": "OpenRouter", "auth_url": "https://openrouter.ai/auth",
                   "exchange_url": "https://openrouter.ai/api/v1/auth/keys"},
}
PENDING_TTL_S = 600
MAX_PENDING = 20
EXPIRY_SKEW_S = 60
DEFAULT_EXPIRES_S = 3600
MAX_EXPIRES_S = 30 * 24 * 3600
MAX_TOKEN_LEN = 8192
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"


class OAuthError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class OAuthProvider:
    name: str
    grant: str
    client_id: str
    token_url: str
    auth_url: str = ""
    device_url: str = ""
    scopes: Tuple[str, ...] = ()
    client_secret_env: str = ""
    redirect_uri: str = ""
    extra: Tuple[Tuple[str, str], ...] = ()


def load_oauth_providers(cfg: Mapping[str, Any], url_validator: Callable[..., str] = validate_provider_url
                         ) -> Dict[str, OAuthProvider]:
    """Lê `oauth:` do config. Entrada inválida (nome, fluxo, URL fora do HTTPS/SSRF) é ignorada com aviso."""
    out: Dict[str, OAuthProvider] = {}
    for name, p in (cfg.get("oauth") or {}).items():
        try:
            if not isinstance(p, Mapping) or not NAME_RE.match(str(name)):
                raise ValueError("nome ou bloco inválido")
            grant = str(p.get("grant") or "authorization_code")
            if grant not in GRANTS:
                raise ValueError(f"fluxo desconhecido {grant!r}")
            client_id = str(p.get("client_id") or "").strip()
            if not client_id or len(client_id) > 256:
                raise ValueError("client_id obrigatório")
            urls = {"token_url": str(p.get("token_url") or "")}
            if grant == "authorization_code":
                urls["auth_url"] = str(p.get("auth_url") or "")
            if grant == "device_code":
                urls["device_url"] = str(p.get("device_url") or "")
            for key, url in urls.items():
                if not url.startswith("https://"):
                    raise ValueError(f"{key} precisa ser HTTPS")
                urls[key] = url_validator(url, is_local_profile=False)
            secret_env = str(p.get("client_secret_env") or "")
            if grant == "client_credentials" and not secret_env:
                raise ValueError("client_credentials exige client_secret_env")
            redirect = str(p.get("redirect_uri") or "")
            if redirect and not re.match(r"^https?://(127\.0\.0\.1|localhost|\[::1\])(:\d+)?/", redirect) \
                    and not redirect.startswith("https://"):
                raise ValueError("redirect_uri precisa ser loopback ou HTTPS")
            extra = tuple(sorted((str(k), str(v)) for k, v in (p.get("extra") or {}).items()
                                 if k not in ("client_id", "client_secret", "code", "state", "redirect_uri")))
            out[str(name)] = OAuthProvider(
                name=str(name), grant=grant, client_id=client_id, token_url=urls["token_url"],
                auth_url=urls.get("auth_url", ""), device_url=urls.get("device_url", ""),
                scopes=tuple(str(s) for s in (p.get("scopes") or [])), client_secret_env=secret_env,
                redirect_uri=redirect, extra=extra)
        except (ValueError, SSRFSecurityError) as exc:
            log.warning("[oauth] provedor %r ignorado: %s", name, exc)
    return out


def _https_or_empty(value: Any) -> str:
    """Links que o provedor manda para o usuario abrir: so HTTPS (nada de `javascript:` ou `data:`)."""
    text = str(value or "").strip()[:500]
    return text if text.startswith("https://") and not any(c.isspace() or ord(c) < 32 for c in text) else ""


def pkce_pair() -> Tuple[str, str]:
    verifier = secrets.token_urlsafe(64)                               # 86 caracteres (43 a 128 pela RFC 7636)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
    return verifier, challenge


@dataclass
class _Pending:
    provider: str
    verifier: str
    redirect_uri: str
    created: float
    kind: str = "oauth"          # "oauth" (tokens do app do usuario) | "key" (login que devolve uma chave de API)


@dataclass
class _Device:
    device_code: str
    interval: int
    expires_at: float
    last_poll: float = 0.0


class OAuthManager:
    def __init__(self, providers: Mapping[str, OAuthProvider], store: Any,
                 environ: Optional[Mapping[str, str]] = None, clock: Callable[[], float] = time.time):
        self.providers: Dict[str, OAuthProvider] = dict(providers)
        self.store, self.environ, self.clock = store, os.environ if environ is None else environ, clock
        self._pending: Dict[str, _Pending] = {}
        self.on_key: Optional[Callable[[str, str], None]] = None      # o servidor guarda a chave (cofre + provedor ligado)
        self._device: Dict[str, _Device] = {}
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    # ── armazenamento ──────────────────────────────────────────────────────────
    def _key(self, name: str) -> str:
        return f"oauth:{name}"

    def _tokens(self, name: str) -> Optional[Dict[str, Any]]:
        if name in self._cache:
            return self._cache[name]
        raw = self.store.get_raw(self._key(name))
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        if isinstance(data, dict) and isinstance(data.get("access_token"), str):
            self._cache[name] = data
            return data
        return None

    def _save(self, name: str, tokens: Dict[str, Any]) -> None:
        self._cache[name] = tokens
        self.store.set_raw(self._key(name), json.dumps(tokens))

    def disconnect(self, name: str) -> None:
        self._cache.pop(name, None)
        self._device.pop(name, None)
        self.store.delete(self._key(name))

    def invalidate(self, name: str) -> None:
        """O provedor recusou o token (401): descarta o `access_token` (o refresh, se houver, renova no próximo uso)."""
        tokens = self._tokens(name)
        if tokens:
            self._save(name, {**tokens, "access_token": tokens["access_token"], "expires_at": 0})

    def status(self, name: str) -> Dict[str, Any]:
        p, tokens = self.providers[name], self._tokens(name)
        now = self.clock()
        return {"name": name, "grant": p.grant, "connected": bool(tokens),
                "expires_in": max(0, int(tokens["expires_at"] - now)) if tokens else None,
                "has_refresh": bool(tokens and tokens.get("refresh_token")), "scope": (tokens or {}).get("scope") or " ".join(p.scopes),
                "pending": name in self._device or any(x.provider == name for x in self._pending.values())}

    def statuses(self) -> List[Dict[str, Any]]:
        return [self.status(n) for n in sorted(self.providers)]

    def _provider(self, name: str) -> OAuthProvider:
        if name not in self.providers:
            raise OAuthError(f"Provedor OAuth '{name}' não está configurado.", 404)
        return self.providers[name]

    def _secret(self, p: OAuthProvider) -> str:
        return (self.environ.get(p.client_secret_env) or "").strip() if p.client_secret_env else ""

    # ── troca com o servidor de autorização ────────────────────────────────────
    async def _token_request(self, p: OAuthProvider, form: Dict[str, str], client: httpx.AsyncClient) -> Dict[str, Any]:
        data = {**dict(p.extra), **form, "client_id": p.client_id}
        secret = self._secret(p)
        if secret:
            data["client_secret"] = secret
        try:
            resp = await client.post(p.token_url, data=data, headers={"Accept": "application/json"}, timeout=30.0)
        except httpx.TimeoutException:
            raise OAuthError(f"Timeout falando com {p.name}.", 504)
        except httpx.RequestError as exc:
            raise OAuthError(f"Erro de rede falando com {p.name}: {type(exc).__name__}.", 502)
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if resp.status_code != 200 or body.get("error"):
            err = str(body.get("error") or f"http_{resp.status_code}")[:64]
            desc = re.sub(r"[^\x20-\x7e]", "", str(body.get("error_description") or ""))[:200]
            exc = OAuthError(f"{p.name} recusou: {err}" + (f" ({desc})" if desc else ""), 400 if resp.status_code < 500 else 502)
            exc.code = err                                                      # type: ignore[attr-defined]
            raise exc
        return body

    def _tokens_from(self, p: OAuthProvider, body: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        access = body.get("access_token")
        if not isinstance(access, str) or not access or len(access) > MAX_TOKEN_LEN:
            raise OAuthError(f"{p.name} não devolveu um access_token válido.", 502)
        refresh = body.get("refresh_token")
        if not (isinstance(refresh, str) and refresh and len(refresh) <= MAX_TOKEN_LEN):
            refresh = (previous or {}).get("refresh_token")                    # nem todo refresh devolve um novo
        try:
            lifetime = int(body.get("expires_in") or DEFAULT_EXPIRES_S)
        except (TypeError, ValueError):
            lifetime = DEFAULT_EXPIRES_S
        lifetime = min(max(lifetime, 1), MAX_EXPIRES_S)
        out = {"access_token": access, "expires_at": self.clock() + lifetime,
               "token_type": str(body.get("token_type") or "Bearer")[:32], "scope": str(body.get("scope") or "")[:500]}
        if refresh:
            out["refresh_token"] = refresh
        return out

    # ── conectar ───────────────────────────────────────────────────────────────
    def _gc_pending(self) -> None:
        now = self.clock()
        for state in [s for s, x in self._pending.items() if now - x.created > PENDING_TTL_S]:
            del self._pending[state]

    async def start(self, name: str, redirect_uri: str, client: httpx.AsyncClient) -> Dict[str, Any]:
        p = self._provider(name)
        if p.grant == "authorization_code":
            self._gc_pending()
            if len(self._pending) >= MAX_PENDING:
                raise OAuthError("Muitas conexões OAuth pendentes; tente de novo em alguns minutos.", 429)
            redirect = p.redirect_uri or redirect_uri
            verifier, challenge = pkce_pair()
            state = secrets.token_urlsafe(32)
            self._pending[state] = _Pending(name, verifier, redirect, self.clock())
            query = {"response_type": "code", "client_id": p.client_id, "redirect_uri": redirect, "state": state,
                     "code_challenge": challenge, "code_challenge_method": "S256", **dict(p.extra)}
            if p.scopes:
                query["scope"] = " ".join(p.scopes)
            parts = urlsplit(p.auth_url)
            merged = urlencode([*parse_qsl(parts.query), *query.items()])
            return {"grant": p.grant, "authorize_url": urlunsplit(parts._replace(query=merged)),
                    "redirect_uri": redirect, "expires_in": PENDING_TTL_S}
        if p.grant == "device_code":
            try:
                resp = await client.post(p.device_url, data={"client_id": p.client_id, "scope": " ".join(p.scopes), **dict(p.extra)},
                                         headers={"Accept": "application/json"}, timeout=30.0)
                body = resp.json()
            except (httpx.HTTPError, ValueError):
                raise OAuthError(f"Não foi possível iniciar o login por código em {p.name}.", 502)
            if resp.status_code != 200 or not isinstance(body, dict) or not body.get("device_code"):
                raise OAuthError(f"{p.name} recusou iniciar o login por código.", 400)
            interval = min(max(int(body.get("interval") or 5), 1), 60)
            expires = min(int(body.get("expires_in") or 900), 1800)
            self._device[name] = _Device(str(body["device_code"]), interval, self.clock() + expires)
            return {"grant": p.grant, "user_code": str(body.get("user_code") or "")[:64],
                    "verification_uri": _https_or_empty(body.get("verification_uri") or body.get("verification_url")),
                    "verification_uri_complete": _https_or_empty(body.get("verification_uri_complete")) or None,
                    "interval": interval, "expires_in": expires}
        await self.access_token(name, client, force=True)                       # client_credentials: conecta na hora
        return {"grant": p.grant, "connected": True}

    def start_key_login(self, name: str, redirect_uri: str) -> Dict[str, Any]:
        """Inicia o login de um clique que devolve uma chave (PKCE S256 + `state` de uso unico). Sem rede aqui."""
        spec = KEY_LOGINS.get(name)
        if spec is None:
            raise OAuthError(f"'{name}' nao tem login por conta.", 404)
        self._gc_pending()
        if len(self._pending) >= MAX_PENDING:
            raise OAuthError("Muitas conexoes pendentes; tente de novo em alguns minutos.", 429)
        parts = urlsplit(redirect_uri)
        if parts.hostname in ("127.0.0.1", "::1"):                      # a doc do servico fala em `localhost`
            redirect_uri = urlunsplit(parts._replace(netloc="localhost" + (f":{parts.port}" if parts.port else "")))
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(32)
        self._pending[state] = _Pending(name, verifier, redirect_uri, self.clock(), kind="key")
        query = urlencode({"callback_url": redirect_uri, "code_challenge": challenge, "code_challenge_method": "S256",
                           "state": state, "key_label": "Hera"})
        return {"grant": "key_login", "authorize_url": f"{spec['auth_url']}?{query}", "redirect_uri": redirect_uri,
                "expires_in": PENDING_TTL_S}

    async def _finish_key_login(self, pending: "_Pending", code: str, client: httpx.AsyncClient) -> str:
        spec = KEY_LOGINS[pending.provider]
        try:
            resp = await client.post(spec["exchange_url"], json={"code": code, "code_verifier": pending.verifier,
                                                                 "code_challenge_method": "S256"},
                                     headers={"Accept": "application/json"}, timeout=30.0)
            body = resp.json()
        except httpx.TimeoutException:
            raise OAuthError(f"Timeout falando com {spec['label']}.", 504)
        except (httpx.HTTPError, ValueError):
            raise OAuthError(f"Nao consegui concluir o login em {spec['label']}.", 502)
        key = body.get("key") if isinstance(body, dict) else None
        if resp.status_code != 200 or not isinstance(key, str) or not 8 <= len(key) <= 512 or any(c.isspace() for c in key):
            raise OAuthError(f"{spec['label']} recusou o login (codigo invalido ou expirado). Tente de novo.", 400)
        if self.on_key is None:
            raise OAuthError("Servidor sem destino para a chave.", 500)
        self.on_key(pending.provider, key)
        return pending.provider

    async def callback(self, state: str, code: str, client: httpx.AsyncClient) -> str:
        """Fim do `authorization_code`: o `state` é de uso único e só existe se a conexão foi iniciada por quem tem o token."""
        self._gc_pending()
        pending = self._pending.pop(state or "", None)
        if pending is None:
            raise OAuthError("Conexão OAuth desconhecida ou expirada. Comece de novo pelo painel.", 400)
        if not code or len(code) > 2048:
            raise OAuthError("Código de autorização ausente.", 400)
        if pending.kind == "key":
            return await self._finish_key_login(pending, code, client)
        p = self._provider(pending.provider)
        body = await self._token_request(p, {"grant_type": "authorization_code", "code": code,
                                             "redirect_uri": pending.redirect_uri, "code_verifier": pending.verifier}, client)
        self._save(p.name, self._tokens_from(p, body))
        return p.name

    async def poll_device(self, name: str, client: httpx.AsyncClient) -> Dict[str, Any]:
        p = self._provider(name)
        dev = self._device.get(name)
        if dev is None:
            raise OAuthError("Nenhum login por código em andamento.", 400)
        now = self.clock()
        if now >= dev.expires_at:
            self._device.pop(name, None)
            return {"status": "expired"}
        if now - dev.last_poll < dev.interval:
            return {"status": "pending", "retry_in": int(dev.interval - (now - dev.last_poll)) + 1}
        dev.last_poll = now
        try:
            body = await self._token_request(p, {"grant_type": DEVICE_GRANT, "device_code": dev.device_code}, client)
        except OAuthError as exc:
            code = getattr(exc, "code", "")
            if code == "authorization_pending":
                return {"status": "pending", "retry_in": dev.interval}
            if code == "slow_down":
                dev.interval = min(dev.interval + 5, 60)
                return {"status": "pending", "retry_in": dev.interval}
            if code in ("expired_token", "access_denied"):
                self._device.pop(name, None)
                return {"status": "expired" if code == "expired_token" else "denied"}
            raise
        self._device.pop(name, None)
        self._save(name, self._tokens_from(p, body))
        return {"status": "connected"}

    # ── uso ────────────────────────────────────────────────────────────────────
    def cached_token(self, name: str) -> Optional[str]:
        """Token válido AGORA (sem rede), ou None. Usado pelo gerenciador de chaves do adaptador."""
        tokens = self._tokens(name)
        if tokens and tokens.get("expires_at", 0) - EXPIRY_SKEW_S > self.clock():
            return tokens["access_token"]
        return None

    async def access_token(self, name: str, client: httpx.AsyncClient, force: bool = False) -> str:
        """Token válido; renova (refresh_token ou client_credentials) se estiver vencendo. Uma renovação por vez."""
        p = self._provider(name)
        if not force and (tok := self.cached_token(name)):
            return tok
        lock = self._locks.setdefault(name, asyncio.Lock())
        async with lock:
            if not force and (tok := self.cached_token(name)):                  # outro pedido já renovou
                return tok
            tokens = self._tokens(name)
            if p.grant == "client_credentials":
                form = {"grant_type": "client_credentials"}
                if p.scopes:
                    form["scope"] = " ".join(p.scopes)
            elif tokens and tokens.get("refresh_token"):
                form = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]}
            else:
                raise OAuthError(f"'{name}' não está conectado (faça o login OAuth no painel).", 401)
            try:
                body = await self._token_request(p, form, client)
            except OAuthError as exc:
                if getattr(exc, "code", "") in ("invalid_grant", "invalid_token", "unauthorized_client"):
                    self.disconnect(name)                                      # refresh revogado: precisa logar de novo
                raise
            self._save(name, self._tokens_from(p, body, tokens))
            return self._tokens(name)["access_token"]                          # type: ignore[index]


class OAuthKeyManager:
    """Faz um token OAuth passar por `KeyManager` para o `CloudOpenAIGatewayAdapter` (acquire / report_* / prepare)."""

    def __init__(self, manager: OAuthManager, provider: str):
        self.manager, self.provider, self.label = manager, provider, f"oauth:{provider}"
        self._cooldown_until = 0.0

    def __len__(self) -> int:
        return 1

    async def prepare(self, client: httpx.AsyncClient) -> None:
        """Antes de cada envio: renova o token se estiver vencendo (falha vira 'sem chave' no `acquire`)."""
        try:
            await self.manager.access_token(self.provider, client)
        except OAuthError as exc:
            log.warning("[oauth] %s: %s", self.provider, exc)

    def acquire(self) -> KeyLease:
        now = self.manager.clock()
        if self._cooldown_until > now:
            raise NoKeyAvailableError(self._cooldown_until - now)
        token = self.manager.cached_token(self.provider)
        if not token:
            raise NoKeyAvailableError(None)
        return KeyLease(self.label, token)

    def report_rate_limited(self, key_id: str, retry_after: Optional[float]) -> None:
        self._cooldown_until = self.manager.clock() + min(max(retry_after if retry_after is not None else 60.0, 1.0), 3600.0)

    def report_auth_failure(self, key_id: str) -> None:
        self.manager.invalidate(self.provider)

    def status(self) -> List[dict]:
        s = self.manager.status(self.provider)
        return [{"id": self.label, "state": "ok" if s["connected"] else "disabled",
                 "cooldown_remaining_s": max(0, int(self._cooldown_until - self.manager.clock())), "used_today": 0}]
