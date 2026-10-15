"""Busca na web pelo Roteador (`POST /v1/search`): Brave, Tavily e Exa, com saída normalizada.

Cada provedor tem API própria (não existe padrão OpenAI para busca). Aqui cada um vira um adaptador que devolve
`[{title, url, snippet, ...}]`; o roteador tenta os ligados em ordem (com o mesmo rodízio/espera de chaves do chat).
Config (`search:` no config.yaml; as CHAVES ficam em variável de ambiente ou no cofre, nunca no arquivo):

    search:
      brave:  {enabled: true, api_key_env: BRAVE_API_KEY}
      tavily: {enabled: true, api_key_env: TAVILY_API_KEY}
      exa:    {enabled: false, api_key_env: EXA_API_KEY}
      order: [brave, tavily, exa]            # opcional

SearXNG local não entra: a política anti-SSRF do Hera só libera loopback na porta do Ollama.
Os formatos de requisição/resposta abaixo seguem a documentação pública de cada serviço, mas NÃO foram testados
contra os serviços reais (só contra respostas simuladas): confira com uma busca de teste após ligar o provedor.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional

import httpx

from app.gateway.key_manager import KeyManager, NoKeyAvailableError, load_keys_from_env, parse_retry_after
from app.gateway.provider_policy import SSRFSecurityError, validate_provider_url
from app.gateway.providers import GatewayProviderError

log = logging.getLogger("hera.gateway.search")

MAX_RESULTS = 20
MAX_QUERY_CHARS = 2000
SNIPPET_CHARS = 1000


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    published: Optional[str] = None
    score: Optional[float] = None

    def public(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"title": self.title, "url": self.url, "snippet": self.snippet}
        if self.published:
            out["published"] = self.published
        if self.score is not None:
            out["score"] = self.score
        return out


def _clip(text: Any) -> str:
    return str(text or "").strip()[:SNIPPET_CHARS]


class SearchProvider:
    name = "base"
    base_url = ""
    tier = "cloud_cheap"

    def __init__(self, key_manager: KeyManager, timeout: float = 20.0):
        self.base_url = validate_provider_url(self.base_url, is_local_profile=False)
        self.key_manager, self.timeout = key_manager, timeout

    # subclasses: monta a requisição e normaliza a resposta
    def build(self, query: str, count: int, secret: str) -> Dict[str, Any]:
        raise NotImplementedError

    def parse(self, data: Dict[str, Any]) -> List[SearchResult]:
        raise NotImplementedError

    async def search(self, query: str, count: int, client: httpx.AsyncClient) -> List[SearchResult]:
        last: Optional[GatewayProviderError] = None
        for _ in range(max(1, len(self.key_manager))):
            try:
                lease = self.key_manager.acquire()
            except NoKeyAvailableError as exc:
                if last is not None:
                    raise last
                wait = f" (tente em {int(exc.retry_after)}s)" if exc.retry_after else ""
                raise GatewayProviderError(f"Todas as chaves de busca {self.name} estão em espera ou desativadas{wait}",
                                           status_code=429 if exc.retry_after else 503, retryable=True)
            try:
                resp = await client.request(timeout=self.timeout, **self.build(query, count, lease.secret))
            except httpx.TimeoutException as e:
                raise GatewayProviderError(f"Timeout na busca {self.name}: {type(e).__name__}", status_code=504, retryable=True)
            except httpx.RequestError as e:
                raise GatewayProviderError(f"Erro de rede na busca {self.name}: {type(e).__name__}", status_code=502, retryable=True)
            if resp.status_code == 200:
                try:
                    return self.parse(resp.json())[:count]
                except (ValueError, AttributeError, TypeError):
                    raise GatewayProviderError(f"Resposta inesperada da busca {self.name}", status_code=502, retryable=True)
            if resp.status_code == 429:
                self.key_manager.report_rate_limited(lease.key_id, parse_retry_after(resp.headers.get("retry-after")))
                last = GatewayProviderError(f"Busca {self.name} retornou HTTP 429 (limite de uso)", status_code=429, retryable=True)
                continue
            if resp.status_code in (401, 403):
                self.key_manager.report_auth_failure(lease.key_id)
                last = GatewayProviderError(f"Busca {self.name} recusou a chave {lease.key_id} (HTTP {resp.status_code}); chave desativada",
                                            status_code=resp.status_code, retryable=True)
                continue
            raise GatewayProviderError(f"Busca {self.name} retornou HTTP {resp.status_code}", status_code=resp.status_code,
                                       retryable=resp.status_code in (500, 502, 503, 504))
        raise last or GatewayProviderError(f"Falha desconhecida na busca {self.name}", status_code=502, retryable=True)


class BraveSearch(SearchProvider):
    name, base_url = "brave", "https://api.search.brave.com/res/v1/web/search"

    def build(self, query, count, secret):
        return {"method": "GET", "url": self.base_url, "params": {"q": query, "count": count},
                "headers": {"X-Subscription-Token": secret, "Accept": "application/json"}}

    def parse(self, data):
        return [SearchResult(_clip(r.get("title")), str(r.get("url") or ""), _clip(r.get("description")),
                             r.get("age") or r.get("page_age"))
                for r in ((data.get("web") or {}).get("results") or []) if r.get("url")]


class TavilySearch(SearchProvider):
    name, base_url = "tavily", "https://api.tavily.com/search"

    def build(self, query, count, secret):
        return {"method": "POST", "url": self.base_url, "json": {"query": query, "max_results": count},
                "headers": {"Authorization": f"Bearer {secret}"}}

    def parse(self, data):
        return [SearchResult(_clip(r.get("title")), str(r.get("url") or ""), _clip(r.get("content")),
                             r.get("published_date"), r.get("score") if isinstance(r.get("score"), (int, float)) else None)
                for r in (data.get("results") or []) if r.get("url")]


class ExaSearch(SearchProvider):
    name, base_url = "exa", "https://api.exa.ai/search"

    def build(self, query, count, secret):
        return {"method": "POST", "url": self.base_url,
                "json": {"query": query, "numResults": count, "contents": {"text": {"maxCharacters": SNIPPET_CHARS}}},
                "headers": {"x-api-key": secret}}

    def parse(self, data):
        return [SearchResult(_clip(r.get("title")), str(r.get("url") or ""), _clip(r.get("text")), r.get("publishedDate"),
                             r.get("score") if isinstance(r.get("score"), (int, float)) else None)
                for r in (data.get("results") or []) if r.get("url")]


PROVIDER_CLASSES = {"brave": BraveSearch, "tavily": TavilySearch, "exa": ExaSearch}


def build_search_providers(cfg: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None,
                           secrets: Optional[Any] = None) -> List[SearchProvider]:
    """Monta os provedores de busca ligados e com chave. Sem chave ou URL reprovada: ignorado com aviso."""
    env = os.environ if environ is None else environ
    section = cfg.get("search") or {}
    order = [n for n in (section.get("order") or list(PROVIDER_CLASSES)) if n in PROVIDER_CLASSES]
    order += [n for n in PROVIDER_CLASSES if n not in order]
    out: List[SearchProvider] = []
    for name in order:
        p = section.get(name)
        if not isinstance(p, Mapping) or not p.get("enabled", False):
            continue
        env_names = [n.strip() for n in str(p.get("api_key_env") or "").split(",") if n.strip()]
        keys = load_keys_from_env(env_names, env)
        if secrets is not None:
            keys = list(dict.fromkeys([*keys, *secrets.get(f"search:{name}")]))
        if not keys:
            log.warning("[busca] provedor %r ignorado: nenhuma chave (variaveis %s ou cofre)", name, env_names or "-")
            continue
        try:
            out.append(PROVIDER_CLASSES[name](KeyManager(keys, label=env_names[0] if env_names else name),
                                              timeout=float(p.get("timeout", 20.0))))
        except (SSRFSecurityError, ValueError) as exc:
            log.warning("[busca] provedor %r ignorado: %s", name, exc)
    return out


@dataclass
class SearchRouter:
    providers: List[SearchProvider] = field(default_factory=list)

    def names(self) -> List[str]:
        return [p.name for p in self.providers]

    async def search(self, query: str, count: int, client: httpx.AsyncClient,
                     provider: Optional[str] = None) -> Dict[str, Any]:
        candidates = [p for p in self.providers if provider in (None, "", p.name)]
        if not candidates:
            raise GatewayProviderError(
                f"Nenhum provedor de busca ligado{f' com o nome {provider!r}' if provider else ''}.",
                status_code=404 if provider else 503, retryable=False)
        last: Optional[GatewayProviderError] = None
        for i, p in enumerate(candidates):
            started = time.monotonic()
            try:
                results = await p.search(query, count, client)
            except GatewayProviderError as e:
                last = e
                if not e.retryable or i == len(candidates) - 1:
                    raise
                continue
            return {"object": "search.results", "provider": p.name, "query": query,
                    "latency_ms": int((time.monotonic() - started) * 1000), "results": [r.public() for r in results]}
        raise last or GatewayProviderError("Falha desconhecida na busca.", status_code=502)
