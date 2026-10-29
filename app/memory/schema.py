"""Esquema de notas de memoria."""

from __future__ import annotations

import re
from dataclasses import dataclass

TIPOS = ('usuario', 'feedback', 'projeto', 'referencia')

_PADRAO_NOME = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')
# Nomes de dispositivo do Windows: "con.md" abriria o console em vez de um arquivo.
_RESERVADOS_WINDOWS = frozenset({'con', 'prn', 'aux', 'nul',
                                 *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))})


@dataclass(frozen=True)
class Nota:
    """Nota de memoria carregada do disco."""

    nome: str
    descricao: str
    tipo: str
    corpo: str
    data: str


def validar_nome(nome: str) -> None:
    """Valida o nome da nota contra o padrao permitido."""
    if not isinstance(nome, str) or not _PADRAO_NOME.fullmatch(nome) or nome in _RESERVADOS_WINDOWS:
        raise ValueError(f'nome invalido: {nome!r}')


def validar_tipo(tipo: str) -> None:
    """Valida o tipo da nota contra TIPOS."""
    if tipo not in TIPOS:
        raise ValueError(f'tipo invalido: {tipo!r}')
