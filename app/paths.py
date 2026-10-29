from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

RAIZ_CODIGO = Path(__file__).resolve().parent.parent


def modo_instalado(raiz: Path | None = None) -> bool:
    '''True quando a raiz nao parece um checkout de codigo.'''
    base = Path(raiz) if raiz is not None else RAIZ_CODIGO
    tem_pyproject = (base / 'pyproject.toml').exists()
    tem_git = (base / '.git').exists()
    return not (tem_pyproject or tem_git)


def _env(environ: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if environ is None else environ


def _valor(environ: Mapping[str, str] | None, chave: str) -> str | None:
    valor = _env(environ).get(chave)
    if valor is None or valor == '':
        return None
    return valor


def home_hera(
    environ: Mapping[str, str] | None = None,
    raiz: Path | None = None,
    home: Path | None = None,
) -> Path:
    '''Diretorio base do Hera: HERA_HOME, ~/.hera ou a raiz do codigo.'''
    valor = _valor(environ, 'HERA_HOME')
    if valor is not None:
        return Path(valor)
    base = Path(raiz) if raiz is not None else RAIZ_CODIGO
    if modo_instalado(base):
        casa = Path(home) if home is not None else Path.home()
        return casa / '.hera'
    return base


def data_dir(
    environ: Mapping[str, str] | None = None,
    raiz: Path | None = None,
    home: Path | None = None,
) -> Path:
    '''Diretorio de dados: HERA_DATA_DIR ou home_hera/data.'''
    valor = _valor(environ, 'HERA_DATA_DIR')
    if valor is not None:
        return Path(valor)
    return home_hera(environ, raiz, home) / 'data'


def config_path(
    environ: Mapping[str, str] | None = None,
    raiz: Path | None = None,
    home: Path | None = None,
) -> Path:
    '''Caminho do config.yaml: HERA_CONFIG ou home_hera/config.yaml.'''
    valor = _valor(environ, 'HERA_CONFIG')
    if valor is not None:
        return Path(valor)
    return home_hera(environ, raiz, home) / 'config.yaml'


def diretorio_relatorios(
    environ: Mapping[str, str] | None = None,
    raiz: Path | None = None,
    home: Path | None = None,
) -> Path:
    '''Diretorio de relatorios dentro de data_dir.'''
    return data_dir(environ, raiz, home) / 'reports'


def db_padrao(
    environ: Mapping[str, str] | None = None,
    raiz: Path | None = None,
    home: Path | None = None,
) -> Path:
    '''Caminho padrao do banco dentro de data_dir.'''
    return data_dir(environ, raiz, home) / 'orchestrator.db'
