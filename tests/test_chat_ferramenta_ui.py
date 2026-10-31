# tests/test_chat_ferramenta_ui.py
'''Testes das funcoes de UI de chamadas de ferramenta e diffs.'''

import pytest

from cli.chat_ferramenta_ui import (
    eh_chamada_de_ferramenta,
    eh_pensamento,
    formatar_chamada_de_ferramenta,
    formatar_colapso_acoes,
    formatar_grupo_acoes,
    formatar_pensamento,
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
    assert saida == '● Explored 1 file (C:\\x\\a.py)\n'


def test_formatar_com_cor():
    saida = formatar_chamada_de_ferramenta('-> ler_arquivo C:\\x\\a.py', cor=True)
    assert '\x1b[38;5;37m● \x1b[0m' in saida
    assert '\x1b[1mExplored 1 file\x1b[0m' in saida
    assert '\x1b[2m(C:\\x\\a.py)\x1b[0m' in saida
    assert saida.endswith('\n')


def test_formatar_sem_cor_sem_ansi():
    saida = formatar_chamada_de_ferramenta('-> ler_arquivo x', cor=False)
    assert '\x1b' not in saida
    assert saida == '● Explored 1 file (x)\n'


def test_formatar_executar_comando():
    saida = formatar_chamada_de_ferramenta('-> executar_comando pytest -v', cor=False)
    assert saida == '● Ran (pytest -v)\n'


def test_formatar_editar_arquivo():
    saida = formatar_chamada_de_ferramenta('-> editar_arquivo fix.py', cor=False)
    assert saida == '● Edited 1 file (fix.py)\n'


def test_formatar_argumentos_longos():
    args = 'a' * 150
    saida = formatar_chamada_de_ferramenta('-> ferramenta ' + args, cor=False)
    assert '…' in saida
    assert saida.endswith('\n')
    assert 'a' * 100 + '…' in saida


def test_formatar_argumentos_com_quebra_de_linha():
    saida = formatar_chamada_de_ferramenta('-> ferramenta linha1\nlinha2', cor=False)
    assert '\n' not in saida[:-1]
    assert 'linha1 linha2' in saida
    assert saida.endswith('\n')


def test_formatar_sem_argumentos():
    saida = formatar_chamada_de_ferramenta('-> listar', cor=False)
    assert saida == '● Listar\n'


def test_eh_pensamento():
    assert eh_pensamento('○ Thought (analisando)') is True
    assert eh_pensamento('-> pensamento pensando...') is True
    assert eh_pensamento('texto normal') is False


def test_formatar_pensamento():
    saida = formatar_pensamento('○ Thought (pensando algo)', cor=False)
    assert saida == '○ Thought (pensando algo)\n'

    saida_dur = formatar_pensamento('analisando', cor=False, duracao='3m 2s')
    assert saida_dur == '○ Thought for 3m 2s (analisando)\n'


def test_formatar_colapso_acoes():
    saida = formatar_colapso_acoes(57, leituras=17, edicoes=7, comandos=33, falhas=6, cor=False)
    assert saida == '… 57 more actions (17 reads, 7 edits, 33 commands: 6 failed) …\n' 


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


# --- formatar_grupo_acoes e estilo Antigravity ---

def test_eh_chamada_bullet():
    assert eh_chamada_de_ferramenta('● Ran 9 commands') is True
    assert eh_chamada_de_ferramenta('● Edited 1 file (x.py)') is True


def test_formatar_pensamento_com_duracao_na_string():
    saida = formatar_pensamento('○ Thought for 1m 18s (analisando o contexto)', cor=False)
    assert saida == '○ Thought for 1m 18s (analisando o contexto)\n'


def test_formatar_grupo_acoes_comandos():
    acoes = [{'tipo': 'executar_comando', 'alvo': 'powershell -Command ...', 'ok': True}] * 9
    saida = formatar_grupo_acoes(acoes, cor=False)
    assert saida == '● Ran 9 commands (powershell -Command ... ×9)\n'


def test_formatar_grupo_acoes_comando_unico():
    acoes = [{'tipo': 'executar_comando', 'alvo': 'git status', 'ok': True}]
    saida = formatar_grupo_acoes(acoes, cor=False)
    assert saida == '● Ran (git status)\n'


def test_formatar_grupo_acoes_comandos_com_falha():
    acoes = [
        {'tipo': 'executar_comando', 'alvo': 'python "C:\\Projects\\app\\test.py"', 'ok': True},
        {'tipo': 'executar_comando', 'alvo': 'powershell -Command ...', 'ok': False},
    ]
    saida = formatar_grupo_acoes(acoes, cor=False)
    assert '● Ran 2 commands' in saida
    assert ': failed)' in saida


def test_formatar_grupo_acoes_edicao_unica():
    acoes = [{'tipo': 'editar_arquivo', 'alvo': 'check_autofix_git.py', 'ok': True}]
    saida = formatar_grupo_acoes(acoes, cor=False)
    assert saida == '● Edited 1 file (check_autofix_git.py)\n'


def test_formatar_grupo_acoes_edicao_caminho_longo_usa_basename():
    acoes = [{'tipo': 'editar_arquivo', 'alvo': 'C:\\Projects\\app\\test_webplayer_build.py', 'ok': True}]
    saida = formatar_grupo_acoes(acoes, cor=False)
    assert saida == '● Edited 1 file (test_webplayer_build.py)\n'


def test_formatar_grupo_acoes_misto_com_expand():
    acoes = [{'tipo': 'executar_comando', 'alvo': 'python "C:\\Projects\\app\\test.py"', 'ok': True}] * 6 + [
        {'tipo': 'delegar_tarefa', 'alvo': 'task-1', 'ok': True},
        {'tipo': 'delegar_tarefa', 'alvo': 'task-2', 'ok': True},
    ]
    saida = formatar_grupo_acoes(acoes, cor=False, expansivel=True)
    assert '● Ran 6 commands, checked 2 tasks' in saida
    assert '(ctrl+o to expand)' in saida
