"""Instalacao por pip/pipx: estado fora do site-packages e arquivos de dados dentro do pacote."""
import tomllib
from pathlib import Path

from app.config_manager import ConfigManager
from app.security.data_dir import data_dir as data_dir_seguranca
from app.state_store import StateStore
from cli.chat_client import ler_token

RAIZ = Path(__file__).resolve().parent.parent


def test_data_dir_da_seguranca_respeita_hera_home(tmp_path):
    assert data_dir_seguranca({"HERA_HOME": str(tmp_path)}) == tmp_path / "data"
    assert data_dir_seguranca({"HERA_DATA_DIR": str(tmp_path / "d")}) == tmp_path / "d"
    assert data_dir_seguranca({}) == RAIZ / "data"            # checkout de codigo: igual a antes


def test_config_manager_usa_hera_config(tmp_path, monkeypatch):
    alvo = tmp_path / "minha" / "config.yaml"
    alvo.parent.mkdir()
    monkeypatch.setenv("HERA_CONFIG", str(alvo))
    assert ConfigManager().config_path == alvo


def test_state_store_usa_a_pasta_de_dados_configurada(tmp_path, monkeypatch):
    monkeypatch.setenv("HERA_DATA_DIR", str(tmp_path))
    assert StateStore().db_path == tmp_path / "orchestrator.db"


def test_ler_token_procura_na_pasta_de_dados_configurada(tmp_path):
    pasta = tmp_path / "dados"
    pasta.mkdir()
    (pasta / ".hera_token").write_text("tok-da-pasta-de-dados\n", encoding="utf-8")
    assert ler_token(tmp_path / "outra-raiz", environ={"HERA_DATA_DIR": str(pasta)}) == "tok-da-pasta-de-dados"


def test_ler_token_sem_variavel_continua_lendo_raiz_data(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / ".hera_token").write_text("tok-raiz", encoding="utf-8")
    assert ler_token(tmp_path, environ={}) == "tok-raiz"


def test_pyproject_empacota_politicas_e_interface_web():
    cfg = tomllib.loads((RAIZ / "pyproject.toml").read_text(encoding="utf-8"))
    incluidos = cfg["tool"]["setuptools"]["packages"]["find"]["include"]
    for pacote in ("app*", "adapters*", "models*", "cli*", "policies*", "web*"):
        assert pacote in incluidos, pacote
    dados = cfg["tool"]["setuptools"]["package-data"]["*"]
    for ext in ("*.yaml", "*.html", "*.css", "*.js", "*.woff2", "*.svg", "*.png"):
        assert ext in dados, ext


def test_servidor_e_iniciado_como_modulo_para_funcionar_instalado():
    from cli import chat_cmd
    capturado = []
    chat_cmd.garantir_servidor("http://127.0.0.1:1", RAIZ, iniciar=lambda cmd, cwd: capturado.append(cmd),
                               no_ar=lambda url: bool(capturado), dormir=lambda s: None, escrever=lambda *_: None)
    assert capturado and capturado[0][1:3] == ["-m", "main"], capturado


def test_banco_do_servidor_fica_na_pasta_do_hera_e_nao_no_codigo(tmp_path):
    """O db_path relativo do config vale a partir de HERA_HOME (instalado), nao de site-packages."""
    from app.paths import home_hera
    from app.runtime_state import resolve_db_path
    home = home_hera({"HERA_HOME": str(tmp_path)})
    assert resolve_db_path("data/orchestrator.db", home, environ={}) == tmp_path / "data" / "orchestrator.db"
    server_src = (RAIZ / "app" / "server.py").read_text(encoding="utf-8")
    assert "resolve_db_path(cfg[\"general\"].get(\"db_path\"), home_hera())" in server_src      # nao mais ROOT_DIR
