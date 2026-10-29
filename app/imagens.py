# app/imagens.py
'''Utilitarios para lidar com conteudo de mensagens no formato OpenAI.'''

from math import ceil

TOKENS_POR_IMAGEM = 800


def texto_de_conteudo(conteudo) -> str:
    '''Extrai o texto de um conteudo, trocando imagens por "[imagem]".'''
    if conteudo is None:
        return ''
    if isinstance(conteudo, str):
        return conteudo
    if isinstance(conteudo, list):
        partes = []
        for parte in conteudo:
            if not isinstance(parte, dict):
                continue
            tipo = parte.get('type')
            if tipo == 'text':
                partes.append(parte.get('text', ''))
            elif tipo == 'image_url':
                partes.append('[imagem]')
        return '\n'.join(partes)
    return str(conteudo)


def contar_imagens(conteudo) -> int:
    '''Conta quantas partes de imagem existem no conteudo.'''
    if not isinstance(conteudo, list):
        return 0
    total = 0
    for parte in conteudo:
        if isinstance(parte, dict) and parte.get('type') == 'image_url':
            total += 1
    return total


def tokens_do_conteudo(conteudo) -> int:
    '''Estima tokens do conteudo sem contar o base64 das imagens.'''
    texto = texto_de_conteudo(conteudo)
    # remove o marcador '[imagem]' para nao inflar a conta de texto
    texto_sem_marcador = texto.replace('[imagem]', '').strip()
    tokens_texto = ceil(len(texto_sem_marcador) / 4)
    return tokens_texto + TOKENS_POR_IMAGEM * contar_imagens(conteudo)


def descrever_conteudo(conteudo) -> str:
    '''Descreve o conteudo, anexando a contagem de imagens no fim.'''
    texto = texto_de_conteudo(conteudo)
    n = contar_imagens(conteudo)
    if n > 0:
        return f'{texto} [{n} imagem(ns)]'
    return texto
