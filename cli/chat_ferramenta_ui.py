# cli/chat_ferramenta_ui.py
'''Formatacao de chamadas de ferramenta e diffs para o chat do agente.'''

from pathlib import Path
from typing import Any

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
    '''True se o pedaco representa chamada de ferramenta ('-> ...' ou '● ...').'''
    if not isinstance(pedaco, str):
        return False
    texto = pedaco.lstrip()
    if texto.startswith('● '):
        return True
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
                dur_extraida = partes[0][4:].strip()
                if not duracao:
                    duracao = dur_extraida
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
    if texto.startswith('● '):
        resto = texto[2:].strip()
        partes = resto.split('(', 1)
        verbo = partes[0].strip()
        detalhe = ('(' + partes[1]) if len(partes) > 1 else ''
        if cor:
            bullet = CIANO + '● ' + RESET
            titulo = NEGRITO + verbo + RESET
            corpo_txt = detalhe
            if ': failed' in corpo_txt:
                corpo_txt = corpo_txt.replace(': failed', ': ' + VERMELHO + 'failed' + RESET + APAGADO)
            corpo = (' ' + APAGADO + corpo_txt + RESET) if corpo_txt else ''
            return bullet + titulo + corpo + '\n'
        return f'● {verbo} {detalhe}\n' if detalhe else f'● {verbo}\n'

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


def formatar_grupo_acoes(
    acoes: list[dict[str, Any]],
    cor: bool = False,
    expansivel: bool = False,
) -> str:
    r'''Formata um lote de chamadas de ferramenta executadas no estilo Antigravity CLI.

    Exemplos:
    ● Ran 9 commands (powershell -Command ... ×9)
    ● Edited 1 file (check_autofix_git.py)
    ● Ran 2 commands (python "C:\Projects\app... · powershell -Command ...: failed)
    ● Edited 1 file (test_webplayer_build.py)
    ● Ran 6 commands, checked 2 tasks (python "C:\Projects\app\..."...) (ctrl+o to expand)
    ● Explored 1 file (PlayerScreen.kt)
    ● Explored 4 files (PlayerScreen.kt, UiKit.kt ...)
    '''
    if not acoes:
        return ''

    def _limpar_arg(arg: Any) -> str:
        s = str(arg or '').replace('\r', ' ').replace('\n', ' ').strip()
        try:
            p = Path(s)
            if ('\\' in s or '/' in s) and len(s) > 25:
                s = p.name
        except Exception:
            pass
        return s

    tipos = [str(a.get('tipo', '')) for a in acoes]
    alvos = [_limpar_arg(a.get('alvo', '')) for a in acoes]
    falhas = [not bool(a.get('ok', True)) for a in acoes]
    houve_falha = any(falhas)
    total = len(acoes)

    # 1. Grupo uniforme de comandos
    if all(t == 'executar_comando' for t in tipos):
        cmd0 = alvos[0] if alvos else ''
        if total == 1:
            verbo = 'Ran'
            cmd_preview = cmd0 if len(cmd0) <= LIMITE_ARGS else (cmd0[:LIMITE_ARGS] + '…')
            detalhe = f'({cmd_preview}: failed)' if houve_falha else f'({cmd_preview})'
        else:
            verbo = f'Ran {total} commands'
            mesmo_cmd = all(alv == cmd0 for alv in alvos)
            if mesmo_cmd:
                preview = cmd0 if len(cmd0) <= 30 else (cmd0[:30] + '...')
                detalhe_corpo = f'{preview} ×{total}'
            else:
                p0 = cmd0 if len(cmd0) <= 22 else (cmd0[:22] + '...')
                if houve_falha:
                    falho_idx = falhas.index(True)
                    falho_cmd = alvos[falho_idx]
                    p1 = falho_cmd if len(falho_cmd) <= 22 else (falho_cmd[:22] + '...')
                    detalhe_corpo = f'{p0} · {p1}: failed'
                else:
                    detalhe_corpo = f'{p0} ... ×{total}'
            detalhe = f'({detalhe_corpo})'

    # 2. Grupo uniforme de edicao / escrita
    elif all(t in ('editar_arquivo', 'escrever_arquivo') for t in tipos):
        if total == 1:
            verbo = 'Edited 1 file'
            detalhe = f'({alvos[0]})'
        else:
            verbo = f'Edited {total} files'
            nomes = ', '.join(alvos[:3])
            if total > 3:
                nomes += '…'
            detalhe = f'({nomes})'

    # 3. Grupo uniforme de exploracao / leitura
    elif all(t in ('ler_arquivo', 'listar_pasta', 'buscar') for t in tipos):
        if total == 1:
            if tipos[0] == 'ler_arquivo':
                verbo = 'Explored 1 file'
                detalhe = f'({alvos[0]})'
            else:
                verbo = 'Explored'
                detalhe = f'({alvos[0]})'
        else:
            verbo = f'Explored {total} files'
            nomes = ', '.join(alvos[:3])
            if total > 3:
                nomes += '…'
            detalhe = f'({nomes})'

    # 4. Grupo misto (ex: comandos e tasks delegadas ou leituras)
    else:
        partes_verbo = []
        qtd_cmds = sum(1 for t in tipos if t == 'executar_comando')
        qtd_edits = sum(1 for t in tipos if t in ('editar_arquivo', 'escrever_arquivo'))
        qtd_reads = sum(1 for t in tipos if t in ('ler_arquivo', 'listar_pasta', 'buscar'))
        qtd_tasks = sum(1 for t in tipos if t in ('delegar_tarefa', 'task'))

        if qtd_cmds:
            partes_verbo.append(f'Ran {qtd_cmds} commands' if qtd_cmds > 1 else 'Ran 1 command')
        if qtd_tasks:
            partes_verbo.append(f'checked {qtd_tasks} tasks' if qtd_tasks > 1 else 'checked 1 task')
        if qtd_edits:
            partes_verbo.append(f'edited {qtd_edits} files' if qtd_edits > 1 else 'edited 1 file')
        if qtd_reads:
            partes_verbo.append(f'explored {qtd_reads} files' if qtd_reads > 1 else 'explored 1 file')

        verbo = ', '.join(partes_verbo) if partes_verbo else 'Ran actions'
        alvo0 = alvos[0] if alvos else ''
        if len(alvo0) > 35:
            alvo0 = alvo0[:35] + '...'
        detalhe = f'({alvo0})' if alvo0 else '()'

    if expansivel:
        detalhe = f'{detalhe} (ctrl+o to expand)'

    linha = f'● {verbo} {detalhe}' if detalhe else f'● {verbo}'
    if cor:
        bullet = CIANO + '● ' + RESET
        titulo = NEGRITO + verbo + RESET
        corpo_txt = detalhe
        if ': failed' in corpo_txt:
            corpo_txt = corpo_txt.replace(': failed', ': ' + VERMELHO + 'failed' + RESET + APAGADO)
        corpo = (' ' + APAGADO + corpo_txt + RESET) if corpo_txt else ''
        return bullet + titulo + corpo + '\n'

    return linha + '\n'


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
