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


def formatar_chamada_de_ferramenta(pedaco: str, cor: bool) -> str:
    '''Formata uma chamada de ferramenta como '  ↳ nome args\\n'.'''
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
    # quebras de linha dentro dos argumentos viram espaco
    args = args.replace('\n', ' ').replace('\r', ' ')
    if len(args) > LIMITE_ARGS:
        args = args[:LIMITE_ARGS] + '…'
    if cor:
        return '  ↳ ' + CIANO + nome + RESET + ' ' + APAGADO + args + RESET + '\n'
    return '  ↳ ' + nome + ' ' + args + '\n'


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
