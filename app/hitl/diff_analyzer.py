import re
from pathlib import Path
from typing import List, Optional, Tuple, Set
from app.hitl.schemas import DiffStats, DiffAlert

# Padrões de detecção de segredos em diffs adicionados
SECRET_PATTERNS = [
    (re.compile(r"sk-[a-zA-Z0-9_-]{20,}"), "OpenAI API Key"),
    (re.compile(r"sk-ant-[a-zA-Z0-9_-]{20,}"), "Anthropic API Key"),
    (re.compile(r"ghp_[a-zA-Z0-9]{30,}"), "GitHub Personal Access Token"),
    (re.compile(r"gho_[a-zA-Z0-9]{30,}"), "GitHub OAuth Token"),
    (re.compile(r"AIza[0-9A-Za-z-_]{35}"), "Google Cloud API Key"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS Access Key ID"),
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----"), "Chave Privada Criptográfica"),
    (re.compile(r"(?i)(api[_-]?key|secret[_-]?token|auth[_-]?token)\s*[:=]\s*['\"][a-zA-Z0-9_\-\.]{12,}['\"]"), "Credencial Genérica"),
]

# Nomes de arquivos sensíveis ou críticos
SENSITIVE_FILENAME_PATTERNS = [
    re.compile(r"(^|/)\.env(\.[a-zA-Z0-9_-]+)?$", re.IGNORECASE),
    re.compile(r"(^|/)(credentials|secrets|token|master_key)\.json$", re.IGNORECASE),
    re.compile(r"(^|/)(id_rsa|id_ed25519|id_ecdsa)(\.pub)?$", re.IGNORECASE),
    re.compile(r"\.(pem|key|pkcs12|pfx|keystore)$", re.IGNORECASE),
]

# Arquivos de infraestrutura / migrações que merecem auditoria especial
INFRA_FILENAME_PATTERNS = [
    re.compile(r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Pipfile\.lock)$"),
    re.compile(r"(^|/)(migrations|alembic)/versions/.*\.py$"),
    re.compile(r"(^|/)(docker-compose\.ya?ml|Dockerfile)$", re.IGNORECASE),
]


class DiffAnalyzer:
    """Analisador sintático e de segurança de diffs Git."""

    @staticmethod
    def extract_modified_files(diff_text: str) -> List[str]:
        """Extrai lista de arquivos modificados a partir de cabeçalhos diff --git."""
        files: Set[str] = set()
        for line in diff_text.splitlines():
            if line.startswith("diff --git "):
                parts = line.split()
                if len(parts) >= 4:
                    # extrai parte b/path
                    b_path = parts[3]
                    if b_path.startswith("b/"):
                        files.add(b_path[2:])
                    else:
                        files.add(b_path)
            elif line.startswith("+++ b/"):
                files.add(line[6:].strip())
        return sorted(list(files))

    @classmethod
    def calculate_stats(cls, diff_text: str) -> DiffStats:
        """Calcula estatísticas de adições, remoções e contagem de arquivos."""
        insertions = 0
        deletions = 0
        binary_count = 0
        files = cls.extract_modified_files(diff_text)

        for line in diff_text.splitlines():
            if line.startswith("Binary files ") or "GIT binary patch" in line:
                binary_count += 1
            elif line.startswith("+") and not line.startswith("+++"):
                insertions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1

        return DiffStats(
            files_changed=len(files),
            insertions=insertions,
            deletions=deletions,
            binary_files_count=binary_count,
        )

    @classmethod
    def analyze_alerts(
        cls,
        diff_text: str,
        allowed_paths: Optional[List[str]] = None
    ) -> List[DiffAlert]:
        """Gera alertas de segurança (segredos, arquivos restritos, infraestrutura e escopo)."""
        alerts: List[DiffAlert] = []
        files = cls.extract_modified_files(diff_text)

        # 1. Alertas de arquivos sensíveis ou de infraestrutura
        for f in files:
            # Arquivo sensível / credencial
            for pat in SENSITIVE_FILENAME_PATTERNS:
                if pat.search(f):
                    alerts.append(DiffAlert(
                        level="danger",
                        category="sensitive_file",
                        message=f"Arquivo de alta sensibilidade detectado no diff: '{f}'. Verifique se não contém segredos locais.",
                        file_path=f
                    ))
                    break

            # Arquivo de infraestrutura / lockfile / migração
            for pat in INFRA_FILENAME_PATTERNS:
                if pat.search(f):
                    alerts.append(DiffAlert(
                        level="warning",
                        category="sensitive_file",
                        message=f"Alteração em arquivo crítico de dependências/migração: '{f}'.",
                        file_path=f
                    ))
                    break

            # Fora de escopo
            if allowed_paths:
                norm_f = Path(f).as_posix()
                is_allowed = any(norm_f == Path(ap).as_posix() or norm_f.startswith(Path(ap).as_posix() + "/") for ap in allowed_paths)
                if not is_allowed:
                    alerts.append(DiffAlert(
                        level="warning",
                        category="out_of_scope",
                        message=f"Arquivo '{f}' alterado está fora da lista de caminhos autorizados da tarefa.",
                        file_path=f
                    ))

        # 2. Alertas de Binários
        if "GIT binary patch" in diff_text or "Binary files " in diff_text:
            alerts.append(DiffAlert(
                level="warning",
                category="binary",
                message="O patch contém alterações em arquivos binários que não podem ser inspecionados visualmente como texto puro."
            ))

        # 3. Varredura de Segredos em linhas adicionadas
        current_file = "desconhecido"
        for line in diff_text.splitlines():
            if line.startswith("+++ b/"):
                current_file = line[6:].strip()
            elif line.startswith("+") and not line.startswith("+++"):
                # Apenas linhas adicionadas
                for pat, secret_type in SECRET_PATTERNS:
                    match = pat.search(line)
                    if match:
                        alerts.append(DiffAlert(
                            level="danger",
                            category="secret",
                            message=f"Possível segredo ou credencial adicionada ({secret_type}) detectada em '{current_file}'.",
                            file_path=current_file
                        ))
                        break

        return alerts
