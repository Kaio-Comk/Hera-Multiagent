# tests/test_chat_ferramenta_ui.py
'''Testes das funcoes de UI de chamadas de ferramenta e diffs.'''

import pytest

from cli.chat_ferramenta_ui import (
    eh_chamada_de_ferramenta,
    formatar_chamada_de_ferramenta,
    parece_diff,
    colorir_diff,
)


# --- eh_chamada_de_ferramenta ---

def test_eh_chamada_valida():
    assert eh_chamada_de_ferramenta('-> ler_arquivo C:\\x\\a.py') is True


def test_eh_chamada_com_espacos_iniciais():
    assert eh_chamada_de_ferramenta('   -> escrever_arquivo x') is True


def test_eh_chamada_underscore():
    assert eh_chamada_de_ferramenta('-> _privado arg') is True


def test_nao_eh_chamada_maiuscula():
    assert eh_chamada_de_ferramenta('-> Ler_arquivo x') is False


def test_nao_eh_chamada_sem_seta():
    assert eh_chamada_de_ferramenta('ler_arquivo x') is False


def test_nao_eh_chamada_vazia():
    assert eh_chamada_de_ferramenta('-> ') is False


# --- formatar_chamada_de_ferramenta ---

def test_formatar_sem_cor():
    saida = formatar_chamada_de_ferramenta('-> ler_arquivo C:\\x\\a.py', cor=False)
    assert saida == '  ↳ ler_arquivo C:\\x\\a.py\n'


def test_formatar_com_cor():
    saida = formatar_chamada_de_ferramenta('-> ler_arquivo C:\\x\\a.py', cor=True)
    assert saida.startswith('  ↳ \x1b[38;5;37mler_arquivo\x1b[0m ')
    assert '\x1b[2mC:\\x\\a.py\x1b[0m' in saida
    assert saida.endswith('\n')


def test_formatar_sem_cor_sem_ansi():
    saida = formatar_chamada_de_ferramenta('-> ler_arquivo x', cor=False)
    assert '\x1b' not in saida


def test_formatar_argumentos_longos():
    args = 'a' * 150
    saida = formatar_chamada_de_ferramenta('-> ferramenta ' + args, cor=False)
    assert '…' in saida
    assert saida.endswith('\n')
    # 100 chars + reticencias
    assert 'a' * 100 + '…' in saida


def test_formatar_argumentos_com_quebra_de_linha():
    saida = formatar_chamada_de_ferramenta('-> ferramenta linha1\nlinha2', cor=False)
    assert '\n' not in saida[:-1]
    assert 'linha1 linha2' in saida
    assert saida.endswith('\n')


def test_formatar_sem_argumentos():
    saida = formatar_chamada_de_ferramenta('-> listar', cor=False)
    assert saida == '  ↳ listar \n'


# --- parece_diff ---

def test_parece_diff_cabecalho():
    texto = '--- a/x\n+++ b/x\n@@ -1,3 +1,4 @@\n+nova\n-velha\n contexto'
    assert parece_diff(texto) is True


def test_parece_diff_apenas_hunk():
    assert parece_diff('@@ -1,3 +1,4 @@\n contexto') is True


def test_nao_eh_diff_lista_markdown():
    assert parece_diff('- item\n- outro item') is False


def test_nao_eh_diff_separador_sozinho():
    assert parece_diff('---\ntexto') is False


def test_nao_eh_diff_texto_comum():
    assert parece_diff('apenas texto normal\nsem nada') is False


# --- colorir_diff ---

DIFF_REAL = (
    '--- a/x\n'
    '+++ b/x\n'
    '@@ -1,3 +1,4 @@\n'
    '+nova\n'
    '-velha\n'
    ' contexto'
)


def test_colorir_diff_sem_cor():
    saida = colorir_diff(DIFF_REAL, cor=False)
    assert '\x1b' not in saida
    assert saida.endswith('\n')
    assert saida.startswith('  --- a/x\n')
    assert '  +nova\n' in saida
    assert '  -velha\n' in saida
    assert '   contexto\n' in saida


def test_colorir_diff_com_cor():
    saida = colorir_diff(DIFF_REAL, cor=True)
    assert '\x1b[1m--- a/x\x1b[0m' in saida
    assert '\x1b[1m+++ b/x\x1b[0m' in saida
    assert '\x1b[36m@@ -1,3 +1,4 @@\x1b[0m' in saida
    assert '\x1b[32m+nova\x1b[0m' in saida
    assert '\x1b[31m-velha\x1b[0m' in saida
    assert saida.endswith('\n')


def test_colorir_diff_garante_newline_final():
    saida = colorir_diff('--- a/x\n+++ b/x', cor=False)
    assert saida.endswith('\n')


def test_colorir_diff_indenta_dois_espacos():
    saida = colorir_diff('+linha', cor=False)
    assert saida == '  +linha\n'


def test_colorir_diff_linha_contexto_sem_cor():
    saida = colorir_diff('@@ -1 +1 @@\n contexto', cor=True)
    assert '   contexto\n' in saida
