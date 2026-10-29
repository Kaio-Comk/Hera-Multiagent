from __future__ import annotations

import json

import httpx
import pytest

from cli.chat_client import ClienteHera, ErroDoServidor
from cli.chat_ferramentas import (
    ChamadaFerramenta,
    Evento,
    conversar_com_ferramentas,
    detectar_chamada_em_texto,
    mensagem_assistente,
    mensagem_resultado,
)


def _sse(*chunks: dict) -> bytes:
    '''Monta corpo SSE a partir de chunks.'''
    partes = []
    for c in chunks:
        partes.append(f'data: {json.dumps(c)}\n\n')
    partes.append('data: [DONE]\n\n')
    return ''.join(partes).encode('utf-8')


def _chunk(delta: dict, finish: str | None = None) -> dict:
    return {'choices': [{'delta': delta, 'finish_reason': finish}]}


def _cliente(transport: httpx.AsyncBaseTransport) -> ClienteHera:
    return ClienteHera(
        base_url='http://hera.local',
        token='tok',
        modelo='m1',
        transporte=transport,
        timeout=5.0,
    )


async def _coletar(cliente, mensagens, ferramentas, **kw) -> list[Evento]:
    eventos = []
    async for ev in conversar_com_ferramentas(cliente, mensagens, ferramentas, **kw):
        eventos.append(ev)
    return eventos


async def test_somente_texto():
    corpo = _sse(
        _chunk({'content': 'Ola'}),
        _chunk({'content': ' mundo'}),
        _chunk({}, 'stop'),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=corpo)

    cliente = _cliente(httpx.MockTransport(handler))
    eventos = await _coletar(cliente, [{'role': 'user', 'content': 'oi'}], [])
    textos = [e.texto for e in eventos if e.tipo == 'texto']
    assert textos == ['Ola', ' mundo']
    assert eventos[-1].tipo == 'fim'
    assert eventos[-1].fim == 'stop'
    assert all(e.tipo != 'chamada' for e in eventos)


async def test_texto_e_chamada_argumentos_fatiados():
    corpo = _sse(
        _chunk({'content': 'vou chamar'}),
        _chunk({'tool_calls': [{'index': 0, 'id': 'call_a', 'function': {'name': 'somar', 'arguments': '{"a"'}}]}),
        _chunk({'tool_calls': [{'index': 0, 'function': {'arguments': ': 1, '}}]}),
        _chunk({'tool_calls': [{'index': 0, 'function': {'arguments': '"b": 2}'}}]}),
        _chunk({}, 'tool_calls'),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=corpo)

    cliente = _cliente(httpx.MockTransport(handler))
    eventos = await _coletar(cliente, [], [])
    chamadas = [e.chamada for e in eventos if e.tipo == 'chamada']
    assert len(chamadas) == 1
    assert chamadas[0].id == 'call_a'
    assert chamadas[0].nome == 'somar'
    assert chamadas[0].argumentos == '{"a": 1, "b": 2}'
    assert chamadas[0].argumentos_json == {'a': 1, 'b': 2}
    assert eventos[-1].fim == 'tool_calls'


async def test_duas_chamadas_intercaladas():
    corpo = _sse(
        _chunk({'tool_calls': [{'index': 0, 'id': 'c0', 'function': {'name': 'f0', 'arguments': '{"x"'}}]}),
        _chunk({'tool_calls': [{'index': 1, 'id': 'c1', 'function': {'name': 'f1', 'arguments': '{"y"'}}]}),
        _chunk({'tool_calls': [{'index': 0, 'function': {'arguments': ': 1}'}}]}),
        _chunk({'tool_calls': [{'index': 1, 'function': {'arguments': ': 2}'}}]}),
        _chunk({}, 'tool_calls'),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=corpo)

    cliente = _cliente(httpx.MockTransport(handler))
    eventos = await _coletar(cliente, [], [])
    chamadas = [e.chamada for e in eventos if e.tipo == 'chamada']
    assert [c.id for c in chamadas] == ['c0', 'c1']
    assert chamadas[0].argumentos == '{"x": 1}'
    assert chamadas[1].argumentos == '{"y": 2}'


