"""Fase 4: nenhum caminho de usuario fixo no codigo, e padroes derivados do ambiente."""
import os
import re
import subprocess
from pathlib import Path

import pytest

from app.config_manager import DEFAULT_CONFIG, ConfigManager
from app.locations import AGY_ENV, VAULT_ENV, default_vault_dir, find_agy_binary, resolve_vault_dir
from app.obsidian import ObsidianVaultWriter
from app.vault_memory import ObsidianMemoryManager

ROOT = Path(__file__).resolve().parent.parent


# ── locations ──────────────────────────────────────────────────────────────────
def test_vault_padrao_vem_do_home_e_a_env_tem_prioridade(tmp_path):
    padrao = Path(default_vault_dir({}))
    assert padrao == Path.home() / "Documents" / "Obsidian Vault" / "Orquestrador Multiagente"
    assert default_vault_dir({VAULT_ENV: str(tmp_path)}) == str(tmp_path)
    assert default_vault_dir({VAULT_ENV: "   "}) == str(padrao)               # env vazia nao conta


def test_resolve_vault_dir_prioridade_config_env_padrao(tmp_path):
    env = {VAULT_ENV: str(tmp_path / "env")}
    assert resolve_vault_dir("C:\\meu\\vault", env) == "C:\\meu\\vault"       # config.yaml / argumento vence
    assert resolve_vault_dir("", env) == str(tmp_path / "env")                 # vazio cai na env
    assert resolve_vault_dir(None, {}) == default_vault_dir({})


def test_find_agy_prioridade_env_path_localappdata_nome_simples(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    assert find_agy_binary({}) == "agy"                                        # nada encontrado
    (tmp_path / "agy" / "bin").mkdir(parents=True)
    exe = tmp_path / "agy" / "bin" / "agy.EXE"
    exe.write_text("x")
    assert find_agy_binary({"LOCALAPPDATA": str(tmp_path)}) == str(exe)
    monkeypatch.setattr("shutil.which", lambda name: "C:\\no\\path\\agy.exe")
    assert find_agy_binary({"LOCALAPPDATA": str(tmp_path)}) == "C:\\no\\path\\agy.exe"   # PATH > LOCALAPPDATA
    outro = tmp_path / "custom.exe"
    outro.write_text("x")
    assert find_agy_binary({AGY_ENV: str(outro)}) == str(outro)                # env vence
    assert find_agy_binary({AGY_ENV: str(tmp_path / "nao-existe.exe")}) != str(tmp_path / "nao-existe.exe")


# ── os componentes usam o padrao portatil ──────────────────────────────────────
def test_config_padrao_nao_tem_caminho_de_usuario(tmp_path):
    assert DEFAULT_CONFIG["general"]["vault_dir"] == ""
    cfg = ConfigManager(config_path=str(tmp_path / "config.yaml")).load_config()   # config novo = padrao portatil
    assert cfg["general"]["vault_dir"] == ""


def test_obsidian_e_memoria_sem_argumento_usam_a_env(tmp_path, monkeypatch):
    monkeypatch.setenv(VAULT_ENV, str(tmp_path / "vault"))
    w = ObsidianVaultWriter()
    m = ObsidianMemoryManager()
    assert Path(w.vault_project_dir) == (tmp_path / "vault").resolve() or w.vault_project_dir == os.path.abspath(tmp_path / "vault")
    assert m.vault_project_dir == os.path.abspath(tmp_path / "vault")
    assert (tmp_path / "vault" / "08 Hist\u00f3rico").is_dir()                       # criou so dentro do vault configurado


def test_argumento_explicito_continua_valendo(tmp_path):
    w = ObsidianVaultWriter(vault_project_dir=str(tmp_path / "x"))
    assert w.vault_project_dir == os.path.abspath(tmp_path / "x")


# ── varredura: o caminho do usuario nao volta ──────────────────────────────────
PLACEHOLDERS = {"you", "user", "username", "yourname", "your-name", "name", "public", "default", "all users"}
USER_PATH = re.compile(r"(?:[A-Za-z]:[\\/]+Users[\\/]+|/home/|/Users/)([^\\/\s\"'`<>{}%$]+)", re.IGNORECASE)
TEXT_EXT = {".py", ".yaml", ".yml", ".toml", ".md", ".html", ".json", ".txt", ".cfg", ".ini"}
SELF = Path(__file__).resolve()


def _tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        return [ROOT / p for p in out.splitlines() if p]
    except (OSError, subprocess.SubprocessError):
        skip = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".worktrees", "data"}
        return [p for p in ROOT.rglob("*") if p.is_file() and not skip & set(p.relative_to(ROOT).parts)]


def test_nenhum_caminho_de_usuario_fixo_nos_arquivos_versionados():
    achados = []
    for path in _tracked_files():
        if path.suffix.lower() not in TEXT_EXT or path.resolve() == SELF or not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for m in USER_PATH.finditer(line):
                if m.group(1).lower() not in PLACEHOLDERS:
                    achados.append(f"{path.relative_to(ROOT)}:{i}: {m.group(0)}")
    assert not achados, "caminho de usuario fixo encontrado:\n" + "\n".join(achados)


def test_config_local_nao_e_versionado_e_o_exemplo_e_portatil():
    tracked = {p.name for p in _tracked_files()}
    assert "config.yaml" not in tracked, "config.yaml tem caminhos da maquina: deve ficar no .gitignore"
    example = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    assert "vault_dir: ''" in example or 'vault_dir: ""' in example
    assert not USER_PATH.search(example)


def test_o_scanner_pega_o_que_deve_pegar():
    for ruim in (r"C:\Users\Fulano\Documents", "C:/Users/fulano/x", "/home/fulano/x", "/Users/fulano/x"):
        assert USER_PATH.search(ruim) and USER_PATH.search(ruim).group(1).lower() not in PLACEHOLDERS
    for ok in (r"C:\Users\<you>\x", r"C:\Users\%USERNAME%\x", "/home/user/x", r"C:\Users\Public"):
        m = USER_PATH.search(ok)
        assert m is None or m.group(1).lower() in PLACEHOLDERS
