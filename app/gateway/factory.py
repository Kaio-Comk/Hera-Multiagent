"""Monta os adaptadores do gateway a partir do config.yaml + variáveis de ambiente.

Config (`providers:` no config.yaml; as CHAVES nunca ficam no arquivo):

    providers:
      deepseek:
        enabled: true
        tier: cloud_cheap                 # cloud_premium | cloud_cheap | cloud_free
        base_url: https://api.deepseek.com/v1
        api_key_env: DEEPSEEK_API_KEY     # nome da variável; pode ter várias chaves separadas por vírgula
        # oauth: meu-provedor             # OU: usa o token OAuth do bloco `oauth:` (ver app/gateway/oauth.py)
        default_model: deepseek-chat
        models: [deepseek-chat, deepseek-reasoner]   # opcional: modelos que este provedor atende
        timeout: 60
        daily_request_limit: 1000         # opcional, por chave
        modalities: [embeddings, images, speech, transcription]   # opcional: além do chat (padrão: todas)

A chave vem das variáveis de ambiente (`api_key_env`) E/OU do cofre local (colada no painel).
Um provedor ligado sem chave, ou com URL reprovada pela política anti-SSRF, é IGNORADO com aviso
(o Hera continua subindo só com o que está válido).
"""
from __future__ import annotations

import logging
import math
import os
from typing import Any, Dict, List, Mapping, Optional

from app.gateway.key_manager import KeyManager, load_keys_from_env
from app.gateway.oauth import OAuthKeyManager
from app.gateway.provider_policy import SSRFSecurityError, sanitize_extra_headers
from app.gateway.providers import BaseGatewayAdapter, CloudOpenAIGatewayAdapter, OllamaGatewayAdapter
from app.gateway.tier_router import TierRouter

log = logging.getLogger("hera.gateway.factory")

CLOUD_TIERS = ("cloud_premium", "cloud_cheap", "cloud_free")


def build_gateway_adapters(cfg: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None,
                           secrets: Optional[Any] = None, oauth: Optional[Any] = None) -> List[BaseGatewayAdapter]:
    env = os.environ if environ is None else environ
    ollama_cfg = (cfg.get("engines") or {}).get("ollama") or {}
    adapters: List[BaseGatewayAdapter] = [OllamaGatewayAdapter(
        base_url=ollama_cfg.get("base_url", "http://localhost:11434"),
        default_model=ollama_cfg.get("default_model", "qwen2.5-coder:1.5b"),
    )]

    for name, p in (cfg.get("providers") or {}).items():
        if not isinstance(p, Mapping) or not p.get("enabled", False):
            continue
        tier = p.get("tier", "cloud_cheap")
        if tier not in CLOUD_TIERS:
            log.warning("[gateway] provedor %r ignorado: tier invalido %r", name, tier)
            continue
        env_names = [n.strip() for n in str(p.get("api_key_env") or "").split(",") if n.strip()]
        oauth_name = str(p.get("oauth") or "")
        if oauth_name:                                   # token OAuth (renovado sozinho) no lugar da chave de API
            if oauth is None or oauth_name not in oauth.providers:
                log.warning("[gateway] provedor %r ignorado: OAuth %r nao esta configurado", name, oauth_name)
                continue
            manager = OAuthKeyManager(oauth, oauth_name)
        else:
            keys = load_keys_from_env(env_names, env)
            if secrets is not None:                      # chaves coladas no painel (cofre protegido pela DPAPI)
                keys = list(dict.fromkeys([*keys, *secrets.get(str(name))]))
            if not keys:
                log.warning("[gateway] provedor %r ignorado: nenhuma chave (variaveis %s ou cofre)", name, env_names or "-")
                continue
            limit = p.get("daily_request_limit")
            manager = KeyManager(keys, label=(env_names[0] if env_names else name),
                                 daily_limit=int(limit) if isinstance(limit, int) and limit > 0 else None)
        try:
            adapters.append(CloudOpenAIGatewayAdapter(
                name=str(name),
                base_url=str(p.get("base_url", "")),
                key_manager=manager,
                default_model=str(p.get("default_model", "default")),
                tier=tier,
                timeout=float(p.get("timeout", 60.0)),
                models=[str(m) for m in (p.get("models") or [])],
                stream_usage=bool(p.get("stream_usage", False)),
                modalities=[str(m) for m in p["modalities"]] if isinstance(p.get("modalities"), list) else None,
                extra_headers=sanitize_extra_headers(p.get("headers")),
            ))
        except (SSRFSecurityError, ValueError) as exc:
            log.warning("[gateway] provedor %r ignorado: %s", name, exc)
    return adapters


