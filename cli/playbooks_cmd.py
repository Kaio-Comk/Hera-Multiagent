from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable

from app.memory.playbooks_aprendidos import PlaybooksAprendidos


def caminho_padrao() -> Path:
    """Arquivo dos playbooks aprendidos."""
    return Path.home() / '.hera' / 'playbooks_aprendidos.json'


def registrar(subparsers: argparse._SubParsersAction) -> None:
    """Registra o comando playbooks e seus subcomandos."""
    parser = subparsers.add_parser('playbooks', help='Playbooks aprendidos (propostas precisam de aprovacao)')
    parser.add_argument('--arquivo', default=None, help='Arquivo dos playbooks')
    acoes = parser.add_subparsers(dest='acao', required=True)
    acoes.add_parser('list', help='Lista propostas e playbooks ativos')
    for nome, ajuda in (('aprovar', 'Ativa uma proposta'), ('recusar', 'Recusa e nunca mais propoe'),
                        ('desativar', 'Desativa um playbook ativo')):
        p = acoes.add_parser(nome, help=ajuda)
        p.add_argument('id', help='Id do playbook')


def _linha(item: dict) -> str:
    passos = ' > '.join(f"{p.get('engine')}/{p.get('model')}" for p in item.get('passos', []))
    return f"{item['id']} | {item['tipo_tarefa']} | {passos}"


def executar(args: argparse.Namespace, saida: Callable[[str], None] = print) -> int:
    """Executa o subcomando de playbooks."""
    pb = PlaybooksAprendidos(Path(args.arquivo) if args.arquivo else caminho_padrao())
    if args.acao == 'list':
        saida('Propostas (aguardam aprovacao):')
        for item in pb.propostas():
            saida('  ' + _linha(item))
        saida('Ativos:')
        for item in pb.ativos():
            saida('  ' + _linha(item))
        return 0
    feito = {'aprovar': pb.aprovar, 'recusar': pb.recusar, 'desativar': pb.desativar}[args.acao](args.id)
    saida('ok' if feito else f'id nao encontrado: {args.id}')
    return 0 if feito else 1
