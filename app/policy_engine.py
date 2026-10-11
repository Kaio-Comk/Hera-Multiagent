import os
import re
import yaml
from typing import Tuple, List, Optional

class PolicyViolationError(Exception):
    """Exceção levantada quando um comando ou ação viola as políticas de segurança."""
    pass

class PolicyEngine:
    """Motor de validação de políticas e segurança (Allowlist / Denylist).
    
    Impede que modelos executem comandos destrutivos no terminal, acessem
    pastas sensíveis do Windows ou operem fora dos limites seguros do projeto.
    """

    DEFAULT_ALLOWED_PREFIXES = [
        "pytest",
        "python -m pytest",
        "python -m py_compile",
        "python main.py",
        "python -c",
        "git status",
        "git diff",
        "git log",
        "git branch",
        "ruff check",
        "flake8",
        "mypy"
    ]

    DEFAULT_FORBIDDEN_PATTERNS = [
        r"\brm\s+-rf\b",
        r"\brmdir\s+/s\b",
        r"\bdel\s+/[fF]\b",
        r"\bremove-item\b.*-recurse.*-force",
        r"\bformat\s+[a-zA-Z]:",
        r"\bgit\s+reset\s+--hard\b",
        r"\bgit\s+push\s+.*--force\b",
        r"\breg\s+delete\b",
        r"\bshutdown\b",
        r"\breboot\b",
        r"\bdrop\s+database\b",
        r"\bdrop\s+table\b"
    ]

    PROTECTED_SYSTEM_PATHS = [
        r"c:\\windows",
        r"c:\\program files",
        r"%systemroot%",
        r"%windir%",
        r"\.\.[\\/]\.\.[\\/]"  # Path traversal
    ]

    def __init__(self, policy_path: Optional[str] = None):
        self.policy_path = policy_path or os.path.join("policies", "permissions.yaml")
        self.allowed_prefixes = list(self.DEFAULT_ALLOWED_PREFIXES)
        self.forbidden_patterns = [re.compile(p, re.IGNORECASE) for p in self.DEFAULT_FORBIDDEN_PATTERNS]
        self._load_policy()

    def _load_policy(self):
        if os.path.exists(self.policy_path):
            try:
                with open(self.policy_path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                if "allowed_commands" in data:
                    self.allowed_prefixes = data["allowed_commands"]
                if "forbidden_patterns" in data:
                    compiled = []
                    for p in data["forbidden_patterns"]:
                        if any(meta in p for meta in [".*", r"\s", r"\b", "[", "]", "(", ")"]):
                            compiled.append(re.compile(p, re.IGNORECASE))
                        else:
                            compiled.append(re.compile(re.escape(p).replace(r"\ ", r"\s+"), re.IGNORECASE))
                    self.forbidden_patterns = compiled
            except Exception:
                pass

    def evaluate_command(self, cmd: str) -> Tuple[bool, str]:
        """Avalia um comando e retorna (is_allowed, reason)."""
        clean_cmd = cmd.strip()

        # 1. Checar caminhos protegidos do sistema ou tentativa de path traversal
        lower_cmd = clean_cmd.lower()
        for protected in self.PROTECTED_SYSTEM_PATHS:
            if re.search(protected, lower_cmd):
                return False, f"Comando bloqueado: tentativa de acesso a diretório protegido ({protected})"

        # 2. Checar padrões explicitamente proibidos
        for pattern in self.forbidden_patterns:
            if pattern.search(clean_cmd):
                return False, f"Comando bloqueado: detectado padrão de alto risco ({pattern.pattern})"

        # 3. Checar se o comando inicia com algum prefixo pré-autorizado
        for prefix in self.allowed_prefixes:
            if clean_cmd.startswith(prefix) or lower_cmd.startswith(prefix.lower()):
                return True, f"Autorizado pela allowlist ('{prefix}')"

        # 4. Caso genérico de execução python segura no diretório
        if clean_cmd.startswith("python "):
            return True, "Comando python local autorizado"

        return False, "Comando não catalogado na política de execução autônoma"
