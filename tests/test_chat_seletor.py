# tests/test_chat_seletor.py
'''Testes do seletor de lista cli.chat_seletor.'''

from __future__ import annotations

from io import StringIO

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output.vt100 import Vt100_Output

from cli.chat_seletor import desenhar, selecionar


def _saida() -> Vt100_Output:
    '''Cria um Vt100_Output sobre StringIO simulando um terminal.'''
    destino = StringIO()
    destino.isatty = lambda: True  # type: ignore[method-assign]
    return Vt100_Output(destino, lambda: Size(rows=20, columns=80), term='xterm-256color')


OPCOES = [
    ('m1', 'Modelo 1'),
    ('m2', 'Modelo 2'),
    ('m3', 'Modelo 3'),
    ('outro', 'Outro modelo'),
]


async def test_enter_direto_devolve_inicial() -> None:
    '''(a) Enter direto devolve o item inicial.'''
    with create_pipe_input() as inp:
        inp.send_text('\r')
        resultado = await selecionar(
            'Escolha', OPCOES, entrada=inp, saida=_saida()
        )
    assert resultado == 'm1'


async def test_duas_setas_baixo() -> None:
    '''(b) Duas setas para baixo + Enter devolve o terceiro.'''
    with create_pipe_input() as inp:
        inp.send_text('\x1b[B\x1b[B\r')
        resultado = await selecionar(
            'Escolha', OPCOES, entrada=inp, saida=_saida()
        )
    assert resultado == 'm3'


async def test_filtro() -> None:
    '''(c) Digitar 'm2' + Enter devolve a opcao que contem m2.'''
    with create_pipe_input() as inp:
        inp.send_text('m2\r')
        resultado = await selecionar(
            'Escolha', OPCOES, entrada=inp, saida=_saida()
        )
    assert resultado == 'm2'


async def test_cancelar_ctrl_c() -> None:
    '''(d) Cancelar com Ctrl+C devolve None.'''
    with create_pipe_input() as inp:
        inp.send_text('\x03')
        resultado = await selecionar(
            'Escolha', OPCOES, entrada=inp, saida=_saida()
        )
    assert resultado is None


async def test_atual_define_indice_inicial() -> None:
    '''(e) `atual` define o indice inicial.'''
    with create_pipe_input() as inp:
        inp.send_text('\r')
        resultado = await selecionar(
            'Escolha', OPCOES, atual='m3', entrada=inp, saida=_saida()
        )
    assert resultado == 'm3'


def test_janela_limita_visiveis() -> None:
    '''(f) A janela mostra no maximo `visiveis` itens.'''
    opcoes = [(f'v{i}', f'Rotulo {i}') for i in range(51)]
    linhas = desenhar(opcoes, 25, 10, None)
    itens = [
        (estilo, texto)
        for estilo, texto in linhas
        if not texto.startswith('  \u2191') and not texto.startswith('  \u2193')
    ]
    # Cada item selecionado gera 2 entradas (marcador + texto); os demais 1.
    # Conta apenas as linhas que representam itens (marcador ou recuo).
    contagem = 0
    for estilo, texto in itens:
        if texto.startswith('\u276f ') or texto.startswith('  '):
            contagem += 1
    # O numero de itens desenhados nao pode passar de `visiveis`.
    assert contagem <= 10
    # E deve haver exatamente `visiveis` itens desenhados.
    assert contagem == 10


def test_janela_mostra_indicadores() -> None:
    '''(f) Indicadores de itens acima/abaixo aparecem quando ha overflow.'''
    opcoes = [(f'v{i}', f'Rotulo {i}') for i in range(51)]
    linhas = desenhar(opcoes, 25, 10, None)
    textos = [texto for _estilo, texto in linhas]
    assert any(t.startswith('  \u2191') for t in textos)
    assert any(t.startswith('  \u2193') for t in textos)


async def test_enter_com_filtro_vazio_nao_escolhe() -> None:
    '''(g) Enter com filtro sem resultado nao devolve nada.'''
    with create_pipe_input() as inp:
        inp.send_text('zzzz\r\x03')
        resultado = await selecionar(
            'Escolha', OPCOES, entrada=inp, saida=_saida()
        )
    assert resultado is None


def test_desenhar_separa_os_itens_por_quebra_de_linha() -> None:
    opcoes = [(f'm{i}', f'modelo-{i}') for i in range(51)]
    texto = ''.join(t for _, t in desenhar(opcoes, 25, 10, 'm25'))
    linhas = texto.split('\n')
    assert len(linhas) == 12  # 10 itens + '↑ acima' + '↓ abaixo'
    assert linhas[6].startswith('❯ modelo-25')
    assert 'acima' in linhas[0] and 'abaixo' in linhas[-1]
