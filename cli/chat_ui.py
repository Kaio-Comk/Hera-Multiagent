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

    def apagar_linha(self) -> str:
        '''Devolve a sequencia para apagar a linha atual, ou vazio sem cor.'''
        if not self.cor:
            return ''
        return APAGAR

    # ------------------------------------------------------------------
    # Banner e reguas
    # ------------------------------------------------------------------
    def banner(self, *, versao: str, modelo: str, pasta: str, modo: str,
               maestro: bool) -> str:
        '''Monta a caixa de abertura do REPL com informacoes do ambiente.'''
        largura = min(self.largura, 64)
        # Largura interna util (entre as bordas).
        interno = max(1, largura - 2)

        # Linha 1: titulo com acento.
        titulo = '  ✻ Hera ' + versao
        # Linha 2: modelo ou maestro, com modo opcional.
        if maestro:
            base = 'modelo automatico (maestro)'
        else:
            base = modelo
        if modo:
            base = base + ' · modo ' + modo
        linha2 = '  ' + base
        # Linha 3: pasta, apagada e cortada pelo inicio se nao couber.
        pasta_visivel = _cortar_inicio(pasta, interno - 2)
        linha3 = '  ' + pasta_visivel

        linhas = []
        linhas.append(self._montar_linha(titulo, interno, acento=True))
        linhas.append(self._montar_linha(linha2, interno))
        linhas.append(self._montar_linha(linha3, interno, apagado=True))

        topo = '╭' + '─' * interno + '╮'
        base_borda = '╰' + '─' * interno + '╯'

        partes = [topo]
        partes.extend(linhas)
        partes.append(base_borda)
        caixa = '\n'.join(partes)

        dica = ' / comandos · @arquivo anexa · Ctrl+C interrompe · /exit sai'
        dica = self._apagado(dica)

        return caixa + '\n' + dica + '\n'

    def _montar_linha(self, conteudo: str, interno: int, *,
                      acento: bool = False, apagado: bool = False) -> str:
        '''Monta uma linha da caixa com bordas e preenchimento correto.'''
        visivel = _largura_visivel(conteudo)
        if visivel > interno:
            # Corta pelo inicio para caber.
            conteudo = _cortar_inicio(_sem_ansi(conteudo), interno)
            visivel = _largura_visivel(conteudo)
        preenchimento = ' ' * (interno - visivel)
        if acento:
            conteudo = self._acento(conteudo)
        elif apagado:
            conteudo = self._apagado(conteudo)
        return '│' + conteudo + preenchimento + '│'

    def regua(self) -> str:
        '''Devolve uma linha horizontal apagada com a largura do terminal.'''
        return self._apagado('─' * self.largura)

    def prompt(self) -> str:
        '''Devolve a regua seguida do marcador de entrada do usuario.'''
        if self.cor:
            marcador = self._acento('❯ ')
        else:
            marcador = '> '
        return self.regua() + '\n' + marcador

    def rodape(self, *, modelo: str, modo: str, maestro: bool, pasta: str) -> str:
        '''Devolve a regua seguida do resumo do estado atual.'''
        partes = []
        if maestro:
            partes.append('maestro')
        else:
            partes.append(modelo)
        if modo:
            partes.append(modo)
        nome_pasta = os.path.basename(pasta.rstrip('/')) or pasta
        partes.append(nome_pasta)
        linha = ' ' + ' · '.join(partes)
        return self.regua() + '\n' + self._apagado(linha)

    # ------------------------------------------------------------------
    # Estado do turno
    # ------------------------------------------------------------------
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
