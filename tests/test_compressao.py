from __future__ import annotations

import json
import asyncio
from typing import Any

import pytest

from app.compressao import (
    Relatorio,
    compactar,
    estimar_tokens,
    podar_saidas,
    ponto_de_corte_seguro,
)


def _msg(role: str, content: Any = '', **extra: Any) -> dict[str, Any]:
    m: dict[str, Any] = {'role': role, 'content': content}
    m.update(extra)
    return m


def test_estimar_tokens_basico() -> None:
    msgs = [
        _msg('system', 'abcd'),
        _msg('user', 'abcdefgh'),
        _msg('assistant', None),
    ]
    # 4*3 + ceil(4/4) + ceil(8/4) + 0 = 12 + 1 + 2 = 15
    assert estimar_tokens(msgs) == 15


def test_estimar_tokens_com_tool_calls() -> None:
    msgs = [
        _msg('assistant', '', tool_calls=[{'id': 'a', 'type': 'function'}]),
    ]
    base = 4
    json_len = len('[{"id":"a","type":"function"}]')
    esperado = base + (json_len + 3) // 4
    assert estimar_tokens(msgs) == esperado


def test_podar_saidas_poda_longa() -> None:
    longa = 'x' * 4000
    msgs = [
        _msg('tool', longa, tool_call_id='t1'),
        _msg('tool', 'curta', tool_call_id='t2'),
    ]
    out = podar_saidas(msgs, manter_ultimas=0, max_chars=100)
    assert 'caracteres omitidos' in out[0]['content']
    assert out[1]['content'] == 'curta'


def test_podar_saidas_mantem_ultimas() -> None:
    longa = 'y' * 4000
    msgs = [
        _msg('tool', longa, tool_call_id='t1'),
        _msg('tool', longa, tool_call_id='t2'),
    ]
    out = podar_saidas(msgs, manter_ultimas=1, max_chars=100)
    assert 'caracteres omitidos' in out[0]['content']
    assert out[1]['content'] == longa


def test_podar_saidas_nao_poda_erro() -> None:
    erro = 'erro: ' + 'z' * 4000
    tb = 'Traceback (most recent call last):\n' + 'z' * 4000
    msgs = [
        _msg('tool', erro, tool_call_id='t1'),
        _msg('tool', tb, tool_call_id='t2'),
    ]
    out = podar_saidas(msgs, manter_ultimas=0, max_chars=100)
    assert out[0]['content'] == erro
    assert out[1]['content'] == tb


def test_podar_saidas_nao_altera_original() -> None:
    longa = 'w' * 4000
    msgs = [_msg('tool', longa, tool_call_id='t1')]
    _ = podar_saidas(msgs, manter_ultimas=0, max_chars=100)
    assert msgs[0]['content'] == longa


def test_ponto_de_corte_seguro_recua() -> None:
    msgs = [
        _msg('user', 'u'),
        _msg('assistant', '', tool_calls=[{'id': 'a'}]),
        _msg('tool', 'r', tool_call_id='a'),
        _msg('user', 'u2'),
    ]
    assert ponto_de_corte_seguro(msgs, 2) == 1


def test_ponto_de_corte_seguro_limpo() -> None:
    msgs = [
        _msg('user', 'u'),
        _msg('assistant', 'a'),
        _msg('user', 'u2'),
    ]
    assert ponto_de_corte_seguro(msgs, 2) == 2


def test_ponto_de_corte_seguro_zero() -> None:
    msgs = [_msg('user', 'u')]
    assert ponto_de_corte_seguro(msgs, 0) == 0


async def test_compactar_abaixo_do_gatilho() -> None:
    msgs = [_msg('user', 'oi')]
    out, rel = await compactar(msgs, janela_tokens=1000, gatilho=0.5)
    assert out == msgs
    assert out is not msgs
    assert rel.antes == rel.depois
    assert rel.resumidas == 0
    assert rel.usou_resumo is False


async def test_compactar_com_resumo() -> None:
    antigas = [
        _msg('assistant', 'trabalho ' + 'a' * 200),
        _msg('tool', 'resultado ' + 'b' * 200, tool_call_id='x'),
    ]
    msgs = [
        _msg('system', 'sys'),
        _msg('user', 'primeiro'),
        *antigas,
        _msg('user', 'segundo'),
        _msg('assistant', 'ok'),
    ]

    async def resumir(texto: str) -> str:
        return 'resumo curto'

    out, rel = await compactar(
        msgs,
        janela_tokens=100,
        gatilho=0.5,
        manter_ultimas=2,
        resumir=resumir,
    )
    assert rel.usou_resumo is True
    assert rel.resumidas == 2
    users = [m for m in out if m.get('role') == 'user']
    assert [m['content'] for m in users] == ['primeiro', 'segundo']
    assert out[0]['role'] == 'system'
    assert any('[resumo do trabalho anterior]' in (m.get('content') or '') for m in out)
    # sem tool orfa
    pendentes: set[str] = set()
    for m in out:
        if m.get('role') == 'assistant':
            for tc in m.get('tool_calls') or []:
                pendentes.add(tc.get('id'))
        elif m.get('role') == 'tool':
            assert m.get('tool_call_id') in pendentes


async def test_compactar_resumir_levanta_fallback() -> None:
    msgs = [
        _msg('user', 'u'),
        _msg('assistant', 'a' * 500),
        _msg('tool', 'b' * 500, tool_call_id='t'),
        _msg('user', 'u2'),
    ]

    async def resumir(texto: str) -> str:
        raise RuntimeError('boom')

    out, rel = await compactar(
        msgs,
        janela_tokens=50,
        gatilho=0.5,
        manter_ultimas=1,
        resumir=resumir,
    )
    assert rel.usou_resumo is False
    assert any('mensagens antigas omitidas' in (m.get('content') or '') for m in out)


