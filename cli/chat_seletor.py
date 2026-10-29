# cli/chat_seletor.py
'''Seletor de lista interativo no terminal.

Mostra uma janela rolante de opcoes, permite filtrar por substring e
nao deixa nada na tela quando termina (erase_when_done=True).
'''

from __future__ import annotations

from typing import Callable

from prompt_toolkit.application import Application
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style


ESTILO = Style.from_dict(
    {
        'titulo': 'bold',
        'selecionado': 'fg:ansicyan bold',
        'apagado': 'fg:ansibrightblack',
    }
)


def _filtrar(
    opcoes: list[tuple[str, str]], filtro: str
) -> list[tuple[str, str]]:
    '''Filtra opcoes por substring (case-insensitive) no valor ou rotulo.'''
    if not filtro:
        return list(opcoes)
    alvo = filtro.lower()
    return [
        (valor, rotulo)
        for valor, rotulo in opcoes
        if alvo in valor.lower() or alvo in rotulo.lower()
    ]


def desenhar(
    opcoes_filtradas: list[tuple[str, str]],
    indice: int,
    visiveis: int,
    atual: str | None,
) -> list[tuple[str, str]]:
    '''Monta o texto formatado da janela de itens.

    Devolve uma lista de tuplas (estilo, texto) prontas para
    FormattedText. Mostra no maximo `visiveis` itens ao redor do indice.
    '''
    linhas: list[tuple[str, str]] = []
    total = len(opcoes_filtradas)
    if total == 0:
        return linhas

    visiveis = max(1, visiveis)
    if total <= visiveis:
        inicio = 0
        fim = total
    else:
        # Centraliza a janela no indice selecionado.
        metade = visiveis // 2
        inicio = indice - metade
        if inicio < 0:
            inicio = 0
        fim = inicio + visiveis
        if fim > total:
            fim = total
            inicio = fim - visiveis

    acima = inicio
    abaixo = total - fim

    def quebra() -> None:
        if linhas:
            linhas.append(('', chr(10)))

    if acima > 0:
        linhas.append(('class:apagado', f'  \u2191 {acima} acima'))

    for pos in range(inicio, fim):
        valor, rotulo = opcoes_filtradas[pos]
        sufixo = ' (atual)' if atual is not None and valor == atual else ''
        quebra()
        if pos == indice:
            linhas.append(('class:selecionado', '\u276f '))
            linhas.append(('class:selecionado', f'{rotulo}{sufixo}'))
        else:
            linhas.append(('', f'  {rotulo}{sufixo}'))

    if abaixo > 0:
        quebra()
        linhas.append(('class:apagado', f'  \u2193 {abaixo} abaixo'))

    return linhas


async def selecionar(
    titulo: str,
    opcoes: list[tuple[str, str]],
    atual: str | None = None,
    *,
    visiveis: int = 10,
    entrada=None,
    saida=None,
) -> str | None:
    '''Exibe o seletor e devolve o valor escolhido ou None se cancelar.'''
    estado: dict = {'filtro': '', 'indice': 0, 'resultado': None}

    # Indice inicial = posicao de `atual` (ou 0).
    if atual is not None:
        for i, (valor, _rotulo) in enumerate(opcoes):
            if valor == atual:
                estado['indice'] = i
                break

    def _filtradas() -> list[tuple[str, str]]:
        return _filtrar(opcoes, estado['filtro'])

    def _clamp() -> None:
        lista = _filtradas()
        if not lista:
            estado['indice'] = 0
        elif estado['indice'] >= len(lista):
            estado['indice'] = len(lista) - 1
        elif estado['indice'] < 0:
            estado['indice'] = 0

    def _texto_itens() -> FormattedText:
        lista = _filtradas()
        return FormattedText(
            desenhar(lista, estado['indice'], visiveis, atual)
        )

    def _texto_filtro() -> FormattedText:
        if not estado['filtro']:
            return FormattedText([])
        return FormattedText([('class:apagado', f'filtro: {estado["filtro"]}')])

    def _texto_dica() -> FormattedText:
        return FormattedText(
            [
                (
                    'class:apagado',
                    'setas navegam \u00b7 digite para filtrar \u00b7 '
                    'Enter escolhe \u00b7 Esc cancela',
                )
            ]
        )

    kb = KeyBindings()

    @kb.add('up')
    @kb.add('c-p')
    def _cima(event) -> None:
        estado['indice'] -= 1
        _clamp()

    @kb.add('down')
    @kb.add('c-n')
    def _baixo(event) -> None:
        estado['indice'] += 1
        _clamp()

    @kb.add('pageup')
    def _pgup(event) -> None:
        estado['indice'] -= visiveis
        _clamp()

    @kb.add('pagedown')
    def _pgdn(event) -> None:
        estado['indice'] += visiveis
        _clamp()

    @kb.add('home')
    def _home(event) -> None:
        estado['indice'] = 0

    @kb.add('end')
    def _end(event) -> None:
        lista = _filtradas()
        estado['indice'] = max(0, len(lista) - 1)

    @kb.add('backspace')
    def _backspace(event) -> None:
        if estado['filtro']:
            estado['filtro'] = estado['filtro'][:-1]
            estado['indice'] = 0
            _clamp()

    @kb.add('enter')
    def _enter(event) -> None:
        lista = _filtradas()
        if not lista:
            return
        _clamp()
        estado['resultado'] = lista[estado['indice']][0]
        event.app.exit(result=estado['resultado'])

    @kb.add('escape')
    def _escape(event) -> None:
        estado['resultado'] = None
        event.app.exit(result=None)

    @kb.add('c-c')
    def _ctrl_c(event) -> None:
        estado['resultado'] = None
        event.app.exit(result=None)

    @kb.add('<any>')
    def _char(event) -> None:
        dados = event.data
        if not dados:
            return
        # Aceita apenas caracteres imprimiveis.
        if dados.isprintable():
            estado['filtro'] += dados
            estado['indice'] = 0
            _clamp()

    janela_titulo = Window(
        FormattedTextControl(
            lambda: FormattedText([('class:titulo', titulo)])
        ),
        height=1,
    )
    janela_itens = Window(FormattedTextControl(_texto_itens))
    janela_filtro = Window(FormattedTextControl(_texto_filtro), height=1)
    janela_dica = Window(FormattedTextControl(_texto_dica), height=1)

    layout = Layout(
        HSplit([janela_titulo, janela_itens, janela_filtro, janela_dica])
    )

    app: Application = Application(
        layout=layout,
        key_bindings=kb,
        style=ESTILO,
        full_screen=False,
        erase_when_done=True,
        input=entrada,
        output=saida,
    )

    return await app.run_async()
