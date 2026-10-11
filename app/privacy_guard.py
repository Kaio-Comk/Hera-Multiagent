import re
from typing import Dict, List, Tuple, Optional, Any

class PrivacyGuard:
    """Motor central de Data Loss Prevention (DLP) e privacidade de dados.
    
    Inspeciona tarefas, contexto do Obsidian e payloads de saída para evitar
    vazamento acidental de credenciais para provedores externos em nuvem.
    """

    PATTERNS: Dict[str, re.Pattern] = {
        "openai_key": re.compile(r'\b(sk-[a-zA-Z0-9_\-]{20,})\b', re.IGNORECASE),
        "anthropic_key": re.compile(r'\b(sk-ant-[a-zA-Z0-9_\-]{20,})\b', re.IGNORECASE),
        "github_token": re.compile(r'\b(gh[pousr]_[a-zA-Z0-9]{20,}|github_pat_[a-zA-Z0-9_]{20,})\b', re.IGNORECASE),
        "gitlab_token": re.compile(r'\b(glpat-[a-zA-Z0-9_\-]{20,})\b', re.IGNORECASE),
        "aws_access_key": re.compile(r'\b(AKIA[0-9A-Z]{16})\b'),
        "bearer_token": re.compile(r'\bBearer\s+([a-zA-Z0-9_\-\.]{20,})\b', re.IGNORECASE),
        "private_key": re.compile(r'-----BEGIN[ A-Z0-9_\-]*PRIVATE KEY-----[\s\S]*?-----END[ A-Z0-9_\-]*PRIVATE KEY-----'),
        "jwt_token": re.compile(r'\beyJ[a-zA-Z0-9_\-]{10,}\.eyJ[a-zA-Z0-9_\-]{10,}\.[a-zA-Z0-9_\-]{10,}\b'),
        "generic_credential": re.compile(
            r'(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd)["\':=\s]+([a-zA-Z0-9_\-\.!@#$%&*]{10,})',
            re.IGNORECASE
        )
    }

    @classmethod
    def scan_content(cls, text: Optional[str]) -> Tuple[bool, List[str]]:
        """Verifica se há segredos ou credenciais no texto.
        
        Retorna (has_secrets, list_of_types_found).
        """
        if not text:
            return False, []

        found_types: List[str] = []
        for name, pattern in cls.PATTERNS.items():
            if pattern.search(text):
                found_types.append(name)

        return len(found_types) > 0, found_types

    @classmethod
    def sanitize_content(cls, text: str) -> str:
        """Substitui segredos e credenciais por identificadores seguros [REDACTED_SECRET]."""
        if not text:
            return ""

        sanitized = text
        for name, pattern in cls.PATTERNS.items():
            sanitized = pattern.sub(f"[REDACTED_SECRET:{name}]", sanitized)

        return sanitized

    @classmethod
    def validate_outbound_payload(cls, messages: List[Dict[str, Any]]) -> Tuple[bool, Optional[str]]:
        """Avalia um payload completo de mensagens antes do envio para a nuvem.
        
        Retorna (is_safe, error_message).
        """
        for i, msg in enumerate(messages):
            content = msg.get("content", "")
            if isinstance(content, str):
                has_secret, types = cls.scan_content(content)
                if has_secret:
                    return False, f"Gate DLP: envio para a nuvem bloqueado (detectado: {', '.join(types)} na mensagem {i})"
        return True, None
