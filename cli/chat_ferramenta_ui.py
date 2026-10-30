# cli/chat_ferramenta_ui.py
'''Formatacao de chamadas de ferramenta e diffs para o chat do agente.'''

CIANO = '\x1b[38;5;37m'
CIANO_SIMPLES = '\x1b[36m'
NEGRITO = '\x1b[1m'
VERDE = '\x1b[32m'
VERMELHO = '\x1b[31m'
APAGADO = '\x1b[2m'
RESET = '\x1b[0m'

LIMITE_ARGS = 100

MAPA_VERBOS = {
    'executar_comando': 'Ran',
    'ler_arquivo': 'Explored 1 file',
    'listar_pasta': 'Explored',
    'buscar': 'Explored',
    'editar_arquivo': 'Edited 1 file',
    'escrever_arquivo': 'Edited 1 file',
    'usar_skill': 'Skill',
    'delegar_tarefa': 'Delegated',
}


def eh_chamada_de_ferramenta(pedaco: str) -> bool:
    '''True se o pedaco, sem espacos iniciais, comeca com '-> ' seguido de letra minuscula ou underscore.'''
    if not isinstance(pedaco, str):
        return False
    texto = pedaco.lstrip()
    if not texto.startswith('-> '):
        return False
    resto = texto[3:]
    if not resto:
        return False
    primeiro = resto[0]
    return primeiro.islower() or primeiro == '_'


def eh_pensamento(pedaco: str) -> bool:
    '''True se o pedaco representa uma linha de pensamento ('○ Thought ...', '○ Pensando ...' ou '-> pensamento ...').'''
    if not isinstance(pedaco, str):
        return False
    texto = pedaco.lstrip()
    return (
        texto.startswith('○ Thought')
        or texto.startswith('○ Pensando')
        or texto.startswith('-> pensamento')
    )


def formatar_pensamento(pedaco: str, cor: bool = False, duracao: str = '') -> str:
    '''Formata pensamento no estilo Antigravity CLI: ○ Thought for Xs (...) ou ○ Thought (...).'''
    texto = pedaco.strip()
    if texto.startswith('-> pensamento '):
        texto = texto[14:].strip()
    elif texto.startswith('○ Thought'):
        texto = texto[9:].strip()
        if texto.startswith('for '):
            partes = texto.split('(', 1)
            if len(partes) > 1:
                texto = '(' + partes[1]
    elif texto.startswith('○ Pensando'):
        texto = texto[10:].strip()

    if texto.startswith('(') and texto.endswith(')'):
        miolo = texto[1:-1]
    else:
        miolo = texto

    miolo = miolo.replace('\n', ' ').replace('\r', ' ')
    if len(miolo) > LIMITE_ARGS:
        miolo = miolo[:LIMITE_ARGS] + '…'

    rotulo = f'Thought for {duracao}' if duracao else 'Thought'
    conteudo = f'({miolo})' if miolo else ''

    if cor:
        marcador = APAGADO + '○ ' + rotulo + RESET
        corpo = (' ' + APAGADO + conteudo + RESET) if conteudo else ''
        return marcador + corpo + '\n'

    return f'○ {rotulo} {conteudo}\n' if conteudo else f'○ {rotulo}\n'


def formatar_chamada_de_ferramenta(pedaco: str, cor: bool = False) -> str:
    '''Formata uma chamada de ferramenta no estilo Antigravity CLI: ● Ran (cmd), ● Explored 1 file (path), etc.'''
    texto = pedaco.lstrip()
    if texto.startswith('-> '):
        texto = texto[3:]
    partes = texto.split(None, 1)
    if partes:
        nome = partes[0]
        args = partes[1] if len(partes) > 1 else ''
    else:
        nome = ''
        args = ''

    args = args.replace('\n', ' ').replace('\r', ' ')
    if len(args) > LIMITE_ARGS:
        args = args[:LIMITE_ARGS] + '…'

    prefixo_sub = nome.startswith('sub/')
    nome_base = nome[4:] if prefixo_sub else nome

    if nome_base in MAPA_VERBOS:
        verbo = MAPA_VERBOS[nome_base]
        if prefixo_sub:
            detalhe = f'(sub/{nome_base} {args})' if args else f'(sub/{nome_base})'
        else:
            detalhe = f'({args})' if args else '()'
    else:
        verbo = nome.capitalize() if nome.islower() else nome
        detalhe = f'({args})' if args else ''

    if cor:
        bullet = CIANO + '● ' + RESET
        titulo = NEGRITO + verbo + RESET
        corpo = (' ' + APAGADO + detalhe + RESET) if detalhe else ''
        return bullet + titulo + corpo + '\n'

    return f'● {verbo} {detalhe}\n' if detalhe else f'● {verbo}\n'


def formatar_colapso_acoes(
    total: int,
    leituras: int = 0,
    edicoes: int = 0,
    comandos: int = 0,
    falhas: int = 0,
    cor: bool = False,
) -> str:
    '''Formata acoes colapsadas no estilo: … N more actions (X reads, Y edits, Z commands: W failed) …'''
    detalhes = []
    if leituras:
        detalhes.append(f'{leituras} reads' if leituras != 1 else '1 read')
    if edicoes:
        detalhes.append(f'{edicoes} edits' if edicoes != 1 else '1 edit')
    if comandos:
        cmd = f'{comandos} commands' if comandos != 1 else '1 command'
        if falhas:
            cmd += f': {falhas} failed'
        detalhes.append(cmd)
    elif falhas:
        detalhes.append(f'{falhas} failed')

    sufixo = f' ({", ".join(detalhes)})' if detalhes else ''
    plural = 'actions' if total != 1 else 'action'
    texto = f'… {total} more {plural}{sufixo} …'
    if cor:
        return APAGADO + texto + RESET + '\n'
    return texto + '\n'


def parece_diff(pedaco: str) -> bool:
    '''True se alguma linha comeca com '@@ ', '+++ ' ou '--- ' (nao conta '- item' nem '---' sozinho).'''
    if not isinstance(pedaco, str):
        return False
    for linha in pedaco.split('\n'):
        if linha.startswith('@@ '):
            return True
        if linha.startswith('+++ '):
            return True
        if linha.startswith('--- '):
            return True
    return False


def _colorir_linha(linha: str, cor: bool) -> str:
    '''Colore uma unica linha do diff conforme o prefixo.'''
    if not cor:
        return linha
    if linha.startswith('+++') or linha.startswith('---'):
        return NEGRITO + linha + RESET
    if linha.startswith('@@'):
        return CIANO_SIMPLES + linha + RESET
    if linha.startswith('+'):
        return VERDE + linha + RESET
    if linha.startswith('-'):
        return VERMELHO + linha + RESET
    return linha


def colorir_diff(pedaco: str, cor: bool) -> str:
    '''Colore um diff linha a linha, indenta 2 espacos e garante '\\n' no fim.'''
    if not isinstance(pedaco, str):
        pedaco = ''
    linhas = pedaco.split('\n')
    # remove ultima linha vazia artificial se o texto ja terminava com \n
    if linhas and linhas[-1] == '':
        linhas = linhas[:-1]
    saida = []
    for linha in linhas:
        saida.append('  ' + _colorir_linha(linha, cor))
    resultado = '\n'.join(saida)
    if not resultado.endswith('\n'):
        resultado += '\n'
    return resultado
