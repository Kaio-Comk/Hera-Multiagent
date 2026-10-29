from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

CABECALHO = '## Memoria do projeto (contexto de sessoes anteriores; pode estar desatualizado)\n\n'


@dataclass(frozen=True)
class Snapshot:
    texto: str
    origens: tuple[str, ...]
    truncado: bool


def estimar_tokens(texto: str) -> int:
    '''Estima tokens como ceil(len(texto)/4); aproximacao grosseira.'''
    return math.ceil(len(texto) / 4)


def _cabe_nota(texto: str, max_chars: int, max_tokens: int) -> bool:
    return len(texto) <= max_chars and estimar_tokens(texto) <= max_tokens


def montar_snapshot(
    index: Any,
    consulta: str,
    *,
    ativo: bool = True,
    max_chars: int = 2200,
    max_tokens: int = 800,
    max_notas: int = 5,
) -> Snapshot:
    '''Monta um snapshot de memoria respeitando tetos de chars e tokens.'''
    if not ativo:
        return Snapshot('', (), False)
    if max_chars <= 0 or max_tokens <= 0:
        return Snapshot('', (), False)

    resultados = index.buscar(consulta, limite=max_notas)
    if not resultados:
        return Snapshot('', (), False)

    if not _cabe_nota(CABECALHO, max_chars, max_tokens):
        return Snapshot('', (), True)

    texto = CABECALHO
    origens: list[str] = []
    truncado = False

    for resultado in resultados:
        try:
            nota = index.store.get(resultado.nome)
        except FileNotFoundError:
            continue

        cabecalho = f'### {nota.nome} [{nota.tipo}, {nota.data}]\n'
        corpo = nota.corpo
        bloco = cabecalho + corpo + '\n\n'

        if _cabe_nota(texto + bloco, max_chars, max_tokens):
            texto += bloco
            origens.append(f'{nota.nome} ({nota.data})')
            continue

        if not _cabe_nota(texto + cabecalho, max_chars, max_tokens):
            truncado = True
            break

        restante_chars = max_chars - len(texto) - len(cabecalho) - len('\n\n')
        restante_tokens = max_tokens - estimar_tokens(texto + cabecalho + '\n\n')
        limite_por_tokens = restante_tokens * 4

        limite = min(restante_chars, limite_por_tokens)
        if limite <= 0:
            truncado = True
            break

        corpo_cortado = corpo[:limite]
        while corpo_cortado and not _cabe_nota(
            texto + cabecalho + corpo_cortado + '...\n\n', max_chars, max_tokens
        ):
            corpo_cortado = corpo_cortado[:-1]

        if not corpo_cortado:
            truncado = True
            break

        texto += cabecalho + corpo_cortado + '...\n\n'
        origens.append(f'{nota.nome} ({nota.data})')
        truncado = True
        break

    if not origens:
        return Snapshot('', (), truncado)
    return Snapshot(texto, tuple(origens), truncado)
