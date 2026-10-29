from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Sequence

Mensagem = dict[str, Any]
Resumidor = Callable[[str], Awaitable[str]]


def _texto_content(msg: Mensagem) -> str:
    c = msg.get('content')
    if c is None:
        return ''
    if isinstance(c, str):
        return c
    return str(c)


def _tokens_texto(texto: str) -> int:
    if not texto:
        return 0
    return math.ceil(len(texto) / 4)


def estimar_tokens(mensagens: Sequence[Mensagem]) -> int:
    total = 0
    for msg in mensagens:
        total += 4
        total += _tokens_texto(_texto_content(msg))
        tcs = msg.get('tool_calls')
        if tcs:
            try:
                bruto = json.dumps(tcs, ensure_ascii=False, separators=(',', ':'))
            except (TypeError, ValueError):
                bruto = str(tcs)
            total += _tokens_texto(bruto)
    return total


def _podar_conteudo(texto: str, max_chars: int) -> str:
    if len(texto) <= max_chars:
        return texto
    metade = max_chars // 2
    inicio = texto[:metade]
    fim = texto[-metade:] if metade > 0 else ''
    omitidos = len(texto) - len(inicio) - len(fim)
    return f'{inicio}\n[... {omitidos} caracteres omitidos ...]\n{fim}'


def _eh_erro(texto: str) -> bool:
    if texto.startswith('erro'):
        return True
    return 'Traceback' in texto


def podar_saidas(
    mensagens: Sequence[Mensagem],
    *,
    manter_ultimas: int = 4,
    max_chars: int = 1500,
) -> list[Mensagem]:
    copia: list[Mensagem] = [dict(m) for m in mensagens]
    indices_tool = [i for i, m in enumerate(copia) if m.get('role') == 'tool']
    if manter_ultimas > 0:
        protegidos = set(indices_tool[-manter_ultimas:])
    else:
        protegidos = set()
    for i in indices_tool:
        if i in protegidos:
            continue
        msg = copia[i]
        texto = _texto_content(msg)
        if len(texto) <= max_chars:
            continue
        if _eh_erro(texto):
            continue
        msg['content'] = _podar_conteudo(texto, max_chars)
    return copia


def ponto_de_corte_seguro(mensagens: Sequence[Mensagem], indice: int) -> int:
    n = len(mensagens)
    if indice <= 0:
        return 0
    if indice >= n:
        return n
    i = indice
    while i > 0 and mensagens[i].get('role') == 'tool':
        i -= 1
    return i


def _ids_tool_calls(msg: Mensagem) -> set[str]:
    ids: set[str] = set()
    tcs = msg.get('tool_calls')
    if isinstance(tcs, list):
        for tc in tcs:
            if isinstance(tc, dict):
                tid = tc.get('id')
                if isinstance(tid, str):
                    ids.add(tid)
    return ids


def _sem_tool_orfa(mensagens: Sequence[Mensagem]) -> bool:
    pendentes: set[str] = set()
    for msg in mensagens:
        role = msg.get('role')
        if role == 'assistant':
            pendentes |= _ids_tool_calls(msg)
        elif role == 'tool':
            tid = msg.get('tool_call_id')
            if isinstance(tid, str) and tid in pendentes:
                pendentes.discard(tid)
            else:
                return False
    return True


def _serializar_para_resumo(mensagens: Sequence[Mensagem]) -> str:
    partes: list[str] = []
    for msg in mensagens:
        role = msg.get('role', '?')
        texto = _texto_content(msg)
        tcs = msg.get('tool_calls')
        if tcs:
            try:
                extra = json.dumps(tcs, ensure_ascii=False, separators=(',', ':'))
            except (TypeError, ValueError):
                extra = str(tcs)
            texto = f'{texto}\n[tool_calls] {extra}'.strip()
        partes.append(f'[{role}] {texto}')
    return '\n'.join(partes)


@dataclass(frozen=True)
class Relatorio:
    antes: int
    depois: int
    resumidas: int
    usou_resumo: bool
    avisos: tuple[str, ...]


def _copia(mensagens: Sequence[Mensagem]) -> list[Mensagem]:
    return [dict(m) for m in mensagens]


