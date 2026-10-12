"""Monta os adaptadores do gateway a partir do config.yaml + variáveis de ambiente.

Config (`providers:` no config.yaml; as CHAVES nunca ficam no arquivo):

    providers:
      deepseek:
        enabled: true
        tier: cloud_cheap                 # cloud_premium | cloud_cheap | cloud_free
        base_url: https://api.deepseek.com/v1
        api_key_env: DEEPSEEK_API_KEY     # nome da variável; pode ter várias chaves separadas por vírgula
        default_model: deepseek-chat
        models: [deepseek-chat, deepseek-reasoner]   # opcional: modelos que este provedor atende
        timeout: 60
        daily_request_limit: 1000         # opcional, por chave

Um provedor ligado sem chave, ou com URL reprovada pela política anti-SSRF, é IGNORADO com aviso
(o Hera continua subindo só com o que está válido).
"""
from __future__ import annotations

import logging
import os
from typing import Any, List, Mapping, Optional

from app.gateway.key_manager import KeyManager, load_keys_from_env
from app.gateway.provider_policy import SSRFSecurityError
from app.gateway.providers import BaseGatewayAdapter, CloudOpenAIGatewayAdapter, OllamaGatewayAdapter

log = logging.getLogger("hera.gateway.factory")

CLOUD_TIERS = ("cloud_premium", "cloud_cheap", "cloud_free")


def build_gateway_adapters(cfg: Mapping[str, Any], environ: Optional[Mapping[str, str]] = None
                           ) -> List[BaseGatewayAdapter]:
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
        keys = load_keys_from_env(env_names, env)
        if not keys:
            log.warning("[gateway] provedor %r ignorado: nenhuma chave nas variaveis %s", name, env_names or "(api_key_env vazio)")
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
            ))
        except (SSRFSecurityError, ValueError) as exc:
            log.warning("[gateway] provedor %r ignorado: %s", name, exc)
    return adapters
