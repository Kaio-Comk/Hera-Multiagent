# tests/test_chat_markdown.py
'''Testes do renderizador de Markdown linha a linha.'''

import re

from cli.chat_markdown import Renderizador


def visivel(t: str) -> str:
    '''Remove escapes ANSI para comparar o texto visivel.'''
    return re.sub(r'\x1b\[[0-9;]*m', '', t)


# --- cor=False: identidade -------------------------------------------------

def test_sem_cor_identidade():
    r = Renderizador(cor=False)
    for linha in [
        '# Titulo',
        'texto **negrito** e `codigo`',
        '```python',
        'print(1)',
        '```',
        '- item',
        '> citacao',
        '---',
    ]:
        assert r.linha(linha) == linha


def test_sem_cor_nao_muda_estado():
    r = Renderizador(cor=False)
    r.linha('```python')
    assert r.linha('print(1)') == 'print(1)'
    r.reiniciar()
    assert r.linha('print(1)') == 'print(1)'


# --- Cerca de codigo -------------------------------------------------------

def test_cerca_abre_fecha_com_linguagem():
    r = Renderizador(cor=True)
    abertura = r.linha('```python')
    assert visivel(abertura) == '╌╌ python'
    assert abertura.endswith('\x1b[0m')

    dentro = r.linha('print(1)')
    assert visivel(dentro) == '│ print(1)'
    assert dentro.endswith('\x1b[0m')

    fechamento = r.linha('```')
    assert visivel(fechamento) == '╌╌'
    assert fechamento.endswith('\x1b[0m')


def test_cerca_sem_linguagem():
    r = Renderizador(cor=True)
    abertura = r.linha('```')
    assert visivel(abertura) == '╌╌'


def test_cerca_com_til():
    r = Renderizador(cor=True)
    abertura = r.linha('~~~')
    assert visivel(abertura) == '╌╌'
    dentro = r.linha('texto')
    assert visivel(dentro) == '│ texto'
    fechamento = r.linha('~~~')
    assert visivel(fechamento) == '╌╌'


def test_dentro_de_bloco_nao_interpreta_markdown():
    r = Renderizador(cor=True)
    r.linha('```')
    saida = r.linha('# nao e titulo')
    assert visivel(saida) == '│ # nao e titulo'
    # Nao deve conter o prefixo de titulo
    assert '\x1b[1;38;5;37m' not in saida


def test_comentario_python_dentro_de_bloco_nao_vira_titulo():
    r = Renderizador(cor=True)
    r.linha('```python')
    saida = r.linha('# comentario')
    assert visivel(saida) == '│ # comentario'
    assert '\x1b[1;38;5;37m' not in saida


# --- Titulos ---------------------------------------------------------------

def test_titulo_h1():
    r = Renderizador(cor=True)
    saida = r.linha('# Ola')
    assert visivel(saida) == 'Ola'
    assert saida.startswith('\x1b[1;38;5;37m')
    assert saida.endswith('\x1b[0m')


def test_titulo_h2():
    r = Renderizador(cor=True)
    saida = r.linha('## Ola')
    assert visivel(saida) == 'Ola'
    assert saida.startswith('\x1b[1;38;5;37m')


def test_titulo_h3_ate_h6():
    r = Renderizador(cor=True)
    for n in range(3, 7):
        saida = r.linha('#' * n + ' Ola')
        assert visivel(saida) == 'Ola'
        assert saida.startswith('\x1b[1m')
        assert saida.endswith('\x1b[0m')


def test_titulo_com_recuo():
    r = Renderizador(cor=True)
    saida = r.linha('   # Ola')
    assert visivel(saida) == 'Ola'
    assert saida.startswith('\x1b[1;38;5;37m')


def test_titulo_com_codigo_inline_reabre_estilo():
    r = Renderizador(cor=True)
    saida = r.linha('# veja `codigo` aqui')
    assert visivel(saida) == 'veja codigo aqui'
    # Deve reabrir o prefixo do titulo apos o codigo
    assert '\x1b[38;5;180m' in saida
    # Apos o reset do codigo, deve vir o prefixo do titulo de novo
    assert '\x1b[0m\x1b[1;38;5;37m' in saida


# --- Listas ----------------------------------------------------------------

def test_lista_marcador_vira_bullet():
    r = Renderizador(cor=True)
    for marcador in ['-', '*', '+']:
        saida = r.linha(marcador + ' item')
        assert visivel(saida) == '• item'


def test_lista_com_recuo():
    r = Renderizador(cor=True)
    saida = r.linha('  - item')
    assert visivel(saida) == '  • item'


def test_lista_numerada_mantem_numero():
    r = Renderizador(cor=True)
    saida = r.linha('1. item')
    assert visivel(saida) == '1. item'