async def test_id_ausente_gera_call_indice():
    corpo = _sse(
        _chunk({'tool_calls': [{'index': 2, 'function': {'name': 'ping', 'arguments': '{}'}}]}),
        _chunk({}, 'tool_calls'),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=corpo)

    cliente = _cliente(httpx.MockTransport(handler))
    eventos = await _coletar(cliente, [], [])
    chamadas = [e.chamada for e in eventos if e.tipo == 'chamada']
    assert len(chamadas) == 1
    assert chamadas[0].id == 'call_2'


async def test_finish_reason_length():
    corpo = _sse(_chunk({'content': 'x'}, 'length'))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=corpo)

    cliente = _cliente(httpx.MockTransport(handler))
    eventos = await _coletar(cliente, [], [])
    assert eventos[-1].tipo == 'fim'
    assert eventos[-1].fim == 'length'


async def test_erro_401_com_detail():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={'detail': 'x'})

    cliente = _cliente(httpx.MockTransport(handler))
    with pytest.raises(ErroDoServidor) as exc:
        await _coletar(cliente, [], [])
    assert exc.value.status == 401
    assert exc.value.mensagem == 'x'


async def test_connect_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('boom')

    cliente = _cliente(httpx.MockTransport(handler))
    with pytest.raises(ErroDoServidor) as exc:
        await _coletar(cliente, [], [])
    assert exc.value.status == 0
    assert 'fora do ar' in exc.value.mensagem
    assert 'http://hera.local' in exc.value.mensagem


async def test_corpo_e_cabecalhos_enviados():
    capturado: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        capturado['headers'] = dict(request.headers)
        capturado['body'] = json.loads(request.content)
        return httpx.Response(200, content=_sse(_chunk({}, 'stop')))

    cliente = _cliente(httpx.MockTransport(handler))
    ferramentas = [{'type': 'function', 'function': {'name': 'f'}}]
    await _coletar(cliente, [{'role': 'user', 'content': 'oi'}], ferramentas, max_tokens=64)
    assert capturado['headers']['authorization'] == 'Bearer tok'
    body = capturado['body']
    assert body['model'] == 'm1'
    assert body['stream'] is True
    assert body['tool_choice'] == 'auto'
    assert body['tools'] == ferramentas
    assert body['max_tokens'] == 64


async def test_linhas_invalidas_ignoradas():
    corpo = (
        'event: ping\n\n'
        'data: {nao json}\n\n'
        'data: {"choices": []}\n\n'
        'data: {"choices": [{"delta": {"content": "ok"}, "finish_reason": null}]}\n\n'
        'data: [DONE]\n\n'
    ).encode('utf-8')

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=corpo)

    cliente = _cliente(httpx.MockTransport(handler))
    eventos = await _coletar(cliente, [], [])
    textos = [e.texto for e in eventos if e.tipo == 'texto']
    assert textos == ['ok']
    assert eventos[-1].fim == 'stop'


async def test_fluxo_sem_finish_reason():
    corpo = _sse(_chunk({'content': 'a'}))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=corpo)

    cliente = _cliente(httpx.MockTransport(handler))
    eventos = await _coletar(cliente, [], [])
    assert eventos[-1].tipo == 'fim'
    assert eventos[-1].fim == 'stop'


async def test_fluxo_sem_finish_com_chamada():
    corpo = _sse(
        _chunk({'tool_calls': [{'index': 0, 'id': 'c', 'function': {'name': 'f', 'arguments': '{}'}}]}),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=corpo)

    cliente = _cliente(httpx.MockTransport(handler))
    eventos = await _coletar(cliente, [], [])
    assert eventos[-1].fim == 'tool_calls'


async def test_chamada_sem_nome_descartada():
    corpo = _sse(
        _chunk({'tool_calls': [{'index': 0, 'id': 'c', 'function': {'arguments': '{}'}}]}),
        _chunk({}, 'tool_calls'),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=corpo)

    cliente = _cliente(httpx.MockTransport(handler))
    eventos = await _coletar(cliente, [], [])
    assert all(e.tipo != 'chamada' for e in eventos)


