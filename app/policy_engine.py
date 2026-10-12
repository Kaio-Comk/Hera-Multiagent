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

    # ── Perfis por ARGUMENTO (fase 3, 12/10/2026) ──────────────────────────────────────────────
    # Programa na allowlist nao basta: `git branch -D`, `git diff --output=arq`, `pytest -p plugin`,
    # `mypy --install-types` ou `ruff --fix` escrevem/apagam/executam coisas. Aqui cada programa
    # ganha seu perfil. `git` e uma allowlist estrita; os demais bloqueiam as opcoes perigosas
    # conhecidas (denylist) E exigem que caminhos fiquem dentro do workspace.
    GIT_READ_SUBCOMMANDS: Set[str] = {"status", "diff", "log"}
    GIT_DENIED_PREFIXES = (
        "--output", "--no-index", "--ext-diff", "--textconv", "--exec-path", "--git-dir",
        "--work-tree", "--open-files-in-pager", "--upload-pack", "--receive-pack",
    )
    # `-c` / `-C` DEPOIS do subcomando sao "diff combinado" / "detectar copias" (inofensivos); a
    # configuracao `git -c k=v` vem ANTES do subcomando e ja cai em "subcomando nao permitido".
    GIT_DENIED_SHORT = ("-O",)          # -O<arquivo> le um arquivo qualquer como ordem do diff
    GIT_BRANCH_LIST_FLAGS = {
        "--list", "-l", "--show-current", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--verbose",
        "--merged", "--no-merged", "--contains", "--no-contains", "--sort", "--format", "--color",
        "--no-color", "--column", "--no-column", "-i", "--ignore-case", "--points-at", "--abbrev",
        "--no-abbrev",
    }
    PYTEST_DENIED_PREFIXES = (
        "-c", "--config-file", "-o", "--override-ini", "--confcutdir", "--rootdir", "--pyargs",
        "--basetemp", "--junitxml", "--junit-xml", "--resultlog", "--result-log", "--report-log",
        "--log-file", "--html", "--cov-report", "--pdb", "--trace", "--debugger",
    )
    LINT_DENIED = {
        "ruff": ("--fix", "--fix-only", "--unsafe-fixes", "--add-noqa", "--config", "--output-file",
                 "--cache-dir", "--watch", "-w", "--isolated-config"),
        "flake8": ("--output-file", "--append-config", "--config", "--install-hook", "--tee"),
        "mypy": ("--config-file", "--python-executable", "--custom-typeshed-dir", "--install-types",
                 "--non-interactive", "--cache-dir", "--junit-xml", "--shadow-file", "--package-root",
                 "--plugin"),
        "black": ("--config", "--workers"),
    }
    # Flags cujo VALOR (proximo argumento) nao e um caminho: nao entram na checagem de workspace.
    VALUE_FLAGS = {"-k", "-m", "-W", "-n", "--maxfail", "--durations", "--tb", "-p", "--color",
                   "--select", "--ignore", "--extend-select", "--max-line-length", "--line-length"}

    @staticmethod
    def _flag_name(arg: str) -> str:
        return arg.split("=", 1)[0].lower()

    def _denied_flag(self, argv_tail: List[str], denied_prefixes) -> Optional[str]:
        for a in argv_tail:
            low = a.lower()
            for d in denied_prefixes:
                if low == d or low.startswith(d + "=") or (d.startswith("--") and low.startswith(d)):
                    return a
        return None

    def _path_violation(self, argv_tail: List[str], workspace: Optional[str]) -> Optional[str]:
        """Argumento que parece caminho e aponta para FORA do workspace (ou None)."""
        if not workspace:
            return None
        ws = Path(workspace).resolve()
        skip_next = False
        for a in argv_tail:
            if skip_next:
                skip_next = False
                continue
            if self._flag_name(a) in self.VALUE_FLAGS and "=" not in a:
                skip_next = True
                continue
            cand = a.split("::", 1)[0]                       # id de teste do pytest: arq.py::teste
            if cand.startswith("-"):
                if "=" not in cand:
                    continue
                cand = cand.split("=", 1)[1]
            cand = cand.strip().strip("\"'")
            if not cand:
                continue
            p = Path(cand)
            looks_like_path = (p.is_absolute() or "/" in cand or "\\" in cand or cand.startswith(".")
                               or (len(cand) > 1 and cand[1] == ":") or (ws / cand).exists())
            if not looks_like_path:
                continue
            full = p if p.is_absolute() else ws / p
            try:
                full.resolve().relative_to(ws)
            except (ValueError, OSError):
                return a
        return None

    def _check_git(self, argv: List[str]) -> Tuple[bool, str]:
        sub = argv[1].lower()
        rest = argv[2:]
        if sub in self.GIT_READ_SUBCOMMANDS:
            for a in rest:
                low = a.lower()
                if a.startswith(self.GIT_DENIED_SHORT) or low.startswith(self.GIT_DENIED_PREFIXES):
                    return False, f"Opcao git '{a}' bloqueada (escreve arquivo, executa programa ou sai do repositorio)"
            return True, f"Subcomando git de leitura '{sub}' autorizado"
        if sub == "branch":
            positional = [a for a in rest if not a.startswith("-")]
            for a in rest:
                if a.startswith("-") and self._flag_name(a) not in self.GIT_BRANCH_LIST_FLAGS:
                    return False, f"git branch so pode LISTAR; opcao '{a}' bloqueada (-D/-d/-m/-c alteram branches)"
            if positional and not {"--list", "-l"} & {a.lower() for a in rest}:
                return False, "git branch com nome sem --list criaria uma branch: bloqueado"
            return True, "git branch (somente listagem) autorizado"
        return False, f"Subcomando git '{sub}' bloqueado (apenas status, diff, log e branch --list)"

    def _check_pytest_args(self, tail: List[str], workspace: Optional[str]) -> Tuple[bool, str]:
        i = 0
        while i < len(tail):
            a = tail[i]
            low = a.lower()
            if low == "-p" or low.startswith("-p"):
                val = (tail[i + 1] if low == "-p" and i + 1 < len(tail) else low[2:] if low != "-p" else "")
                if not val.lower().startswith("no:"):
                    return False, "pytest -p <plugin> bloqueado: importa e executa um modulo arbitrario (so '-p no:...')"
                i += 2 if low == "-p" else 1
                continue
            i += 1
        bad = self._denied_flag([a for a in tail if a.lower() != "-p"], self.PYTEST_DENIED_PREFIXES)
        if bad:
            return False, f"Opcao pytest '{bad}' bloqueada por seguranca (config externa, plugin, escrita de relatorio ou depurador)"
        out = self._path_violation(tail, workspace)
        if out:
            return False, f"Caminho '{out}' fora do workspace nao e permitido"
        return True, "pytest autorizado"

    def _check_linter(self, prog: str, argv: List[str], workspace: Optional[str]) -> Tuple[bool, str]:
        tail = argv[1:]
        if prog == "ruff":
            if not tail or tail[0].lower() not in {"check", "--version", "version"}:
                return False, "ruff so e permitido com o subcomando 'check' (format/rule/server alteram arquivos)"
        if prog == "black" and "--check" not in [a.lower() for a in tail]:
            return False, "O formatador black so e permitido no modo de verificacao (--check)"
        denied = list(self.LINT_DENIED.get(prog, ()))
        if prog == "mypy":
            denied += [a.split("=", 1)[0] for a in tail if a.lower().endswith("-report") or "-report=" in a.lower()]
        bad = self._denied_flag(tail, tuple(denied))
        if bad:
            return False, f"Opcao '{bad}' de {prog} bloqueada por seguranca (config externa, plugin, escrita ou instalacao)"
        out = self._path_violation(tail, workspace)
        if out:
            return False, f"Caminho '{out}' fora do workspace nao e permitido"
        return True, f"Ferramenta de linter/inspecao '{prog}' autorizada"


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

    def evaluate_command(self, cmd: str, workspace: Optional[str] = None) -> Tuple[bool, str]:
        """Avalia estruturadamente um comando e retorna (is_allowed, reason).

        `workspace`: quando informado, caminhos passados como argumento precisam ficar dentro dele."""
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
            return self._check_pytest_args(argv[1:], workspace)

        # 5.2 Perfil: python
        if prog == "python":
            if len(argv) < 2:
                return False, "Comando python sem argumentos não permitido"

            first_arg = argv[1].lower()

            # Caso 1: python -m <modulo_seguro>
            if first_arg == "-m" and len(argv) >= 3:
                mod = argv[2].lower()
                if mod in self.ALLOWED_PYTHON_MODULES:
                    if mod == "pytest":
                        return self._check_pytest_args(argv[3:], workspace)
                    out = self._path_violation(argv[3:], workspace)
                    if out:
                        return False, f"Caminho '{out}' fora do workspace não é permitido"
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
            return self._check_git(argv)

        # 5.4 Perfil: ruff, flake8, mypy, black
        if prog in {"ruff", "flake8", "mypy", "black"}:
            return self._check_linter(prog, argv, workspace)

        # 5.5 Outros programas explicitamente autorizados na configuração
        if prog in self.allowed_programs:
            return True, f"Programa '{prog}' autorizado pela configuração de política"

        return False, "Comando não catalogado na política de execução autônoma"
