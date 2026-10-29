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


def test_banner_contem_informacoes():
    '''O banner deve conter versao, modelo e pasta.'''
    v = Visual(cor=False, largura=80)
    texto = v.banner(versao='2.3', modelo='claude-3', pasta='/home/user/hera',
                     modo='chat', maestro=False)
    assert '2.3' in texto
    assert 'claude-3' in texto
    assert '/home/user/hera' in texto


def test_banner_maestro_mostra_automatico():
    '''Com maestro ligado, o banner mostra o modelo automatico.'''
    v = Visual(cor=False, largura=80)
    texto = v.banner(versao='1.0', modelo='claude', pasta='/tmp',
                     modo='chat', maestro=True)
    assert 'modelo automatico (maestro)' in texto


def test_banner_linhas_mesma_largura():
    '''Todas as linhas da caixa devem ter a mesma largura visivel.'''
    v = Visual(cor=True, largura=80)
    texto = v.banner(versao='1.0', modelo='claude', pasta='/tmp/proj',
                     modo='chat', maestro=False)
    linhas = texto.split('\n')
    # Considera apenas as linhas da caixa (ate a borda inferior).
    caixa = []
    for linha in linhas:
        limpa = _limpar(linha)
        caixa.append(limpa)
        if limpa.startswith('╰'):
            break
    larguras = {len(linha) for linha in caixa}
    assert len(larguras) == 1


def test_banner_pasta_longa_cortada():
    '''Pasta longa deve ser cortada com reticencias no inicio.'''
    v = Visual(cor=False, largura=40)
    pasta = '/muito/longo/caminho/para/uma/pasta/bem/comprida/aqui'
    texto = v.banner(versao='1.0', modelo='claude', pasta=pasta,
                     modo='chat', maestro=False)
    assert '…' in texto
    assert pasta not in texto


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
    primeiro = v.estilizar('ola')
    segundo = v.estilizar(' mundo')
    assert primeiro.startswith('● ')
    assert not segundo.startswith('● ')
    assert 'ola' in primeiro
    assert ' mundo' in segundo


def test_estilizar_quebra_interna_indenta():
    '''Quebras internas do pedaco devem indentar as linhas seguintes.'''
    v = Visual(cor=False, largura=80)
    v.novo_turno()
    saida = v.estilizar('linha1\nlinha2')
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
    sem_pensar = v.estilizar('ola')
    assert APAGAR not in sem_pensar

    v.novo_turno()
    v.marcar_pensando()
    com_pensar = v.estilizar('ola')
    assert APAGAR in com_pensar


def test_novo_turno_reinicia():
    '''novo_turno deve reiniciar o estado do turno.'''
    v = Visual(cor=True, largura=80)
    v.novo_turno()
    v.marcar_pensando()
    v.estilizar('ola')
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


def test_prompt_sem_cor():
    '''Sem cor, o prompt usa o marcador simples.'''
    v = Visual(cor=False, largura=20)
    saida = v.prompt()
    assert saida.endswith('> ')
    assert '─' * 20 in saida


def test_rodape_ultima_pasta():
    '''O rodape deve mostrar apenas o nome da ultima pasta.'''
    v = Visual(cor=False, largura=80)
    saida = v.rodape(modelo='claude', modo='chat', maestro=False,
                     pasta='/home/user/projeto')
    assert 'projeto' in saida
    assert '/home/user/projeto' not in saida
