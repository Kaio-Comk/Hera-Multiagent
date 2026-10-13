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
    {"id": "fireworks", "label": "Fireworks AI", "tier": "cloud_cheap", "base_url": "https://api.fireworks.ai/inference/v1",
     "key_env": "FIREWORKS_API_KEY", "default_model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
     "models": ["accounts/fireworks/models/llama-v3p3-70b-instruct"], "signup": "https://fireworks.ai/account/api-keys", "note": "Inferência rápida de modelos abertos."},
    {"id": "sambanova", "label": "SambaNova", "tier": "cloud_free", "base_url": "https://api.sambanova.ai/v1",
     "key_env": "SAMBANOVA_API_KEY", "default_model": "Meta-Llama-3.3-70B-Instruct", "models": ["Meta-Llama-3.3-70B-Instruct"],
     "signup": "https://cloud.sambanova.ai/apis", "note": "Camada gratuita com limites."},
    {"id": "hyperbolic", "label": "Hyperbolic", "tier": "cloud_cheap", "base_url": "https://api.hyperbolic.xyz/v1",
     "key_env": "HYPERBOLIC_API_KEY", "default_model": "meta-llama/Llama-3.3-70B-Instruct", "models": ["meta-llama/Llama-3.3-70B-Instruct"],
     "signup": "https://app.hyperbolic.xyz/settings", "note": ""},
    {"id": "deepinfra", "label": "DeepInfra", "tier": "cloud_cheap", "base_url": "https://api.deepinfra.com/v1/openai",
     "key_env": "DEEPINFRA_API_KEY", "default_model": "meta-llama/Llama-3.3-70B-Instruct", "models": ["meta-llama/Llama-3.3-70B-Instruct"],
     "signup": "https://deepinfra.com/dash/api_keys", "note": "Preço baixo por token."},
    {"id": "novita", "label": "Novita AI", "tier": "cloud_cheap", "base_url": "https://api.novita.ai/v3/openai",
     "key_env": "NOVITA_API_KEY", "default_model": "meta-llama/llama-3.3-70b-instruct", "models": ["meta-llama/llama-3.3-70b-instruct"],
     "signup": "https://novita.ai/settings/key-management", "note": ""},
    {"id": "siliconflow", "label": "SiliconFlow", "tier": "cloud_free", "base_url": "https://api.siliconflow.com/v1",
     "key_env": "SILICONFLOW_API_KEY", "default_model": "Qwen/Qwen2.5-72B-Instruct", "models": ["Qwen/Qwen2.5-72B-Instruct"],
     "signup": "https://cloud.siliconflow.com/account/ak", "note": "Há modelos gratuitos; confira a lista no painel."},
    {"id": "nebius", "label": "Nebius AI Studio", "tier": "cloud_cheap", "base_url": "https://api.studio.nebius.com/v1",
     "key_env": "NEBIUS_API_KEY", "default_model": "meta-llama/Llama-3.3-70B-Instruct", "models": ["meta-llama/Llama-3.3-70B-Instruct"],
     "signup": "https://studio.nebius.com/settings/api-keys", "note": ""},
    {"id": "github", "label": "GitHub Models", "tier": "cloud_free", "base_url": "https://models.inference.ai.azure.com",
     "key_env": "GITHUB_API_KEY", "default_model": "gpt-4o-mini", "models": ["gpt-4o-mini", "gpt-4o"],
     "signup": "https://github.com/settings/tokens", "note": "Use um token pessoal do GitHub; camada gratuita com limites."},
    {"id": "huggingface", "label": "Hugging Face Inference", "tier": "cloud_free", "base_url": "https://router.huggingface.co/v1",
     "key_env": "HF_API_KEY", "default_model": "meta-llama/Llama-3.3-70B-Instruct", "models": ["meta-llama/Llama-3.3-70B-Instruct"],
     "signup": "https://huggingface.co/settings/tokens", "note": "Token com permissão de inferência."},
    {"id": "cohere", "label": "Cohere", "tier": "cloud_cheap", "base_url": "https://api.cohere.ai/compatibility/v1",
     "key_env": "COHERE_API_KEY", "default_model": "command-r-plus", "models": ["command-r-plus", "command-r"],
     "signup": "https://dashboard.cohere.com/api-keys", "note": "Camada de compatibilidade com a API da OpenAI."},
    {"id": "perplexity", "label": "Perplexity", "tier": "cloud_cheap", "base_url": "https://api.perplexity.ai",
     "key_env": "PERPLEXITY_API_KEY", "default_model": "sonar", "models": ["sonar", "sonar-pro"],
     "signup": "https://www.perplexity.ai/settings/api", "note": "Respostas com busca na web."},
    {"id": "moonshot", "label": "Moonshot (Kimi)", "tier": "cloud_cheap", "base_url": "https://api.moonshot.ai/v1",
     "key_env": "MOONSHOT_API_KEY", "default_model": "kimi-k2-0905-preview", "models": ["kimi-k2-0905-preview"],
     "signup": "https://platform.moonshot.ai/console/api-keys", "note": ""},
    {"id": "dashscope", "label": "Alibaba Qwen (DashScope)", "tier": "cloud_cheap",
     "base_url": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
     "key_env": "DASHSCOPE_API_KEY", "default_model": "qwen-plus", "models": ["qwen-plus", "qwen-turbo", "qwen-max"],
     "signup": "https://bailian.console.alibabacloud.com/", "note": "Endpoint internacional."},
    {"id": "anthropic", "label": "Anthropic (modo compatível)", "tier": "cloud_premium", "base_url": "https://api.anthropic.com/v1",
     "key_env": "ANTHROPIC_API_KEY", "default_model": "claude-sonnet-4-5", "models": ["claude-sonnet-4-5"],
     "signup": "https://console.anthropic.com/settings/keys", "note": "Usa a camada de compatibilidade OpenAI da Anthropic (sem todos os recursos)."},
    {"id": "lambda", "label": "Lambda Inference", "tier": "cloud_cheap", "base_url": "https://api.lambda.ai/v1",
     "key_env": "LAMBDA_API_KEY", "default_model": "llama3.3-70b-instruct-fp8", "models": ["llama3.3-70b-instruct-fp8"],
     "signup": "https://cloud.lambda.ai/api-keys", "note": ""},
]


def get_preset(preset_id: str) -> Dict[str, Any]:
    for p in PROVIDER_PRESETS:
        if p["id"] == preset_id:
            return dict(p)
    raise KeyError(preset_id)
