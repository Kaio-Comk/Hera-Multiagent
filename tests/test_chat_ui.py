# tests/test_chat_ui.py
'''Testes do modulo cli.chat_ui.'''

from __future__ import annotations

import re

import pytest

from cli.chat_ui import APAGAR, Visual

_ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


def _limpar(texto: str) -> str:
    '''Remove sequencias ANSI para inspecionar o texto visivel.'''
    return _ANSI.sub('', texto)


def test_sem_cor_nao_emite_ansi():
    '''Sem cor, nenhum metodo deve emitir escapes ANSI.'''
    v = Visual(cor=False, largura=80)
    saidas = [
        v.banner(versao='1.0', modelo='claude', pasta='/tmp/proj',
                 modo='chat', maestro=False),
        v.regua(),
        v.prompt(),
        v.rodape(modelo='claude', modo='chat', maestro=False,
                 pasta='/tmp/proj'),
        v.pensando(),
        v.apagar_linha(),
        v.marcar_pensando(),
        v.estilizar('[maestro: ok]\n'),
        v.estilizar('Erro (falhou)\n'),
        v.estilizar('Aviso: cuidado\n'),
        v.estilizar('ola'),
        v.aprovacao('rodar comando'),
    ]
    for saida in saidas:
        assert '\x1b' not in saida


def test_estilizar_status():
    '''Linha de status vira a seta com o conteudo entre colchetes.'''
    v = Visual(cor=False, largura=80)
    v.novo_turno()
    saida = v.estilizar('[maestro: pensando]\n')
    assert '  ⎿ maestro: pensando' in saida


def test_estilizar_primeiro_pedaco_assistente():
    '''O primeiro pedaco do assistente ganha o marcador, o segundo nao.'''
    v = Visual(cor=False, largura=80)
    v.novo_turno()
    assert v.estilizar('ola') == ''  # linha incompleta: espera a quebra para desenhar
    primeiro = v.estilizar(' mundo\n')
    segundo = v.estilizar('outra\n')
    assert primeiro == '● ola mundo\n'
    assert segundo == '  outra\n'


def test_estilizar_quebra_interna_indenta():
    '''Quebras internas do pedaco devem indentar as linhas seguintes.'''
    v = Visual(cor=False, largura=80)
    v.novo_turno()
    saida = v.estilizar('linha1\nlinha2\n')
    assert 'linha1\n  linha2' in saida


def test_estilizar_erro_com_cor():
    '''Erro deve sair em vermelho quando a cor esta ligada.'''
    v = Visual(cor=True, largura=80)
    v.novo_turno()
    saida = v.estilizar('Erro (falhou)\n')
    assert '\x1b[31m' in saida


def test_apagar_so_com_pensando():
    '''APAGAR so aparece se marcar_pensando foi chamado.'''
    v = Visual(cor=True, largura=80)
    v.novo_turno()
    sem_pensar = v.estilizar('ola\n')
    assert APAGAR not in sem_pensar

    v.novo_turno()
    v.marcar_pensando()
    com_pensar = v.estilizar('ola\n')
    assert APAGAR in com_pensar


def test_novo_turno_reinicia():
    '''novo_turno deve reiniciar o estado do turno.'''
    v = Visual(cor=True, largura=80)
    v.novo_turno()
    v.marcar_pensando()
    v.estilizar('ola\n')
    assert v.assistente_iniciado is True
    assert v.pensando_visivel is False

    v.novo_turno()
    assert v.assistente_iniciado is False
    assert v.inicio_de_linha is True
    assert v.pensando_visivel is False


def test_aprovacao_em_aviso():
    '''A aprovacao deve sair em amarelo com o prefixo de pergunta.'''
    v = Visual(cor=True, largura=80)
    saida = v.aprovacao('rodar comando')
    assert '\x1b[33m' in saida
    assert '? rodar comando' in _limpar(saida)


