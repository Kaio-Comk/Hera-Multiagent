# cli/chat_ui.py
'''Visual do REPL de terminal do Hera, no estilo do Claude Code.

Este modulo nao depende de prompt_toolkit: apenas devolve strings com
sequencias ANSI. Toda a formatacao e calculada pelo tamanho visivel,
ignorando os escapes de cor.
'''

from __future__ import annotations

import os
import shutil
import sys

# Constante de modulo usada para apagar a linha atual do terminal.
APAGAR = '\r\x1b[2K'

# Codigos ANSI usados pelo modulo.
_ACENTO = '\x1b[38;5;37m'
_APAGADO = '\x1b[2m'
_ERRO = '\x1b[31m'
_AVISO = '\x1b[33m'
_RESET = '\x1b[0m'


def _sem_ansi(texto: str) -> str:
    '''Remove sequencias ANSI de um texto para medir o tamanho visivel.'''
    saida = []
    i = 0
    n = len(texto)
    while i < n:
        if texto[i] == '\x1b' and i + 1 < n and texto[i + 1] == '[':
            j = i + 2
            while j < n and not ('@' <= texto[j] <= '~'):
                j += 1
            i = j + 1
            continue
        saida.append(texto[i])
        i += 1
    return ''.join(saida)


def _largura_visivel(texto: str) -> int:
    '''Devolve o comprimento visivel de um texto, sem contar escapes ANSI.'''
    return len(_sem_ansi(texto))


def _cortar_inicio(texto: str, limite: int) -> str:
    '''Corta o texto pelo inicio, prefixando com reticencias se necessario.'''
    if limite <= 0:
        return ''
    if len(texto) <= limite:
        return texto
    if limite == 1:
        return '…'
    return '…' + texto[-(limite - 1):]


MASCOTE = ('  ▄ ▄██▄ ▄  ', ' ██████████ ', '   ▀████▀   ')


