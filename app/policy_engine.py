import os
import re
import shlex
import yaml
from pathlib import Path
from typing import Tuple, List, Optional, Set

class PolicyViolationError(Exception):
    """Exceção levantada quando um comando ou ação viola as políticas de segurança."""
    pass

class PolicyEngine:
    """Motor de validação de políticas e segurança baseado em argv estruturado.
    
    Aplica validação rigorosa sobre executáveis, argumentos e caminhos,
    eliminando riscos de shell injection, metacaracteres e caminhos destrutivos.
    """

    ALLOWED_PROGRAMS: Set[str] = {"pytest", "python", "git", "ruff", "flake8", "mypy", "black"}

    ALLOWED_PYTHON_MODULES: Set[str] = {"pytest", "py_compile", "unittest"}

    SAFE_GIT_SUBCOMMANDS: Set[str] = {"status", "diff", "log", "branch"}

    FORBIDDEN_SHELL_CHARS = re.compile(r'[;&|><$`\n\r]')

    PROTECTED_SYSTEM_PATHS = [
        r"c:\\windows",
        r"c:\\program files",
        r"%systemroot%",
        r"%windir%",
        r"\.\.[\\/]"  # Path traversal
    ]

    def __init__(self, policy_path: Optional[str] = None):
        self.policy_path = policy_path or os.path.join("policies", "permissions.yaml")
        self.allowed_programs = set(self.ALLOWED_PROGRAMS)
        self.forbidden_patterns: List[re.Pattern] = []
        self._load_policy()

    def _load_policy(self):
        if os.path.exists(self.policy_path):
            with open(self.policy_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}

            if "allowed_programs" in data:
                self.allowed_programs.update([p.lower() for p in data["allowed_programs"]])
            if "allowed_commands" in data:
                for c in data["allowed_commands"]:
                    self.allowed_programs.add(c.split()[0].lower())

            if "forbidden_patterns" in data:
                compiled = []
                for p in data["forbidden_patterns"]:
                    if any(meta in p for meta in [".*", r"\s", r"\b", "[", "]", "(", ")"]):
                        compiled.append(re.compile(p, re.IGNORECASE))
                    else:
                        compiled.append(re.compile(re.escape(p).replace(r"\ ", r"\s+"), re.IGNORECASE))
                self.forbidden_patterns = compiled

    def evaluate_command(self, cmd: str) -> Tuple[bool, str]:
        """Avalia estruturadamente um comando e retorna (is_allowed, reason)."""
        clean_cmd = cmd.strip()
        if not clean_cmd:
            return False, "Comando vazio"

        # 1. Bloqueio imediato de metacaracteres de encadeamento ou injeção de shell
        if self.FORBIDDEN_SHELL_CHARS.search(clean_cmd):
            return False, "Comando bloqueado: detectados metacaracteres de encadeamento ou redirecionamento de shell (; & | > < $ `)"

        # 2. Checar padrões explicitamente proibidos
        for pattern in self.forbidden_patterns:
            if pattern.search(clean_cmd):
                return False, f"Comando bloqueado: detectado padrão de alto risco ({pattern.pattern})"

        # 3. Checar caminhos protegidos do sistema ou tentativa de path traversal
        lower_cmd = clean_cmd.lower()
        for protected in self.PROTECTED_SYSTEM_PATHS:
            if re.search(protected, lower_cmd):
                return False, f"Comando bloqueado: tentativa de acesso a diretório protegido ({protected})"

        # 4. Parsing estruturado de argv com shlex
        try:
            argv = shlex.split(clean_cmd, posix=False if os.name == 'nt' else True)
        except Exception as e:
            return False, f"Comando bloqueado: falha na análise estruturada dos argumentos: {e}"

        if not argv:
            return False, "Comando sem executável identificável"

        prog = os.path.basename(argv[0]).lower()
        if prog.endswith(".exe"):
            prog = prog[:-4]

        # 5. Validação rigorosa do programa e perfil de argumentos
        if prog not in self.allowed_programs:
            return False, f"Comando bloqueado: executável '{prog}' não autorizado pela allowlist restrita"

        # 5.1 Perfil: pytest
        if prog == "pytest":
            # Não permitir nomes maliciosos ou binários disfarçados
            return True, "Executável pytest autorizado"

        # 5.2 Perfil: python
        if prog == "python":
            if len(argv) < 2:
                return False, "Comando python sem argumentos não permitido"

            first_arg = argv[1].lower()

            # Caso 1: python -m <modulo_seguro>
            if first_arg == "-m" and len(argv) >= 3:
                mod = argv[2].lower()
                if mod in self.ALLOWED_PYTHON_MODULES:
                    return True, f"Módulo python '{mod}' autorizado"
                return False, f"Execução do módulo python '{mod}' não permitida na política de segurança"

            # Caso 2: python main.py <subcomando_seguro>
            if "main.py" in first_arg:
                if len(argv) >= 3 and argv[2] in {"health", "list", "vault-search", "benchmark"}:
                    return True, f"Comando CLI interno main.py '{argv[2]}' autorizado"
                return False, "Execução direta de main.py não autorizada para esta ação"

            # Bloquear scripts arbitrários, -c e argumentos desconhecidos
            return False, f"Execução de script/argumento python '{first_arg}' bloqueada por segurança"

        # 5.3 Perfil: git
        if prog == "git":
            if len(argv) < 2:
                return False, "Comando git sem argumentos não permitido"
            subcmd = argv[1].lower()
            if subcmd in self.SAFE_GIT_SUBCOMMANDS:
                return True, f"Subcomando git de leitura '{subcmd}' autorizado"
            return False, f"Subcomando git '{subcmd}' bloqueado (apenas leitura permitida)"

        # 5.4 Perfil: ruff, flake8, mypy, black
        if prog in {"ruff", "flake8", "mypy", "black"}:
            if prog == "black" and "--check" not in [a.lower() for a in argv]:
                return False, "O formatador black só é permitido no modo de verificação (--check)"
            return True, f"Ferramenta de linter/inspeção '{prog}' autorizada"

        # 5.5 Outros programas explicitamente autorizados na configuração
        if prog in self.allowed_programs:
            return True, f"Programa '{prog}' autorizado pela configuração de política"

        return False, "Comando não catalogado na política de execução autônoma"
