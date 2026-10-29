from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path
from typing import Callable, Iterable

from app.memory.index import MemoryIndex
from app.memory.schema import TIPOS
from app.memory.store import DuplicateNoteError, MemoryStore


def pasta_padrao(environ: dict[str, str] | None = None) -> Path:
    """Retorna a pasta padrao da memoria."""
    env = os.environ if environ is None else environ
    valor = env.get('HERA_MEMORY_DIR')
    if valor:
        return Path(valor)
    return Path.home() / '.hera' / 'memoria'


def registrar(subparsers: argparse._SubParsersAction) -> None:
    """Registra o comando memory e seus subcomandos."""
    parser = subparsers.add_parser('memory', help='Gerencia a memoria')
    parser.add_argument('--pasta', default=None, help='Pasta da memoria')
    acoes = parser.add_subparsers(dest='acao', required=True)

    p_list = acoes.add_parser('list', help='Lista notas')
    p_list.add_argument('--tipo', default=None, help='Filtra por tipo')

    p_search = acoes.add_parser('search', help='Busca notas')
    p_search.add_argument('consulta', help='Texto da busca')
    p_search.add_argument('--limite', type=int, default=5, help='Limite de resultados')

    p_show = acoes.add_parser('show', help='Mostra uma nota')
    p_show.add_argument('nome', help='Nome da nota')

    p_add = acoes.add_parser('add', help='Adiciona uma nota')
    p_add.add_argument('nome', help='Nome da nota')
    p_add.add_argument('--descricao', required=True, help='Descricao')
    p_add.add_argument('--tipo', required=True, help='Tipo')
    grupo = p_add.add_mutually_exclusive_group(required=True)
    grupo.add_argument('--corpo', default=None, help='Corpo da nota')
    grupo.add_argument('--arquivo', default=None, help='Arquivo com o corpo')
    p_add.add_argument('--sobrescrever', action='store_true', help='Sobrescreve')

    p_forget = acoes.add_parser('forget', help='Apaga uma nota')
    p_forget.add_argument('nome', help='Nome da nota')
    p_forget.add_argument('--sim', action='store_true', help='Nao pergunta')

    p_export = acoes.add_parser('export', help='Exporta notas')
    p_export.add_argument('destino', help='Pasta de destino')


def _erro(saida: Callable[[str], None], mensagem: str) -> int:
    saida(mensagem)
    return 1


def _validar_tipo(tipo: str) -> bool:
    return tipo in TIPOS


def _executar_list(store: MemoryStore, args: argparse.Namespace, saida: Callable[[str], None]) -> int:
    if args.tipo is not None and not _validar_tipo(args.tipo):
        return _erro(saida, f'Tipo invalido: {args.tipo}')
    notas = store.listar()
    if args.tipo is not None:
        notas = [n for n in notas if n.tipo == args.tipo]
    if not notas:
        saida('Nenhuma nota na memoria.')
        return 0
    for nota in notas:
        saida(f'{nota.nome} | {nota.tipo} | {nota.data} | {nota.descricao}')
    return 0


def _executar_search(index: MemoryIndex, args: argparse.Namespace, saida: Callable[[str], None]) -> int:
    resultados = index.buscar(args.consulta, limite=args.limite)
    if not resultados:
        saida('Nada encontrado.')
        return 0
    for r in resultados:
        saida(f'{r.nome} [{r.tipo}] {r.score}  {r.trecho}')
    return 0


def _executar_show(store: MemoryStore, args: argparse.Namespace, saida: Callable[[str], None]) -> int:
    try:
        nota = store.get(args.nome)
    except FileNotFoundError:
        return _erro(saida, f'Nota nao encontrada: {args.nome}')
    saida(f'Nome: {nota.nome}')
    saida(f'Tipo: {nota.tipo}')
    saida(f'Data: {nota.data}')
    saida(f'Descricao: {nota.descricao}')
    saida('')
    saida(nota.corpo)
    return 0


