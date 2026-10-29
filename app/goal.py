from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

MAX_TURNOS_PADRAO = 5
MAX_TURNOS_LIMITE = 10

_ESTADOS_VEREDITO = {'feito', 'continuar', 'bloqueado'}
_ESTADOS_RESULTADO = {'feito', 'bloqueado', 'limite', 'erro'}


@dataclass(frozen=True)
class Veredito:
    '''Resultado da avaliacao do juiz sobre um turno.'''

    estado: str
    motivo: str


@dataclass(frozen=True)
class ResultadoGoal:
    '''Resultado final da persecucao de um objetivo.'''

    estado: str
    turnos: int
    motivo: str
    ultima_resposta: str


def _normalizar_palavra(texto: str) -> str:
    '''Normaliza uma palavra para comparacao: minuscula, sem acento, sem pontuacao.'''

    tabela = str.maketrans(
        'áàâãäéèêëíìîïóòôõöúùûüçÁÀÂÃÄÉÈÊËÍÌÎÏÓÒÔÕÖÚÙÛÜÇ',
        'aaaaaeeeeiiiiooooouuuucAAAAAEEEEIIIIOOOOOUUUUC',
    )
    limpo = texto.translate(tabela).lower()
    return ''.join(c for c in limpo if c.isalnum())


def montar_prompt_juiz(objetivo: str, ultima_resposta: str) -> str:
    '''Monta o prompt enviado ao juiz barato.'''

    resposta = ultima_resposta[:6000]
    return (
        'Voce e um juiz objetivo. Avalie se o objetivo abaixo foi cumprido.\n'
        'Responda na PRIMEIRA linha com exatamente uma palavra: FEITO, CONTINUAR ou BLOQUEADO.\n'
        'Na SEGUNDA linha, escreva o motivo curto.\n'
        'Diga FEITO somente se o objetivo foi comprovadamente cumprido.\n'
        'Diga BLOQUEADO se falta algo que so o usuario pode fornecer.\n'
        'Caso contrario, diga CONTINUAR.\n\n'
        f'Objetivo: {objetivo}\n\n'
        f'Ultima resposta do agente:\n{resposta}\n'
    )


def parse_veredito(texto: str) -> Veredito:
    '''Interpreta a resposta do juiz em um Veredito.'''

    linhas = [linha.strip() for linha in (texto or '').splitlines()]
    linhas_nao_vazias = [linha for linha in linhas if linha]
    if not linhas_nao_vazias:
        return Veredito('continuar', 'veredito ilegivel')

    primeira = linhas_nao_vazias[0]
    partes = primeira.split(maxsplit=1)
    palavra = _normalizar_palavra(partes[0]) if partes else ''

    if palavra in {'feito', 'done', 'concluido'}:
        estado = 'feito'
    elif palavra in {'bloqueado', 'blocked'}:
        estado = 'bloqueado'
    elif palavra in {'continuar', 'continue'}:
        estado = 'continuar'
    else:
        return Veredito('continuar', 'veredito ilegivel')

    if len(linhas_nao_vazias) > 1:
        motivo = linhas_nao_vazias[1]
    elif len(partes) > 1:
        motivo = partes[1]
    else:
        motivo = ''

    return Veredito(estado, motivo[:200])


def prompt_do_turno(
    objetivo: str, numero: int, total: int, motivo_anterior: str = ''
) -> str:
    '''Monta o prompt de um turno do agente.'''

    if numero <= 1:
        return (
            f'Objetivo: {objetivo}. Trabalhe nele de forma autonoma; '
            'ao terminar, diga o que foi feito e como verificar.'
        )
    return (
        f'Continue o objetivo: {objetivo}. (turno {numero} de {total}) '
        f'Pendencia apontada: {motivo_anterior}. Faca o proximo passo concreto.'
    )


async def perseguir_objetivo(
    objetivo: str,
    *,
    turno: Callable[[str], Awaitable[str]],
    julgar: Callable[[str], Awaitable[str]],
    max_turnos: int = MAX_TURNOS_PADRAO,
    escrever: Callable[[str], None] = lambda t: None,
) -> ResultadoGoal:
    '''Executa turnos ate o juiz concluir, respeitando um teto rigido.'''

    if not objetivo or not objetivo.strip():
        return ResultadoGoal('erro', 0, 'objetivo vazio', '')

    total = max(1, min(int(max_turnos), MAX_TURNOS_LIMITE))
    ultima_resposta = ''
    motivo_anterior = ''

    for numero in range(1, total + 1):
        escrever(f'[goal: turno {numero} de {total}]\n')
        prompt = prompt_do_turno(objetivo, numero, total, motivo_anterior)

        try:
            ultima_resposta = await turno(prompt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return ResultadoGoal(
                'erro', numero, f'{type(exc).__name__}: {exc}', ultima_resposta
            )

        try:
            texto_juiz = await julgar(montar_prompt_juiz(objetivo, ultima_resposta))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            return ResultadoGoal(
                'erro', numero, f'{type(exc).__name__}: {exc}', ultima_resposta
            )

        veredito = parse_veredito(texto_juiz)
        escrever(f'[goal: {veredito.estado} - {veredito.motivo}]\n')

        if veredito.estado == 'feito':
            return ResultadoGoal('feito', numero, veredito.motivo, ultima_resposta)
        if veredito.estado == 'bloqueado':
            return ResultadoGoal('bloqueado', numero, veredito.motivo, ultima_resposta)

        motivo_anterior = veredito.motivo

    return ResultadoGoal('limite', total, 'limite de turnos atingido', ultima_resposta)
