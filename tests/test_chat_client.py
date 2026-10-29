from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from cli.chat_client import ClienteHera, ErroDoServidor, ler_token


def _sse(*eventos: str) -> bytes:
    partes = []
    for evento in eventos:
        partes.append(f'data: {evento}\n\n')
    return ''.join(partes)


def _delta(conteudo: str) -> str:
    return json.dumps({'choices': [{'delta': {'content': conteudo}}]})


async def _coletar(cliente: ClienteHera, mensagens: list[dict]) -> list[str]:
    pedacos = []
    async for pedaco in cliente.conversar(mensagens):
        pedacos.append(pedaco)
    return pedacos


async def test_sse_normal_monta_ola_mundo() -> None:
    corpo = _sse(_delta('ola'), _delta(' '), _delta('mundo'), '[DONE]')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=corpo)

    cliente = ClienteHera(
        'http://teste', 'tok', transporte=httpx.MockTransport(handler)
    )
    pedacos = await _coletar(cliente, [{'role': 'user', 'content': 'oi'}])
    assert ''.join(pedacos) == 'ola mundo'


async def test_deltas_vazios_e_linhas_invalidas_ignorados() -> None:
    corpo = (
        'data: \n\n'
        'data: nao-e-json\n\n'
        'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":""}}]}\n\n'
        + _sse(_delta('ok'))
        + 'data: [DONE]\n\n'
    ).encode('utf-8')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=corpo)

    cliente = ClienteHera(
        'http://teste', 'tok', transporte=httpx.MockTransport(handler)
    )
    pedacos = await _coletar(cliente, [{'role': 'user', 'content': 'oi'}])
    assert pedacos == ['ok']


async def test_done_encerra_mesmo_com_lixo_depois() -> None:
    corpo = (
        _sse(_delta('a'))
        + 'data: [DONE]\n\n'
        + 'data: {"choices":[{"delta":{"content":"b"}}]}\n\n'
    ).encode('utf-8')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=corpo)

    cliente = ClienteHera(
        'http://teste', 'tok', transporte=httpx.MockTransport(handler)
    )
    pedacos = await _coletar(cliente, [{'role': 'user', 'content': 'oi'}])
    assert pedacos == ['a']


async def test_erro_401_com_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={'detail': 'x'})

    cliente = ClienteHera(
        'http://teste', 'tok', transporte=httpx.MockTransport(handler)
    )
    with pytest.raises(ErroDoServidor) as info:
        await _coletar(cliente, [{'role': 'user', 'content': 'oi'}])
    assert info.value.status == 401
    assert info.value.mensagem == 'x'


async def test_erro_503_texto_cru() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text='indisponivel')

    cliente = ClienteHera(
        'http://teste', 'tok', transporte=httpx.MockTransport(handler)
    )
    with pytest.raises(ErroDoServidor) as info:
        await _coletar(cliente, [{'role': 'user', 'content': 'oi'}])
    assert info.value.status == 503
    assert info.value.mensagem == 'indisponivel'


async def test_connect_error_vira_status_zero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('sem rota')

    cliente = ClienteHera(
        'http://teste', 'tok', transporte=httpx.MockTransport(handler)
    )
    with pytest.raises(ErroDoServidor) as info:
        await _coletar(cliente, [{'role': 'user', 'content': 'oi'}])
    assert info.value.status == 0
    assert 'fora do ar' in info.value.mensagem


async def test_cabecalho_e_corpo_corretos() -> None:
    capturado: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        capturado['auth'] = request.headers.get('Authorization')
        capturado['corpo'] = json.loads(request.content.decode('utf-8'))
        return httpx.Response(200, content=_sse(_delta('ok'), '[DONE]'))

    cliente = ClienteHera(
        'http://teste', 'segredo', 'meu-modelo',
        transporte=httpx.MockTransport(handler),
    )
    await _coletar(cliente, [{'role': 'user', 'content': 'oi'}])
    assert capturado['auth'] == 'Bearer segredo'
    assert capturado['corpo']['model'] == 'meu-modelo'
    assert capturado['corpo']['stream'] is True
    assert capturado['corpo']['messages'] == [{'role': 'user', 'content': 'oi'}]


def test_ler_token_do_env() -> None:
    token = ler_token(Path('/inexistente'), environ={'HERA_API_KEY': 'abc'})
    assert token == 'abc'


def test_ler_token_do_arquivo(tmp_path: Path) -> None:
    pasta = tmp_path / 'data'
    pasta.mkdir()
    (pasta / '.hera_token').write_text('  do-arquivo \n', encoding='utf-8')
    token = ler_token(tmp_path, environ={})
    assert token == 'do-arquivo'


def test_ler_token_ausente(tmp_path: Path) -> None:
    with pytest.raises(ErroDoServidor) as info:
        ler_token(tmp_path, environ={})
    assert info.value.status == 0
    assert 'Token do Hera nao encontrado' in info.value.mensagem