async def test_compactar_resumir_lento_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    msgs = [
        _msg('user', 'u'),
        _msg('assistant', 'a' * 500),
        _msg('tool', 'b' * 500, tool_call_id='t'),
        _msg('user', 'u2'),
    ]

    async def resumir(texto: str) -> str:
        await asyncio.sleep(5)
        return 'ok'

    import app.compressao as mod

    original = mod.asyncio.wait_for

    async def wait_for_fake(coro, timeout):  # type: ignore[no-untyped-def]
        return await original(coro, 0.01)

    monkeypatch.setattr(mod.asyncio, 'wait_for', wait_for_fake)

    out, rel = await compactar(
        msgs,
        janela_tokens=50,
        gatilho=0.5,
        manter_ultimas=1,
        resumir=resumir,
    )
    assert rel.usou_resumo is False
    assert any('mensagens antigas omitidas' in (m.get('content') or '') for m in out)


async def test_compactar_resumo_maior_fallback() -> None:
    msgs = [
        _msg('user', 'u'),
        _msg('assistant', 'a' * 500),
        _msg('tool', 'b' * 500, tool_call_id='t'),
        _msg('user', 'u2'),
    ]

    async def resumir(texto: str) -> str:
        return 'z' * 100000

    out, rel = await compactar(
        msgs,
        janela_tokens=50,
        gatilho=0.5,
        manter_ultimas=1,
        resumir=resumir,
    )
    assert rel.usou_resumo is False
    assert any('mensagens antigas omitidas' in (m.get('content') or '') for m in out)


async def test_compactar_system_preservado() -> None:
    msgs = [
        _msg('system', 'instrucoes'),
        _msg('user', 'u'),
        _msg('assistant', 'a' * 500),
        _msg('tool', 'b' * 500, tool_call_id='t'),
        _msg('user', 'u2'),
    ]

    async def resumir(texto: str) -> str:
        return 'r'

    out, _ = await compactar(
        msgs,
        janela_tokens=50,
        gatilho=0.5,
        manter_ultimas=1,
        resumir=resumir,
    )
    assert out[0]['role'] == 'system'
    assert out[0]['content'] == 'instrucoes'


async def test_compactar_relatorio_antes_depois() -> None:
    msgs = [
        _msg('user', 'u'),
        _msg('assistant', 'a' * 500),
        _msg('tool', 'b' * 500, tool_call_id='t'),
        _msg('user', 'u2'),
    ]

    async def resumir(texto: str) -> str:
        return 'r'

    out, rel = await compactar(
        msgs,
        janela_tokens=50,
        gatilho=0.5,
        manter_ultimas=1,
        resumir=resumir,
    )
    assert isinstance(rel, Relatorio)
    assert rel.antes == estimar_tokens(msgs)
    assert rel.depois == estimar_tokens(out)
    assert rel.depois < rel.antes


def _conversa_aleatoria(rng):
    msgs = [{'role': 'system', 'content': 'regras'}]
    n_id = 0
    for _ in range(rng.randint(3, 25)):
        msgs.append({'role': 'user', 'content': 'pergunta ' + 'u' * rng.randint(1, 800)})
        for _ in range(rng.randint(0, 3)):
            ids = []
            chamadas = []
            for _ in range(rng.randint(1, 3)):
                n_id += 1
                ids.append(f'c{n_id}')
                chamadas.append({'id': f'c{n_id}', 'type': 'function', 'function': {'name': 'ler_arquivo', 'arguments': '{}'}})
            msgs.append({'role': 'assistant', 'content': None, 'tool_calls': chamadas})
            for i in ids:
                msgs.append({'role': 'tool', 'tool_call_id': i, 'content': 'saida ' + 'x' * rng.randint(1, 4000)})
        msgs.append({'role': 'assistant', 'content': 'resposta ' + 'a' * rng.randint(1, 800)})
    return msgs


def _invariantes(original, novas):
    # todo 'tool' tem, antes dele, um assistant com o tool_call correspondente
    vistos = set()
    for m in novas:
        if m['role'] == 'assistant':
            for c in m.get('tool_calls') or []:
                vistos.add(c['id'])
        if m['role'] == 'tool':
            assert m['tool_call_id'] in vistos, 'tool orfa'
    # todas as mensagens do usuario, intactas e na ordem
    assert [m['content'] for m in novas if m['role'] == 'user'] == [m['content'] for m in original if m['role'] == 'user']
    assert novas[0]['role'] == 'system'


@pytest.mark.parametrize('semente', range(300))
async def test_propriedade_sem_tool_orfa_e_usuario_preservado(semente):
    import random
    from app.compressao import compactar, estimar_tokens
    rng = random.Random(semente)
    original = _conversa_aleatoria(rng)
    copia = json.loads(json.dumps(original))
    janela = max(200, estimar_tokens(original) // rng.randint(2, 6))

    async def resumir(texto):
        if rng.random() < 0.3:
            raise RuntimeError('falhou')
        return 'resumo curto'

    novas, _ = await compactar(original, janela_tokens=janela, gatilho=0.5, manter_ultimas=rng.randint(2, 8),
                               resumir=resumir if rng.random() < 0.8 else None)
    assert original == copia          # nao altera a entrada
    _invariantes(original, novas)
