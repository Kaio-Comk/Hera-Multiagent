"""Catálogo de provedores compatíveis com a API OpenAI que o Roteador do Hera sabe configurar.

É só um CATÁLOGO: o Hera nunca guarda a chave. Você cria a chave no site do provedor e a coloca numa variável
de ambiente (`key_env`); o provedor só entra em uso quando você o liga (com o código humano) e a variável existe.
Os modelos mudam com frequência: confira `models` no painel do provedor e ajuste.
"""
from __future__ import annotations

from typing import Any, Dict, List

PROVIDER_PRESETS: List[Dict[str, Any]] = [
    {"id": "groq", "label": "Groq", "tier": "cloud_free", "base_url": "https://api.groq.com/openai/v1",
     "key_env": "GROQ_API_KEY", "default_model": "llama-3.3-70b-versatile",
     "models": ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
     "signup": "https://console.groq.com/keys", "note": "Plano gratuito com limite por minuto/dia; muito rápido."},
    {"id": "openrouter", "label": "OpenRouter", "tier": "cloud_cheap", "base_url": "https://openrouter.ai/api/v1",
     "key_env": "OPENROUTER_API_KEY", "default_model": "deepseek/deepseek-chat-v3.1:free",
     "models": ["deepseek/deepseek-chat-v3.1:free"],
     "signup": "https://openrouter.ai/keys", "note": "Um endereço para centenas de modelos; há modelos ':free'."},
    {"id": "deepseek", "label": "DeepSeek", "tier": "cloud_cheap", "base_url": "https://api.deepseek.com/v1",
     "key_env": "DEEPSEEK_API_KEY", "default_model": "deepseek-chat", "models": ["deepseek-chat", "deepseek-reasoner"],
     "signup": "https://platform.deepseek.com/api_keys", "note": "Barato; bom para código."},
    {"id": "gemini", "label": "Google Gemini", "tier": "cloud_free",
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
     "key_env": "GEMINI_API_KEY", "default_model": "gemini-2.5-flash", "models": ["gemini-2.5-flash", "gemini-2.5-pro"],
     "signup": "https://aistudio.google.com/apikey", "note": "Camada gratuita com limites; compatível com OpenAI."},
    {"id": "mistral", "label": "Mistral", "tier": "cloud_cheap", "base_url": "https://api.mistral.ai/v1",
     "key_env": "MISTRAL_API_KEY", "default_model": "mistral-small-latest", "models": ["mistral-small-latest", "codestral-latest"],
     "signup": "https://console.mistral.ai/api-keys", "note": ""},
    {"id": "cerebras", "label": "Cerebras", "tier": "cloud_free", "base_url": "https://api.cerebras.ai/v1",
     "key_env": "CEREBRAS_API_KEY", "default_model": "llama3.1-8b", "models": ["llama3.1-8b"],
     "signup": "https://cloud.cerebras.ai/", "note": "Camada gratuita; respostas muito rápidas."},
    {"id": "together", "label": "Together AI", "tier": "cloud_cheap", "base_url": "https://api.together.xyz/v1",
     "key_env": "TOGETHER_API_KEY", "default_model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
     "models": ["meta-llama/Llama-3.3-70B-Instruct-Turbo"], "signup": "https://api.together.ai/settings/api-keys", "note": ""},
    {"id": "nvidia", "label": "NVIDIA NIM", "tier": "cloud_free", "base_url": "https://integrate.api.nvidia.com/v1",
     "key_env": "NVIDIA_API_KEY", "default_model": "meta/llama-3.3-70b-instruct", "models": ["meta/llama-3.3-70b-instruct"],
     "signup": "https://build.nvidia.com/", "note": "Créditos gratuitos para testes."},
    {"id": "openai", "label": "OpenAI", "tier": "cloud_premium", "base_url": "https://api.openai.com/v1",
     "key_env": "OPENAI_API_KEY", "default_model": "gpt-4o-mini", "models": ["gpt-4o-mini", "gpt-4o"],
     "signup": "https://platform.openai.com/api-keys", "note": "Pago por uso."},
    {"id": "xai", "label": "xAI (Grok)", "tier": "cloud_premium", "base_url": "https://api.x.ai/v1",
     "key_env": "XAI_API_KEY", "default_model": "grok-3-mini", "models": ["grok-3-mini"],
     "signup": "https://console.x.ai/", "note": "Pago por uso."},
]


def get_preset(preset_id: str) -> Dict[str, Any]:
    for p in PROVIDER_PRESETS:
        if p["id"] == preset_id:
            return dict(p)
    raise KeyError(preset_id)
