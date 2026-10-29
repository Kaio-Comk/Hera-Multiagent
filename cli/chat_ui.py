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
import time

from cli.chat_ferramenta_ui import (
    colorir_diff,
    eh_chamada_de_ferramenta,
    formatar_chamada_de_ferramenta,
    parece_diff,
)
from cli.chat_markdown import Renderizador

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


# modo -> (texto, em destaque?, simbolo sem cor). O auto mode (como no Claude Code) fica em amarelo.
_ROTULOS_MODO = {
    'auto': ('⏵⏵ auto mode ligado (shift+tab alterna)', True, '>>'),
    'workspace': ('⏵⏵ pasta de trabalho livre (shift+tab alterna)', True, '>>'),
    'guarded': ('⏵ aprovação manual (shift+tab alterna)', False, '>'),
    'read-only': ('⏸ somente leitura (shift+tab alterna)', False, '||'),
}


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
        self._pensando_desde = 0.0
        self._md = Renderizador(self.cor)
        self._buf = ''  # texto do assistente ainda sem a quebra de linha que completa a linha

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

    def eco(self, texto: str) -> str:
        '''Repete a pergunta do usuario no historico, depois que o campo de entrada sai do fundo da tela.'''
        return self._acento('❯ ' if self.cor else '> ') + texto + '\n'

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
    def banner(self, *, versao, modelo, pasta, modo, maestro, instrucoes: int = 0, arquivos: tuple = (),
               retomando: str = '') -> str:
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
        if retomando:
            linhas.append('▏ ' + self._apagado(retomando) + '\n')
            if instrucoes <= 0:
                linhas.append('\n')
        if instrucoes > 0:
            texto = str(instrucoes) + ' arquivo(s) de instrucoes carregado(s)'
            if arquivos:
                texto += ': ' + ', '.join(arquivos)
            linhas.append('▏ ' + self._apagado(texto) + '\n')
            linhas.append('\n')
        return ''.join(linhas)

    def prompt(self) -> str:
        '''Retorna a regua e o marcador de entrada do usuario.'''
        return self.regua() + '\n' + self._acento('❯ ' if self.cor else '> ')

    def regua(self) -> str:
        '''Devolve uma linha horizontal apagada com a largura do terminal.'''
        return self._apagado('─' * self.largura)

    def rodape(self, *, modelo, modo, maestro, pasta, todo_pc: bool = False) -> str:
        '''Monta o rodape: modo do agente (Shift+Tab alterna), alcance, modelo e atalhos.'''
        partes = []
        rotulo = _ROTULOS_MODO.get(modo)
        if rotulo:
            texto, destaque, simbolo_sem_cor = rotulo
            texto = texto if self.cor else simbolo_sem_cor + ' ' + texto.split(' ', 1)[1]
            partes.append((self._aviso(texto) if destaque else self._apagado(texto)) + self._apagado(' · '))
        itens = ['todo o PC'] if todo_pc and rotulo else []
        itens += ['maestro' if maestro else modelo, '/ comandos', '@arquivo', 'ctrl+c interrompe']
        partes.append(self._apagado(' · '.join(itens)))
        return self.regua() + '\n' + '  ' + ''.join(partes)

    def placeholder(self) -> str:
        '''Retorna o texto de placeholder do campo de entrada.'''
        return self._apagado('Pergunte algo, / para comandos, @ para anexar arquivo')

    def pensando(self, segundos: int = 0) -> str:
        '''Devolve o indicador de pensamento, sem quebra de linha; a partir de 2 s mostra o tempo e como interromper.'''
        quadros = '✻✽✶✳✢'
        simbolo = quadros[segundos % len(quadros)] if segundos else quadros[0]
        texto = f'{simbolo} Pensando…'
        if segundos >= 2:
            texto += f' ({segundos}s · ctrl+c interrompe)'
        return self._apagado(texto)

    def tick_pensando(self) -> str:
        '''Redesenha o indicador com o tempo decorrido; vazio se ele nao esta na tela (a animacao para sozinha).'''
        if not self.cor or not self.pensando_visivel:
            return ''
        segundos = int(time.monotonic() - self._pensando_desde)
        return self.apagar_linha() + self.pensando(segundos)

    def novo_turno(self) -> None:
        '''Reinicia o estado do turno atual.'''
        self.assistente_iniciado = False
        self.inicio_de_linha = True
        self.pensando_visivel = False
        self._md.reiniciar()
        self._buf = ''

    def marcar_pensando(self) -> str:
        '''Devolve o indicador de pensamento e marca que ele esta visivel.'''
        self.pensando_visivel = True
        self._pensando_desde = time.monotonic()
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
        # So antes da resposta comecar: depois, uma linha de Markdown como '[link](url)' nao e status.
        if (not pedaco[0].isspace() and pedaco.startswith('[') and pedaco.endswith('\n')
                and not self.assistente_iniciado and not self._buf):
            interno = pedaco[1:-2] if len(pedaco) >= 3 else ''
            esperando = self.pensando_visivel
            prefixo = self._prefixo_apagar()
            linha = prefixo + self._apagado('  ⎿ ' + interno) + '\n'
            if esperando:
                # Ainda esperando a resposta: o indicador volta para baixo da linha de status.
                self.pensando_visivel = True
                linha += self.pensando()
            return linha

        # (b0) Chamada de ferramenta do agente ('-> ler_arquivo x') e diff: ficam fora do Markdown.
        if eh_chamada_de_ferramenta(pedaco):
            antes = self._esvaziar()
            prefixo = self._prefixo_apagar()
            linha = formatar_chamada_de_ferramenta(pedaco, self.cor)
            self.inicio_de_linha = True
            if self.cor:  # o agente vai chamar o modelo de novo: o indicador volta enquanto espera
                self.pensando_visivel = True
                self._pensando_desde = time.monotonic()
                linha += self.pensando()
            return prefixo + antes + linha
        if parece_diff(pedaco):
            antes = self._esvaziar()
            prefixo = self._prefixo_apagar()
            self.inicio_de_linha = True
            return antes + prefixo + colorir_diff(pedaco, self.cor)

        # (b) Erro ou aviso.
        if pedaco.startswith('Erro ('):
            antes = self._esvaziar()
            prefixo = self._prefixo_apagar()
            return antes + prefixo + self._erro(pedaco)
        if pedaco.startswith('Aviso:') or pedaco.startswith('Aviso '):
            antes = self._esvaziar()
            prefixo = self._prefixo_apagar()
            return antes + prefixo + self._aviso(pedaco)

        # (c) Texto do assistente: so sai linha completa, ja com o Markdown desenhado.
        self._buf += pedaco
        completas, quebra, self._buf = self._buf.rpartition('\n')
        if not quebra:
            return ''
        return self._emitir(''.join(self._md.linha(linha) + '\n' for linha in completas.split('\n')))

    def _emitir(self, texto: str) -> str:
        '''Texto do assistente pronto para a tela: o primeiro trecho do turno apaga o Pensando e ganha o marcador.'''
        if not texto:
            return ''
        prefixo = self._prefixo_apagar()  # apaga o 'Pensando' se ele estiver na tela (inclusive entre chamadas de ferramenta)
        if not self.assistente_iniciado:
            prefixo += self._acento('● ')
            self.assistente_iniciado = True
            self.inicio_de_linha = False
        return prefixo + self._indentar(texto)

    def _esvaziar(self) -> str:
        '''Descarrega a linha do assistente que ficou pela metade (antes de uma linha de status, erro ou aviso).'''
        if not self._buf:
            return ''
        resto, self._buf = self._buf, ''
        return self._emitir(self._md.linha(resto) + '\n')

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
