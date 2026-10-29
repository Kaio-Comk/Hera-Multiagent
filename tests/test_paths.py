from __future__ import annotations

from pathlib import Path

from app import paths


def _checkout(tmp_path: Path) -> Path:
    raiz = tmp_path / 'repo'
    (raiz / 'app').mkdir(parents=True)
    (raiz / 'pyproject.toml').write_text('[project]\n', encoding='utf-8')
    return raiz


def _instalado(tmp_path: Path) -> Path:
    raiz = tmp_path / 'site-packages'
    (raiz / 'app').mkdir(parents=True)
    return raiz


def test_modo_instalado_com_pyproject(tmp_path: Path) -> None:
    raiz = _checkout(tmp_path)
    assert paths.modo_instalado(raiz) is False


def test_modo_instalado_com_git_pasta(tmp_path: Path) -> None:
    raiz = _instalado(tmp_path)
    (raiz / '.git').mkdir()
    assert paths.modo_instalado(raiz) is False


def test_modo_instalado_com_git_arquivo(tmp_path: Path) -> None:
    raiz = _instalado(tmp_path)
    (raiz / '.git').write_text('gitdir: ../.git/worktrees/x\n', encoding='utf-8')
    assert paths.modo_instalado(raiz) is False


def test_modo_instalado_sem_marcadores(tmp_path: Path) -> None:
    raiz = _instalado(tmp_path)
    assert paths.modo_instalado(raiz) is True


def test_home_hera_hera_home_vence_instalado(tmp_path: Path) -> None:
    raiz = _instalado(tmp_path)
    home = tmp_path / 'home'
    env = {'HERA_HOME': str(tmp_path / 'custom')}
    assert paths.home_hera(env, raiz, home) == tmp_path / 'custom'


def test_home_hera_hera_home_vence_checkout(tmp_path: Path) -> None:
    raiz = _checkout(tmp_path)
    home = tmp_path / 'home'
    env = {'HERA_HOME': str(tmp_path / 'custom')}
    assert paths.home_hera(env, raiz, home) == tmp_path / 'custom'


def test_home_hera_instalado_usa_home(tmp_path: Path) -> None:
    raiz = _instalado(tmp_path)
    home = tmp_path / 'home'
    assert paths.home_hera({}, raiz, home) == home / '.hera'


def test_home_hera_checkout_usa_raiz(tmp_path: Path) -> None:
    raiz = _checkout(tmp_path)
    home = tmp_path / 'home'
    assert paths.home_hera({}, raiz, home) == raiz


def test_home_hera_valor_vazio_ignorado(tmp_path: Path) -> None:
    raiz = _instalado(tmp_path)
    home = tmp_path / 'home'
    assert paths.home_hera({'HERA_HOME': ''}, raiz, home) == home / '.hera'


def test_data_dir_hera_data_dir_vence(tmp_path: Path) -> None:
    raiz = _instalado(tmp_path)
    home = tmp_path / 'home'
    env = {'HERA_DATA_DIR': str(tmp_path / 'dados')}
    assert paths.data_dir(env, raiz, home) == tmp_path / 'dados'


def test_data_dir_padrao(tmp_path: Path) -> None:
    raiz = _instalado(tmp_path)
    home = tmp_path / 'home'
    assert paths.data_dir({}, raiz, home) == home / '.hera' / 'data'


def test_config_path_hera_config_vence(tmp_path: Path) -> None:
    raiz = _instalado(tmp_path)
    home = tmp_path / 'home'
    env = {'HERA_CONFIG': str(tmp_path / 'cfg.yaml')}
    assert paths.config_path(env, raiz, home) == tmp_path / 'cfg.yaml'


def test_config_path_padrao(tmp_path: Path) -> None:
    raiz = _instalado(tmp_path)
    home = tmp_path / 'home'
    assert paths.config_path({}, raiz, home) == home / '.hera' / 'config.yaml'


def test_diretorio_relatorios(tmp_path: Path) -> None:
    raiz = _instalado(tmp_path)
    home = tmp_path / 'home'
    esperado = home / '.hera' / 'data' / 'reports'
    assert paths.diretorio_relatorios({}, raiz, home) == esperado


def test_db_padrao(tmp_path: Path) -> None:
    raiz = _instalado(tmp_path)
    home = tmp_path / 'home'
    esperado = home / '.hera' / 'data' / 'orchestrator.db'
    assert paths.db_padrao({}, raiz, home) == esperado


def test_funcoes_nao_criam_pastas(tmp_path: Path) -> None:
    raiz = _instalado(tmp_path)
    home = tmp_path / 'home'
    paths.home_hera({}, raiz, home)
    paths.data_dir({}, raiz, home)
    paths.config_path({}, raiz, home)
    paths.diretorio_relatorios({}, raiz, home)
    paths.db_padrao({}, raiz, home)
    assert not home.exists()
    assert not (home / '.hera').exists()


def test_raiz_codigo_contem_app_paths() -> None:
    assert (paths.RAIZ_CODIGO / 'app' / 'paths.py').exists()


def test_checkout_real_nao_e_instalado() -> None:
    assert paths.modo_instalado(paths.RAIZ_CODIGO) is False
    assert paths.data_dir({}, paths.RAIZ_CODIGO) == paths.RAIZ_CODIGO / 'data'
