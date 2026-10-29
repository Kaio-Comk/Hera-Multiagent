# cli/chat_markdown.py
'''Renderizador de Markdown linha a linha para o terminal.

A resposta do modelo chega em streaming; o chamador junta o texto e
entrega uma linha completa por vez (sem o '\n'). Este modulo mantem o
estado minimo necessario (dentro de bloco de codigo) e devolve a linha
ja formatada com escapes ANSI quando cor=True.
'''

import re

# Prefixos ANSI usados
_RESET = '\x1b[0m'
_DIM = '\x1b[2m'
_BOLD = '\x1b[1m'
_ITALIC = '\x1b[3m'
_TITULO_H = '\x1b[1;38;5;37m'
_CODIGO = '\x1b[38;5;180m'

# Regex para cerca de codigo (``` ou ~~~), com recuo de ate 3 espacos
_RE_CERCA = re.compile(r'^ {0,3}(`{3,}|~{3,})(.*)$')

# Regex para titulos: ate 3 espacos de recuo, 1 a 6 '#', seguido de espaco
_RE_TITULO = re.compile(r'^ {0,3}(#{1,6}) (.*)$')

# Regex para regua horizontal: ---, ***, ___ (3 ou mais)
_RE_REGUA = re.compile(r'^ {0,3}(-{3,}|\*{3,}|_{3,})\s*$')

# Regex para citacao
_RE_CITACAO = re.compile(r'^ {0,3}> ?(.*)$')

# Regex para item de lista com marcador - * +
_RE_LISTA_MARCADOR = re.compile(r'^( *)([-*+]) (.*)$')

# Regex para item de lista numerada
_RE_LISTA_NUM = re.compile(r'^( *)(\d+\.) (.*)$')

# Regex para linha de tabela separadora: |---|---|
_RE_TABELA_SEP = re.compile(r'^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$')


def _inline(texto: str, prefixo: str = '') -> str:
    '''Aplica o tratamento inline a um trecho de texto.

    prefixo e o estilo "de fundo" (ex.: titulo) que deve ser reaberto
    apos cada estilo inline fechado.
    '''
    # Processa em uma passada, respeitando prioridade:
    # 1) `codigo` (nada dentro e interpretado)
    # 2) **negrito** / __negrito__
    # 3) *italico* / _italico_
    # 4) [texto](url)
    resultado = []
    i = 0
    n = len(texto)
    while i < n:
        c = texto[i]

        # Codigo inline: `...`
        if c == '`':
            fim = texto.find('`', i + 1)
            if fim != -1:
                conteudo = texto[i + 1:fim]
                resultado.append(_CODIGO + conteudo + _RESET + prefixo)
                i = fim + 1
                continue

        # Negrito: **...** ou __...__
        if texto.startswith('**', i):
            fim = texto.find('**', i + 2)
            if fim != -1:
                conteudo = _inline(texto[i + 2:fim], _BOLD)
                resultado.append(_BOLD + conteudo + _RESET + prefixo)
                i = fim + 2
                continue
        if texto.startswith('__', i):
            fim = texto.find('__', i + 2)
            if fim != -1:
                conteudo = _inline(texto[i + 2:fim], _BOLD)
                resultado.append(_BOLD + conteudo + _RESET + prefixo)
                i = fim + 2
                continue

        # Italico: *...* (nao confundir com ** nem com asterisco solto)
        if c == '*':
            # Evita capturar '**' (ja tratado acima)
            if i + 1 < n and texto[i + 1] == '*':
                resultado.append(c)
                i += 1
                continue
            # Nao inicia italico se o char anterior for espaco ou inicio
            # e o proximo for espaco (asterisco solto)
            if i + 1 < n and texto[i + 1] != ' ':
                fim = texto.find('*', i + 1)
                if fim != -1 and fim > i + 1 and texto[fim - 1] != ' ':
                    conteudo = texto[i + 1:fim]
                    resultado.append(_ITALIC + conteudo + _RESET + prefixo)
                    i = fim + 1
                    continue
            resultado.append(c)
            i += 1
            continue

        # Italico: _..._ (nao dentro de palavra, ex.: snake_case)
        if c == '_':
            # Nao inicia se estiver dentro de palavra (char anterior alfanumerico)
            if i > 0 and (texto[i - 1].isalnum()):
                resultado.append(c)
                i += 1
                continue
            if i + 1 < n and texto[i + 1] != ' ':
                fim = texto.find('_', i + 1)
                if fim != -1 and fim > i + 1 and texto[fim - 1] != ' ':
                    # Nao fecha se o char seguinte for alfanumerico (snake_case)
                    if fim + 1 < n and texto[fim + 1].isalnum():
                        resultado.append(c)
                        i += 1
                        continue
                    conteudo = texto[i + 1:fim]
                    resultado.append(_ITALIC + conteudo + _RESET + prefixo)
                    i = fim + 1
                    continue
            resultado.append(c)
            i += 1
            continue

        # Link: [texto](url)
        if c == '[':
            fim_col = texto.find(']', i + 1)
            if fim_col != -1 and fim_col + 1 < n and texto[fim_col + 1] == '(':
                fim_par = texto.find(')', fim_col + 2)
                if fim_par != -1:
                    rotulo = texto[i + 1:fim_col]
                    url = texto[fim_col + 2:fim_par]
                    resultado.append(
                        rotulo + ' ' + _DIM + '(' + url + ')' + _RESET + prefixo
                    )
                    i = fim_par + 1
                    continue

        resultado.append(c)
        i += 1

    return ''.join(resultado)