def _executar_add(store: MemoryStore, args: argparse.Namespace, saida: Callable[[str], None]) -> int:
    if args.arquivo is not None:
        try:
            corpo = Path(args.arquivo).read_text(encoding='utf-8')
        except FileNotFoundError:
            return _erro(saida, f'Arquivo nao encontrado: {args.arquivo}')
        except (UnicodeDecodeError, IsADirectoryError, PermissionError) as exc:
            return _erro(saida, f'Nao foi possivel ler o arquivo ({type(exc).__name__}): {args.arquivo}')
    else:
        corpo = args.corpo
    try:
        store.save(args.nome, args.descricao, args.tipo, corpo, overwrite=args.sobrescrever)
    except ValueError as exc:
        return _erro(saida, f'Erro: {exc}')
    except FileExistsError:
        return _erro(saida, f'Nota ja existe: {args.nome}')
    except DuplicateNoteError:
        return _erro(saida, 'Conteudo duplicado de outra nota.')
    saida(f'Nota salva: {args.nome}')
    try:
        gravada = store.get(args.nome)
    except FileNotFoundError:
        return 0
    if gravada.corpo != corpo:
        saida('Aviso: segredos foram removidos do texto.')
    return 0


def _executar_forget(
    store: MemoryStore,
    args: argparse.Namespace,
    saida: Callable[[str], None],
    entrada: Callable[[str], str],
) -> int:
    try:
        store.get(args.nome)
    except FileNotFoundError:
        return _erro(saida, 'Nota nao encontrada.')
    if not args.sim:
        try:
            resposta = entrada(f'Apagar a nota {args.nome}? [s/N] ')
        except EOFError:
            resposta = ''
        if resposta.strip().lower() not in ('s', 'sim'):
            saida('Operacao cancelada.')
            return 0
    store.delete(args.nome)
    saida(f'Nota apagada: {args.nome}')
    return 0


def _executar_export(store: MemoryStore, args: argparse.Namespace, saida: Callable[[str], None]) -> int:
    origem = store.base_dir.resolve()
    destino = Path(args.destino).resolve()
    if destino == origem or origem in destino.parents:
        return _erro(saida, 'Destino nao pode ser a propria pasta da memoria nem estar dentro dela.')
    destino.mkdir(parents=True, exist_ok=True)
    destino_store = MemoryStore(destino)
    total = 0
    ignoradas = 0
    for nota in store.listar():
        try:
            destino_store.save(nota.nome, nota.descricao, nota.tipo, nota.corpo, overwrite=False)
            total += 1
        except (FileExistsError, DuplicateNoteError, ValueError):
            ignoradas += 1
    aviso = f' ({ignoradas} ignorada(s): ja existiam no destino ou duplicadas)' if ignoradas else ''
    saida(f'{total} nota(s) exportada(s){aviso}.')
    return 0


def executar(
    args: argparse.Namespace,
    *,
    saida: Callable[[str], None] = print,
    entrada: Callable[[str], str] = input,
    environ: dict[str, str] | None = None,
) -> int:
    """Executa o comando memory."""
    pasta = Path(args.pasta) if args.pasta else pasta_padrao(environ)
    store = MemoryStore(pasta)
    index = MemoryIndex(store, pasta / '.indice.sqlite')
    try:
        acao = args.acao
        if acao == 'list':
            return _executar_list(store, args, saida)
        if acao == 'search':
            return _executar_search(index, args, saida)
        if acao == 'show':
            return _executar_show(store, args, saida)
        if acao == 'add':
            return _executar_add(store, args, saida)
        if acao == 'forget':
            return _executar_forget(store, args, saida, entrada)
        if acao == 'export':
            return _executar_export(store, args, saida)
        return _erro(saida, f'Acao desconhecida: {acao}')
    except ValueError as exc:
        return _erro(saida, f'Erro: {exc}')
    finally:
        index.close()
