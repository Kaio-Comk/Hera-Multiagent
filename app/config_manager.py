import os
import yaml
from pathlib import Path
from typing import Dict, Any, Optional

DEFAULT_CONFIG: Dict[str, Any] = {
    "general": {
        "host": "127.0.0.1",
        "port": 8000,
        "default_offline": False,
        "default_engine": "ollama",
        "vault_dir": "",  # vazio = $HERA_VAULT_DIR ou ~/Documents/Obsidian Vault/Orquestrador Multiagente
        "db_path": "data/orchestrator.db"
    },
    "engines": {
        "ollama": {
            "enabled": True,
            "base_url": "http://localhost:11434",
            "default_model": "qwen2.5-coder:1.5b",
            "timeout": 120.0
        },
        "nine_router": {
            "enabled": True,
            "base_url": "http://127.0.0.1:20128/v1",
            "default_model": "alicode-intl/qwen3.5-plus",
            "timeout": 60.0
        },
        "claude_code": {
            "enabled": True,
            "default_model": "claude-3-7-sonnet",
            "timeout": 300.0
        },
        "antigravity": {
            "enabled": True,
            "default_model": "gemini-2.5-pro",
            "timeout": 300.0
        }
    },
    "feature_flags": {
        "enable_gateway": True,
        "enable_rtk": False,
        "enable_arena": False,
        "enable_mcp": False
    },
    # Provedores de nuvem do gateway (desligados por padrão). As CHAVES ficam em variáveis de ambiente
    # (api_key_env), nunca neste arquivo. Exemplos em config.example.yaml.
    "providers": {},
    "mcp": {
        # Executáveis permitidos para servidores MCP stdio (nome simples, sem caminho).
        "allowed_commands": ["python", "python3", "py"]
    },
    "gateway": {
        "max_concurrent_requests": 10,
        "request_timeout_seconds": 60.0,
        "total_timeout_seconds": 180.0,  # prazo TOTAL de uma requisição (todas as tentativas/fallbacks)
        "default_tier_order": ["cloud_premium", "cloud_cheap", "cloud_free", "local"]
    }
}

class ConfigManager:
    """Gerenciador centralizado de configurações persistidas em config.yaml."""

    def __init__(self, config_path: Optional[str] = None):
        if config_path:
            self.config_path = Path(config_path)
        else:
            self.config_path = Path(__file__).resolve().parent.parent / "config.yaml"
        self._ensure_config()

    def _ensure_config(self):
        """Cria config.yaml inicial caso não exista."""
        if not self.config_path.exists():
            self.save_config(DEFAULT_CONFIG)

    def load_config(self) -> Dict[str, Any]:
        """Carrega e mescla a configuração do disco com os padrões seguros."""
        if not self.config_path.exists():
            return DEFAULT_CONFIG.copy()
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            
            # Mesclagem defensiva com defaults
            merged = DEFAULT_CONFIG.copy()
            for section in ("general", "engines", "feature_flags", "gateway", "mcp", "providers"):
                if section in data and isinstance(data[section], dict):
                    merged[section] = {**merged.get(section, {}), **data[section]}
            return merged
        except Exception:
            return DEFAULT_CONFIG.copy()

    def save_config(self, config_data: Dict[str, Any]):
        """Persiste configurações no arquivo YAML com indentação limpa."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.config_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(config_data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    def update_engine_model(self, engine_name: str, model_name: str):
        """Atualiza o modelo padrão de um motor específico."""
        cfg = self.load_config()
        if engine_name in cfg.get("engines", {}):
            cfg["engines"][engine_name]["default_model"] = model_name
            self.save_config(cfg)

    def set_offline_mode(self, enabled: bool):
        """Ativa ou desativa a política de execução 100% offline."""
        cfg = self.load_config()
        cfg["general"]["default_offline"] = enabled
        self.save_config(cfg)
