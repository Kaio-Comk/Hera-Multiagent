"""Modelo escolhido -> argumento `--model` das CLIs (Claude Code e Antigravity).

Ate hoje o modelo escolhido no painel NAO chegava ao CLI (o comando era sempre `claude -p ...`): a lista aparecia, a
escolha era ignorada. Agora o valor vira `--model <valor>`, depois de validado: so um nome simples (letras, numeros e
`._:-/[]`), nunca algo que comece com `-` (viraria outra opcao do CLI) nem com espacos/metacaracteres.
"""
from __future__ import annotations

import re
from typing import List, Optional

_SAFE_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/\-\[\]]{0,99}$")
_NO_MODEL = {"", "default", "auto", "padrao"}


def clean_model(value: Optional[str]) -> Optional[str]:
    """O modelo, se for seguro e especifico; None = deixa o CLI usar o padrao dele."""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower() in _NO_MODEL or not _SAFE_MODEL.match(text):
        return None
    return text


def model_args(value: Optional[str], allowed: Optional[List[str]] = None) -> List[str]:
    """`["--model", valor]` ou `[]`. Com `allowed`, so aceita o que o CLI realmente lista."""
    model = clean_model(value)
    if model is None or (allowed is not None and model not in allowed):
        return []
    return ["--model", model]
