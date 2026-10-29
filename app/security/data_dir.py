"""Pasta de dados locais (tokens, segredos, nonces): $HERA_DATA_DIR ou <projeto>/data."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

DATA_ENV = "HERA_DATA_DIR"


def data_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    from app.paths import data_dir as _data_dir        # regra unica: checkout => <projeto>/data; instalado => ~/.hera/data
    env = os.environ if environ is None else environ
    return _data_dir(env)