def _fallback_omitir(antigas: Sequence[Mensagem]) -> Mensagem:
    n = len(antigas)
    return {
        'role': 'assistant',
        'content': f'[{n} mensagens antigas omitidas]',
    }


async def _tentar_resumir(
    resumir: Resumidor | None,
    texto: str,
    original_tokens: int,
    timeout: float = 30.0,
) -> tuple[str | None, tuple[str, ...]]:
    avisos: list[str] = []
    if resumir is None:
        avisos.append('resumidor ausente')
        return None, tuple(avisos)
    try:
        resultado = await asyncio.wait_for(resumir(texto), timeout=timeout)
    except asyncio.TimeoutError:
        avisos.append('resumidor excedeu o tempo limite')
        return None, tuple(avisos)
    except Exception as exc:  # noqa: BLE001
        avisos.append(f'resumidor falhou: {type(exc).__name__}')
        return None, tuple(avisos)
    if not isinstance(resultado, str) or not resultado.strip():
        avisos.append('resumo vazio')
        return None, tuple(avisos)
    if _tokens_texto(resultado) > original_tokens:
        avisos.append('resumo maior que o original')
        return None, tuple(avisos)
    return resultado, tuple(avisos)


async def compactar(
    mensagens: Sequence[Mensagem],
    *,
    janela_tokens: int,
    gatilho: float = 0.5,
    manter_ultimas: int = 6,
    resumir: Resumidor | None = None,
) -> tuple[list[Mensagem], Relatorio]:
    antes = estimar_tokens(mensagens)
    limite = gatilho * janela_tokens
    if antes <= limite:
        return _copia(mensagens), Relatorio(
            antes=antes,
            depois=antes,
            resumidas=0,
            usou_resumo=False,
            avisos=(),
        )

    podadas = podar_saidas(mensagens, manter_ultimas=manter_ultimas)
    tokens_podadas = estimar_tokens(podadas)
    if tokens_podadas <= limite:
        return podadas, Relatorio(
            antes=antes,
            depois=tokens_podadas,
            resumidas=0,
            usou_resumo=False,
            avisos=(),
        )

    n = len(podadas)
    corte = n - manter_ultimas if manter_ultimas > 0 else n
    if corte < 0:
        corte = 0
    corte = ponto_de_corte_seguro(podadas, corte)

    antigas: list[Mensagem] = []
    mantidas: list[Mensagem] = []
    for i, msg in enumerate(podadas):
        role = msg.get('role')
        if i < corte and role in ('assistant', 'tool'):
            antigas.append(msg)
        else:
            mantidas.append(msg)

    if not antigas:
        return podadas, Relatorio(
            antes=antes,
            depois=tokens_podadas,
            resumidas=0,
            usou_resumo=False,
            avisos=('nada elegivel para resumir',),
        )

    texto_antigo = _serializar_para_resumo(antigas)
    tokens_antigos = _tokens_texto(texto_antigo)
    resumo, avisos = await _tentar_resumir(resumir, texto_antigo, tokens_antigos)

    if resumo is not None:
        msg_resumo: Mensagem = {
            'role': 'assistant',
            'content': f'[resumo do trabalho anterior]\n{resumo}',
        }
        usou_resumo = True
    else:
        msg_resumo = _fallback_omitir(antigas)
        usou_resumo = False

    resultado: list[Mensagem] = []
    inserido = False
    for msg in mantidas:
        if not inserido and msg.get('role') == 'user':
            resultado.append(msg_resumo)
            inserido = True
        resultado.append(msg)
    if not inserido:
        resultado.insert(0, msg_resumo)

    if not _sem_tool_orfa(resultado):
        resultado = [m for m in resultado if m.get('role') != 'tool']
        if not _sem_tool_orfa(resultado):
            resultado = [m for m in resultado if m.get('role') != 'assistant' or not m.get('tool_calls')]

    depois = estimar_tokens(resultado)
    return resultado, Relatorio(
        antes=antes,
        depois=depois,
        resumidas=len(antigas),
        usou_resumo=usou_resumo,
        avisos=avisos,
    )
