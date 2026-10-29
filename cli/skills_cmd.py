from __future__ import annotations

import argparse
import difflib
from pathlib import Path
from typing import Callable, Sequence

from app.skills import (
    RelatorioImportacao,
    Skill,
    descobrir_skills,
    importar_do_claude,
    ler_skill,
    resolver_skill,
)

MAX_AVISOS = 10      # mais que isso vira so uma contagem


def pastas_de_skills(cwd: Path | None = None, home: Path | None = None) -> list[Path]:
    '''Retorna as pastas de skills, projeto antes de usuario.'''
    base_cwd = Path.cwd() if cwd is None else Path(cwd)
    base_home = Path.home() if home is None else Path(home)
    return [base_cwd / '.hera' / 'skills', base_home / '.hera' / 'skills']


def registrar(subparsers: argparse._SubParsersAction) -> None:
    '''Registra o comando skills e seus subcomandos.'''
    parser = subparsers.add_parser('skills', help='Gerencia skills locais')
    acoes = parser.add_subparsers(dest='acao', required=True)

    acoes.add_parser('list', help='Lista skills disponiveis')

    imp = acoes.add_parser('import', help='Importa skills do Claude')
    imp.add_argument('--origem', default=None, help='Pasta de origem das skills')
    imp.add_argument('--destino', default=None, help='Pasta de destino das skills')
    imp.add_argument('--sobrescrever', action='store_true', help='Sobrescreve existentes')

    show = acoes.add_parser('show', help='Mostra detalhes de uma skill')
    show.add_argument('nome', help='Nome da skill')


def _descricao_curta(descricao: str, limite: int = 60) -> str:
    '''Encurta a descricao para exibicao.'''
    texto = ' '.join(descricao.split())
    if len(texto) <= limite:
        return texto
    return texto[: limite - 3] + '...'


def _listar(skills: Sequence[Skill], saida: Callable[[str], None]) -> int:
    '''Imprime a lista de skills.'''
    if not skills:
        saida('Nenhuma skill. Importe as do Claude com: hera skills import')
        return 0
    for skill in skills:
        saida(f'{skill.nome} | {_descricao_curta(skill.descricao)} | {skill.origem}-{skill.pasta}')
        for aviso in skill.avisos:
            saida(f'  aviso: {aviso}')
    return 0


def _importar(args: argparse.Namespace, saida: Callable[[str], None], home: Path) -> int:
    '''Executa a importacao de skills do Claude.'''
    origem = Path(args.origem) if args.origem else home / '.claude' / 'skills'
    destino = Path(args.destino) if args.destino else home / '.hera' / 'skills'
    relatorio: RelatorioImportacao = importar_do_claude(
        origem, destino, sobrescrever=args.sobrescrever
    )
    saida(f'Copiadas: {len(relatorio.copiadas)}')
    saida(f'Ignoradas (ja existiam): {len(relatorio.ignoradas)}')
    saida(f'Sobrescritas: {len(relatorio.sobrescritas)}')
    for aviso in relatorio.avisos[:MAX_AVISOS]:
        saida(aviso)
    if len(relatorio.avisos) > MAX_AVISOS:
        saida(f'... e mais {len(relatorio.avisos) - MAX_AVISOS} aviso(s).')
    saida(f'As originais em {origem} nao foram alteradas.')
    return 0


def _sugerir(nome: str, skills: Sequence[Skill], limite: int = 3) -> list[str]:
    '''Sugere nomes parecidos com o informado.'''
    nomes = [s.nome for s in skills]
    return difflib.get_close_matches(nome, nomes, n=limite, cutoff=0.4)


def _show(args: argparse.Namespace, skills: Sequence[Skill], saida: Callable[[str], None]) -> int:
    '''Mostra detalhes de uma skill.'''
    skill = resolver_skill(args.nome, skills)
    if skill is None:
        saida(f'Skill nao encontrada: {args.nome}')
        sugestoes = _sugerir(args.nome, skills)
        if sugestoes:
            saida('Voce quis dizer: ' + ', '.join(sugestoes))
        return 1
    saida(f'Nome: {skill.nome}')
    saida(f'Descricao: {skill.descricao}')
    saida(f'Caminho: {skill.caminho}')
    for aviso in skill.avisos:
        saida(f'aviso: {aviso}')
    saida('')
    saida(ler_skill(skill))
    return 0


def executar(
    args: argparse.Namespace,
    *,
    saida: Callable[[str], None] = print,
    cwd: Path | None = None,
    home: Path | None = None,
) -> int:
    '''Executa a acao solicitada do comando skills.'''
    base_home = Path.home() if home is None else Path(home)
    pastas = pastas_de_skills(cwd, base_home)
    skills = descobrir_skills(pastas)

    acao = getattr(args, 'acao', None)
    if acao == 'list':
        return _listar(skills, saida)
    if acao == 'import':
        return _importar(args, saida, base_home)
    if acao == 'show':
        return _show(args, skills, saida)
    saida(f'Acao desconhecida: {acao}')
    return 1


def texto_para_comando_skill(
    nome: str, skills: Sequence[Skill], argumentos: str = ''
) -> str | None:
    '''Monta o texto enviado ao modelo para uma skill via /nome.'''
    skill = resolver_skill(nome, skills)
    if skill is None:
        return None
    corpo = ler_skill(skill)
    pedido = argumentos if argumentos else 'siga a skill'
    return (
        f'Use a skill "{skill.nome}". Instrucoes da skill ({skill.caminho}):\n'
        f'{corpo}\n\nPedido do usuario: {pedido}'
    )
