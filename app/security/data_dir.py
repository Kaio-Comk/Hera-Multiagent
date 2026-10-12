"""Pasta de dados locais (tokens, segredos, nonces): $HERA_DATA_DIR ou <projeto>/data."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

DATA_ENV = "HERA_DATA_DIR"


def data_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if environ is None else environ
    fixed = (env.get(DATA_ENV) or "").strip()
    return Path(fixed) if fixed else Path(__file__).resolve().parent.parent.parent / "data"
