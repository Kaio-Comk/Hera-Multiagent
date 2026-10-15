"""Qual pasta de projeto o pedido do usuário cita?

O planejador roda com a pasta do Hera como diretório de trabalho; sem esta ajuda ele "adivinhava" que qualquer projeto era o
próprio Hera (pedido: "reorganize o nethub" mexeu no Hera). Aqui o objetivo é comparado com o NOME das pastas liberadas em
`general.allowed_repo_roots` (ex.: "nethub" -> B:/NetHub BR e B:/NetHub ES).
"""
from __future__ import annotations

import os
import re
from typing import List


def _words(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def match_project_folders(goal: str, roots: List[str], hera_root: str = "") -> List[str]:
    """Pastas liberadas que o objetivo cita pelo nome. A pasta do próprio Hera só entra se o pedido falar do Hera:
    o espaço de trabalho padrão NÃO é o projeto do usuário."""
    text = f" {_words(goal)} "
    found: List[str] = []
    for root in roots:
        root = str(root)
        base = _words(os.path.basename(root.rstrip("\\/")))
        if not base:
            continue
        keys = {base}
        first = base.split()[0]
        if len(first) >= 4:
            keys.add(first)
        if hera_root and os.path.normcase(os.path.abspath(root)) == os.path.normcase(os.path.abspath(hera_root)):
            keys |= {"hera", "orquestrador"}
        if any(f" {k} " in text for k in keys) and root not in found:
            found.append(root)
    return found