class Renderizador:
    '''Renderiza Markdown linha a linha para o terminal.'''

    def __init__(self, cor: bool):
        '''Inicializa o renderizador.

        cor: se True, aplica escapes ANSI; se False, devolve o texto
        exatamente como veio.
        '''
        self.cor = cor
        self._em_bloco = False
        self._linguagem = ''

    def reiniciar(self) -> None:
        '''Fecha qualquer estado de bloco de codigo aberto.'''
        self._em_bloco = False
        self._linguagem = ''

    def linha(self, texto: str) -> str:
        '''Renderiza uma linha completa (sem '\n') e devolve o resultado.'''
        if not self.cor:
            return texto

        # Dentro de bloco de codigo: nao interpreta Markdown
        if self._em_bloco:
            m = _RE_CERCA.match(texto)
            if m:
                # Fechamento do bloco
                self._em_bloco = False
                self._linguagem = ''
                return _DIM + '╌╌' + _RESET
            return _DIM + '│' + _RESET + ' ' + _CODIGO + texto + _RESET

        # Cerca de codigo (abertura)
        m = _RE_CERCA.match(texto)
        if m:
            self._em_bloco = True
            self._linguagem = m.group(2).strip()
            if self._linguagem:
                return _DIM + '╌╌ ' + self._linguagem + _RESET
            return _DIM + '╌╌' + _RESET

        # Regua horizontal
        if _RE_REGUA.match(texto):
            return _DIM + '─' * 40 + _RESET

        # Titulos
        m = _RE_TITULO.match(texto)
        if m:
            nivel = len(m.group(1))
            conteudo = m.group(2)
            if nivel <= 2:
                prefixo = _TITULO_H
            else:
                prefixo = _BOLD
            return prefixo + _inline(conteudo, prefixo) + _RESET

        # Citacao
        m = _RE_CITACAO.match(texto)
        if m:
            resto = m.group(1)
            return _DIM + '▎ ' + _RESET + _ITALIC + _inline(resto, _ITALIC) + _RESET

        # Item de lista com marcador - * +
        m = _RE_LISTA_MARCADOR.match(texto)
        if m:
            recuo = m.group(1)
            resto = m.group(3)
            return recuo + '• ' + _inline(resto)

        # Item de lista numerada
        m = _RE_LISTA_NUM.match(texto)
        if m:
            recuo = m.group(1)
            num = m.group(2)
            resto = m.group(3)
            return recuo + num + ' ' + _inline(resto)

        # Linha separadora de tabela
        if _RE_TABELA_SEP.match(texto):
            return _DIM + texto + _RESET

        # Linha normal
        return _inline(texto)
