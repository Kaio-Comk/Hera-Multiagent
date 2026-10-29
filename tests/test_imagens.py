# tests/test_imagens.py
'''Testes para app/imagens.py.'''

from app.imagens import (
    TOKENS_POR_IMAGEM,
    contar_imagens,
    descrever_conteudo,
    texto_de_conteudo,
    tokens_do_conteudo,
)


def test_texto_de_conteudo_str():
    assert texto_de_conteudo('ola') == 'ola'


def test_texto_de_conteudo_none():
    assert texto_de_conteudo(None) == ''


def test_texto_de_conteudo_lista_texto_e_imagens():
    base64_gigante = 'A' * 1000
    conteudo = [
        {'type': 'text', 'text': 'pergunta'},
        {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{base64_gigante}'}},
        {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{base64_gigante}'}},
    ]
    resultado = texto_de_conteudo(conteudo)
    assert resultado == 'pergunta\n[imagem]\n[imagem]'
    assert base64_gigante not in resultado


def test_texto_de_conteudo_parte_malformada():
    conteudo = [
        'nao sou dict',
        {'type': 'text', 'text': 'ok'},
        123,
    ]
    assert texto_de_conteudo(conteudo) == 'ok'


def test_contar_imagens():
    conteudo = [
        {'type': 'text', 'text': 'a'},
        {'type': 'image_url', 'image_url': {'url': 'x'}},
        {'type': 'image_url', 'image_url': {'url': 'y'}},
    ]
    assert contar_imagens(conteudo) == 2
    assert contar_imagens('texto') == 0
    assert contar_imagens(None) == 0


def test_tokens_do_conteudo_base64_gigante_nao_infla():
    base64_gigante = 'A' * (1024 * 1024)
    conteudo = [
        {'type': 'text', 'text': 'abcd'},
        {'type': 'image_url', 'image_url': {'url': f'data:image/png;base64,{base64_gigante}'}},
    ]
    # texto 'abcd' -> ceil(4/4) = 1 token + 800 da imagem
    assert tokens_do_conteudo(conteudo) == 1 + TOKENS_POR_IMAGEM


def test_tokens_do_conteudo_str():
    assert tokens_do_conteudo('abcd') == 1


def test_descrever_conteudo_com_imagens():
    conteudo = [
        {'type': 'text', 'text': 'pergunta'},
        {'type': 'image_url', 'image_url': {'url': 'x'}},
        {'type': 'image_url', 'image_url': {'url': 'y'}},
    ]
    assert descrever_conteudo(conteudo) == 'pergunta\n[imagem]\n[imagem] [2 imagem(ns)]'


def test_descrever_conteudo_sem_imagens():
    assert descrever_conteudo('pergunta') == 'pergunta'


def test_tipo_estranho_int():
    assert texto_de_conteudo(42) == '42'
    assert contar_imagens(42) == 0