def parse_daily_budget(raw: Any) -> Optional[float]:
    """Lê gateway.daily_budget_usd (aceita número ou None; valor negativo/inválido vira None com aviso no log)."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        log.warning("[gateway] daily_budget_usd invalido (booleano): %r", raw)
        return None
    try:
        val = float(raw)
        if val < 0 or math.isnan(val) or math.isinf(val):
            log.warning("[gateway] daily_budget_usd invalido (negativo ou nao-finito): %r", raw)
            return None
        return val
    except (ValueError, TypeError):
        log.warning("[gateway] daily_budget_usd invalido: %r", raw)
        return None


def parse_sticky_minutes(raw: Any) -> int:
    """Lê gateway.sticky_minutes (padrão 15; 0 desliga)."""
    if raw is None:
        return 15
    if isinstance(raw, bool):
        log.warning("[gateway] sticky_minutes invalido (booleano): %r, usando padrao 15", raw)
        return 15
    try:
        val = int(raw)
        if val < 0:
            log.warning("[gateway] sticky_minutes negativo (%r), usando padrao 15", raw)
            return 15
        return val
    except (ValueError, TypeError):
        log.warning("[gateway] sticky_minutes invalido (%r), usando padrao 15", raw)
        return 15


def build_tier_router(
    cfg: Mapping[str, Any],
    adapters: Optional[List[BaseGatewayAdapter]] = None,
    environ: Optional[Mapping[str, str]] = None,
    secrets: Optional[Any] = None,
    oauth: Optional[Any] = None,
    usage: Optional[Any] = None,
    clock: Optional[Any] = None,
    **kwargs: Any,
) -> TierRouter:
    """Monta o TierRouter a partir do config.yaml, seção `gateway`."""
    if adapters is None:
        adapters = build_gateway_adapters(cfg, environ=environ, secrets=secrets, oauth=oauth)

    gw = cfg.get("gateway") if isinstance(cfg.get("gateway"), Mapping) else {}
    if not gw and ("daily_budget_usd" in cfg or "sticky_minutes" in cfg or "tier_order" in cfg):
        gw = cfg

    raw_budget = gw.get("daily_budget_usd") if "daily_budget_usd" in gw else cfg.get("gateway.daily_budget_usd")
    daily_budget = parse_daily_budget(raw_budget)

    raw_sticky = gw.get("sticky_minutes") if "sticky_minutes" in gw else cfg.get("gateway.sticky_minutes")
    sticky_minutes = parse_sticky_minutes(raw_sticky)

    tier_order = gw.get("default_tier_order") or gw.get("tier_order")

    router_kwargs: Dict[str, Any] = {
        "adapters": adapters,
        "daily_budget_usd": daily_budget,
        "sticky_minutes": sticky_minutes,
    }
    if tier_order is not None:
        router_kwargs["tier_order"] = list(tier_order)
    if clock is not None:
        router_kwargs["clock"] = clock
    if "total_timeout_seconds" in gw:
        router_kwargs["total_timeout_seconds"] = float(gw["total_timeout_seconds"])
    elif "total_timeout" in gw:
        router_kwargs["total_timeout_seconds"] = float(gw["total_timeout"])
    if "max_concurrent_requests" in gw:
        router_kwargs["max_concurrent_requests"] = int(gw["max_concurrent_requests"])

    router_kwargs.update(kwargs)
    router = TierRouter(**router_kwargs)
    if usage is not None:
        router.usage = usage
    return router


build_router = build_tier_router