def test_banner_sem_caixa():
    v = Visual(cor=False, largura=80)
    t = v.banner(versao='1.2.3', modelo='claude-sonnet', pasta='/home/user/projeto', modo='guarded', maestro=False)
    assert '╭' not in t
    assert '│' not in t
    assert 'Hera' in t
    assert 'v1.2.3' in t
    assert 'claude-sonnet' in t
    assert '/home/user/projeto' in t


def test_banner_mascote_nas_tres_linhas():
    v = Visual(cor=False, largura=80)
    t = v.banner(versao='1.0', modelo='m', pasta='/p', modo='', maestro=False)
    limpo = _limpar(t)
    linhas = limpo.split('\n')
    assert '▄ ▄██▄ ▄' in linhas[0]
    assert '██████████' in linhas[1]
    assert '▀████▀' in linhas[2]


def test_banner_instrucoes_so_quando_positivo():
    v = Visual(cor=False, largura=80)
    sem = v.banner(versao='1.0', modelo='m', pasta='/p', modo='', maestro=False, instrucoes=0)
    assert 'arquivo(s) de instrucoes' not in sem
    com = v.banner(versao='1.0', modelo='m', pasta='/p', modo='', maestro=False, instrucoes=3)
    assert '3 arquivo(s) de instrucoes carregado(s)' in com


def test_prompt_comeca_com_regua_e_termina_com_marcador():
    v = Visual(cor=False, largura=80)
    t = v.prompt()
    assert t.startswith(v.regua())
    assert t.endswith('> ')


def test_rodape_com_modo():
    v = Visual(cor=False, largura=80)
    t = v.rodape(modelo='m', modo='guarded', maestro=False, pasta='/p')
    assert 'aprovação manual' in t and 'shift+tab' in t


def test_rodape_sem_modo():
    v = Visual(cor=False, largura=80)
    t = v.rodape(modelo='m', modo='', maestro=False, pasta='/p')
    assert 'modo' not in t


def test_placeholder_sem_escapes():
    v = Visual(cor=False, largura=80)
    t = v.placeholder()
    assert t == 'Pergunte algo, / para comandos, @ para anexar arquivo'


def test_banner_pasta_longa_cortada():
    v = Visual(cor=False, largura=80)
    pasta = '/' + 'a' * 200
    t = v.banner(versao='1.0', modelo='m', pasta=pasta, modo='', maestro=False)
    assert '…' in t


def test_banner_mascote_com_cor():
    v = Visual(cor=True, largura=80)
    t = v.banner(versao='1.0', modelo='m', pasta='/p', modo='', maestro=False)
    assert '38;5;37' in t


def test_limpar_tela_so_com_cor():
    assert Visual(cor=False).limpar_tela() == ''
    assert Visual(cor=True).limpar_tela() == '\x1b[2J\x1b[H'


def test_eco_repete_a_pergunta():
    assert Visual(cor=False).eco('ola') == '> ola\n'
    assert Visual(cor=True).eco('ola').endswith('ola\n')


def test_status_durante_a_espera_devolve_o_indicador():
    v = Visual(cor=True, largura=80)
    v.novo_turno()
    v.marcar_pensando()
    saida = v.estilizar('[maestro: faixa leve -> m]\n')
    assert saida.endswith('Pensando…\x1b[0m')
    assert v.pensando_visivel is True
    assert 'Pensando' not in v.estilizar('Ola\n')  # a resposta apaga o indicador e ele nao volta
    assert v.pensando_visivel is False


def test_pensando_mostra_o_tempo_a_partir_de_2s():
    v = Visual(cor=False)
    assert v.pensando(0) == '✻ Pensando…'
    assert '(5s · ctrl+c interrompe)' in v.pensando(5)
    assert '(' not in v.pensando(1)


def test_tick_so_com_cor_e_indicador_visivel():
    v = Visual(cor=True)
    assert v.tick_pensando() == ''
    v.marcar_pensando()
    assert v.tick_pensando().startswith('\r\x1b[2K')
    assert Visual(cor=False).tick_pensando() == ''