def test_lista_com_negrito():
    r = Renderizador(cor=True)
    saida = r.linha('- **forte**')
    assert visivel(saida) == '• forte'
    assert '\x1b[1m' in saida


# --- Citacao ---------------------------------------------------------------

def test_citacao():
    r = Renderizador(cor=True)
    saida = r.linha('> texto')
    assert visivel(saida) == '▎ texto'
    assert '\x1b[3m' in saida
    assert saida.endswith('\x1b[0m')


# --- Regua horizontal ------------------------------------------------------

def test_regua_horizontal():
    r = Renderizador(cor=True)
    for marca in ['---', '***', '___', '-----']:
        saida = r.linha(marca)
        assert visivel(saida) == '─' * 40
        assert saida.startswith('\x1b[2m')
        assert saida.endswith('\x1b[0m')


# --- Inline ----------------------------------------------------------------

def test_codigo_inline():
    r = Renderizador(cor=True)
    saida = r.linha('use `print(1)` agora')
    assert visivel(saida) == 'use print(1) agora'
    assert '\x1b[38;5;180m' in saida


def test_negrito_asterisco():
    r = Renderizador(cor=True)
    saida = r.linha('**forte**')
    assert visivel(saida) == 'forte'
    assert '\x1b[1m' in saida


def test_negrito_underscore():
    r = Renderizador(cor=True)
    saida = r.linha('__forte__')
    assert visivel(saida) == 'forte'
    assert '\x1b[1m' in saida


def test_italico_asterisco():
    r = Renderizador(cor=True)
    saida = r.linha('*leve*')
    assert visivel(saida) == 'leve'
    assert '\x1b[3m' in saida


def test_italico_underscore():
    r = Renderizador(cor=True)
    saida = r.linha('_leve_')
    assert visivel(saida) == 'leve'
    assert '\x1b[3m' in saida


def test_asterisco_solto_literal():
    r = Renderizador(cor=True)
    saida = r.linha('a * b')
    assert visivel(saida) == 'a * b'


def test_negrito_sem_par_literal():
    r = Renderizador(cor=True)
    saida = r.linha('** sem par')
    assert visivel(saida) == '** sem par'


def test_snake_case_intacto():
    r = Renderizador(cor=True)
    saida = r.linha('variavel snake_case aqui')
    assert visivel(saida) == 'variavel snake_case aqui'
    assert '\x1b[3m' not in saida


def test_link():
    r = Renderizador(cor=True)
    saida = r.linha('veja [aqui](http://x.com) agora')
    assert visivel(saida) == 'veja aqui (http://x.com) agora'
    assert '\x1b[2m' in saida


# --- Tabelas ---------------------------------------------------------------

def test_tabela_separadora_apagada():
    r = Renderizador(cor=True)
    saida = r.linha('|---|---|')
    assert visivel(saida) == '|---|---|'
    assert saida.startswith('\x1b[2m')
    assert saida.endswith('\x1b[0m')


def test_tabela_conteudo_intacto():
    r = Renderizador(cor=True)
    saida = r.linha('| a | b |')
    assert visivel(saida) == '| a | b |'


# --- reiniciar -------------------------------------------------------------

def test_reiniciar_fecha_bloco():
    r = Renderizador(cor=True)
    r.linha('```python')
    r.reiniciar()
    # Apos reiniciar, uma linha normal nao deve ser tratada como codigo
    saida = r.linha('# titulo')
    assert visivel(saida) == 'titulo'
    assert saida.startswith('\x1b[1;38;5;37m')


# --- Sequencia real --------------------------------------------------------

def test_sequencia_resposta_real():
    r = Renderizador(cor=True)
    linhas = [
        '# Titulo',
        'Paragrafo com `codigo` e **negrito**.',
        '- item um',
        '- item dois',
        '```python',
        'def f():',
        '    return 1',
        '```',
        'Fim.',
    ]
    saidas = [r.linha(l) for l in linhas]
    assert len(saidas) == len(linhas)
    # Nenhuma linha some ou se multiplica
    for s in saidas:
        assert isinstance(s, str)
    # Verifica visiveis principais
    assert visivel(saidas[0]) == 'Titulo'
    assert visivel(saidas[1]) == 'Paragrafo com codigo e negrito.'
    assert visivel(saidas[2]) == '• item um'
    assert visivel(saidas[3]) == '• item dois'
    assert visivel(saidas[4]) == '╌╌ python'
    assert visivel(saidas[5]) == '│ def f():'
    assert visivel(saidas[6]) == '│     return 1'
    assert visivel(saidas[7]) == '╌╌'
    assert visivel(saidas[8]) == 'Fim.'