class Visual:
    '''Renderiza o visual do REPL de terminal do Hera.'''

    def __init__(self, *, cor: bool | None = None, largura: int | None = None):
        '''Inicializa o visual, decidindo cor e largura do terminal.'''
        if cor is None:
            cor = bool(sys.stdout.isatty()) and 'NO_COLOR' not in os.environ
        self.cor = bool(cor)
        if largura is None:
            largura = shutil.get_terminal_size((80, 20)).columns
        self.largura = max(1, int(largura))
        # Estado do turno atual.
        self.assistente_iniciado = False
        self.inicio_de_linha = True
        self.pensando_visivel = False

    # ------------------------------------------------------------------
    # Auxiliares de cor
    # ------------------------------------------------------------------
    def _pintar(self, texto: str, codigo: str) -> str:
        '''Aplica um codigo ANSI ao texto, se a cor estiver ligada.'''
        if not self.cor:
            return texto
        return codigo + texto + _RESET

    def _acento(self, texto: str) -> str:
        '''Pinta o texto com a cor de acento (verde-azulado).'''
        return self._pintar(texto, _ACENTO)

    def _apagado(self, texto: str) -> str:
        '''Pinta o texto com o estilo apagado (dim).'''
        return self._pintar(texto, _APAGADO)

    def _erro(self, texto: str) -> str:
        '''Pinta o texto com a cor de erro (vermelho).'''
        return self._pintar(texto, _ERRO)

    def _aviso(self, texto: str) -> str:
        '''Pinta o texto com a cor de aviso (amarelo).'''
        return self._pintar(texto, _AVISO)

    def limpar_tela(self) -> str:
        '''Limpa a tela visivel e leva o cursor ao topo (abertura do chat); sem cor, nao faz nada.'''
        return '\x1b[2J\x1b[H' if self.cor else ''

    def apagar_linha(self) -> str:
        '''Devolve a sequencia para apagar a linha atual, ou vazio sem cor.'''
        if not self.cor:
            return ''
        return APAGAR

    # ------------------------------------------------------------------
    # Banner e reguas
    # ------------------------------------------------------------------
    def banner(self, *, versao, modelo, pasta, modo, maestro, instrucoes: int = 0) -> str:
        '''Monta o banner do chat com o mascote e informacoes de contexto.'''
        linhas = []
        linha1 = self._pintar('Hera', '\x1b[1m')
        if versao:
            linha1 += ' ' + self._apagado('v' + versao)
        base2 = 'modelo automatico (maestro)' if maestro else modelo
        if modo:
            base2 += ' · modo ' + modo
        linha2 = self._apagado(base2)
        limite = self.largura - 16
        linha3 = self._apagado(_cortar_inicio(pasta, limite))
        textos = [linha1, linha2, linha3]
        for i, texto in enumerate(textos):
            linhas.append(self._acento(MASCOTE[i]) + '  ' + texto + '\n')
        linhas.append('\n')
        if instrucoes > 0:
            linhas.append('▏ ' + self._apagado(str(instrucoes) + ' arquivo(s) de instrucoes carregado(s)') + '\n')
            linhas.append('\n')
        return ''.join(linhas)

    def prompt(self) -> str:
        '''Retorna a regua e o marcador de entrada do usuario.'''
        return self.regua() + '\n' + self._acento('❯ ' if self.cor else '> ')

    def regua(self) -> str:
        '''Devolve uma linha horizontal apagada com a largura do terminal.'''
        return self._apagado('─' * self.largura)

    def rodape(self, *, modelo, modo, maestro, pasta) -> str:
        '''Monta o rodape com modo, modelo e atalhos.'''
        partes = []
        if modo:
            partes.append(self._aviso(('⏵⏵ ' if self.cor else '>> ') + 'modo ' + modo) + self._apagado(' · '))
        itens = ['maestro' if maestro else modelo, '/ comandos', '@arquivo', 'ctrl+c interrompe']
        partes.append(self._apagado(' · '.join(itens)))
        return self.regua() + '\n' + '  ' + ''.join(partes)

    def placeholder(self) -> str:
        '''Retorna o texto de placeholder do campo de entrada.'''
        return self._apagado('Pergunte algo, / para comandos, @ para anexar arquivo')

    def pensando(self) -> str:
        '''Devolve o indicador de pensamento, sem quebra de linha.'''
        return self._apagado('✻ Pensando…')

    def novo_turno(self) -> None:
        '''Reinicia o estado do turno atual.'''
        self.assistente_iniciado = False
        self.inicio_de_linha = True
        self.pensando_visivel = False

    def marcar_pensando(self) -> str:
        '''Devolve o indicador de pensamento e marca que ele esta visivel.'''
        self.pensando_visivel = True
        return self.pensando()

    def _prefixo_apagar(self) -> str:
        '''Devolve APAGAR se o indicador de pensamento estiver visivel.'''
        if self.pensando_visivel:
            self.pensando_visivel = False
            return self.apagar_linha()
        return ''

    # ------------------------------------------------------------------
    # Estilizacao de pedacos
    # ------------------------------------------------------------------
    def estilizar(self, pedaco: str) -> str:
        '''Estiliza um pedaco de texto escrito pelo REPL.

        Trata linhas de status, mensagens de erro/aviso e texto do
        assistente, mantendo o estado entre chamadas.
        '''
        if not pedaco:
            return ''

        # (a) Linha de status: comeca com '[' e termina com '\n'.
        if (not pedaco[0].isspace() and pedaco.startswith('[')
                and pedaco.endswith('\n')):
            interno = pedaco[1:-2] if len(pedaco) >= 3 else ''
            prefixo = self._prefixo_apagar()
            return prefixo + self._apagado('  ⎿ ' + interno) + '\n'

        # (b) Erro ou aviso.
        if pedaco.startswith('Erro ('):
            prefixo = self._prefixo_apagar()
            return prefixo + self._erro(pedaco)
        if pedaco.startswith('Aviso:') or pedaco.startswith('Aviso '):
            prefixo = self._prefixo_apagar()
            return prefixo + self._aviso(pedaco)

        # (c) Texto do assistente.
        prefixo = ''
        if not self.assistente_iniciado:
            prefixo = self._prefixo_apagar()
            prefixo += self._acento('● ')
            self.assistente_iniciado = True
            self.inicio_de_linha = False

        texto = self._indentar(pedaco)
        return prefixo + texto

    def _indentar(self, pedaco: str) -> str:
        '''Indenta com dois espacos so as linhas que tem texto (linha em branco fica vazia).'''
        saida = []
        for caractere in pedaco:
            if caractere == '\n':
                saida.append(caractere)
                self.inicio_de_linha = True
                continue
            if self.inicio_de_linha:
                saida.append('  ')
                self.inicio_de_linha = False
            saida.append(caractere)
        return ''.join(saida)

    # ------------------------------------------------------------------
    # Aprovacao
    # ------------------------------------------------------------------
    def aprovacao(self, descricao: str) -> str:
        '''Devolve a linha de pedido de aprovacao em amarelo.'''
        return self._aviso('? ' + descricao)