def test_como_dict_e_argumentos_json():
    c = ChamadaFerramenta(id='c1', nome='f', argumentos='{"a": 1}')
    assert c.como_dict() == {
        'id': 'c1',
        'type': 'function',
        'function': {'name': 'f', 'arguments': '{"a": 1}'},
    }
    assert c.argumentos_json == {'a': 1}
    assert c.argumentos_validos is True

    invalido = ChamadaFerramenta(id='c2', nome='f', argumentos='{nao}')
    assert invalido.argumentos_json == {}
    assert invalido.argumentos_validos is False

    nao_dict = ChamadaFerramenta(id='c3', nome='f', argumentos='[1, 2]')
    assert nao_dict.argumentos_json == {}
    assert nao_dict.argumentos_validos is False


def test_detectar_bloco_json():
    texto = 'pensando...\n```json\n{"name": "somar", "arguments": {"a": 1}}\n```\nfim'
    c = detectar_chamada_em_texto(texto)
    assert c is not None
    assert c.nome == 'somar'
    assert c.argumentos_json == {'a': 1}


def test_detectar_bloco_tool():
    texto = '```tool\n{"tool": "buscar", "args": {"q": "x"}}\n```'
    c = detectar_chamada_em_texto(texto)
    assert c is not None
    assert c.nome == 'buscar'
    assert c.argumentos_json == {'q': 'x'}


def test_detectar_objeto_solto():
    texto = 'vou usar {"name": "ping", "arguments": {}} agora'
    c = detectar_chamada_em_texto(texto)
    assert c is not None
    assert c.nome == 'ping'
    assert c.argumentos == '{}'


def test_detectar_args_como_string():
    texto = '{"function": "f", "parameters": "{\\"k\\": 1}"}'
    c = detectar_chamada_em_texto(texto)
    assert c is not None
    assert c.nome == 'f'
    assert c.argumentos_json == {'k': 1}


def test_detectar_texto_comum_none():
    assert detectar_chamada_em_texto('apenas uma resposta normal') is None


def test_detectar_json_quebrado_none():
    assert detectar_chamada_em_texto('```json\n{"name": "f", \n```') is None
    assert detectar_chamada_em_texto('{"name": "f"') is None


def test_mensagem_assistente():
    c = ChamadaFerramenta(id='c1', nome='f', argumentos='{}')
    msg = mensagem_assistente('ola', [c])
    assert msg['role'] == 'assistant'
    assert msg['content'] == 'ola'
    assert msg['tool_calls'] == [c.como_dict()]

    vazio = mensagem_assistente('', [])
    assert vazio['content'] is None
    assert vazio['tool_calls'] == []


def test_mensagem_resultado():
    c = ChamadaFerramenta(id='c1', nome='f', argumentos='{}')
    msg = mensagem_resultado(c, 'ok')
    assert msg == {'role': 'tool', 'tool_call_id': 'c1', 'content': 'ok'}


SIG = {'google': {'thought_signature': 'ASSINATURA-OPACA'}}


async def test_assinatura_do_gemini_e_guardada_e_reenviada():
    """Regressao: o Gemini 3 recusa (HTTP 400) a conversa se o extra_content da chamada nao voltar na mensagem do assistente."""
    corpo = _sse(
        _chunk({'tool_calls': [{'index': 0, 'id': 'c1', 'type': 'function', 'function': {'name': 'ler_arquivo', 'arguments': '{"cami'},
                                'extra_content': SIG}]}),
        _chunk({'tool_calls': [{'index': 0, 'function': {'arguments': 'nho":"a.py"}'}}]}, finish='tool_calls'),
    )
    cliente = _cliente(httpx.MockTransport(lambda req: httpx.Response(200, content=corpo)))
    eventos = await _coletar(cliente, [{'role': 'user', 'content': 'x'}], [])
    [chamada] = [e.chamada for e in eventos if e.tipo == 'chamada']
    assert chamada.extra == SIG
    assert chamada.argumentos_json == {'caminho': 'a.py'}
    assert mensagem_assistente('', [chamada])['tool_calls'][0]['extra_content'] == SIG


def test_chamada_sem_assinatura_nao_leva_extra_content():
    chamada = ChamadaFerramenta(id='c1', nome='f', argumentos='{}')
    assert chamada.extra is None and 'extra_content' not in chamada.como_dict()
