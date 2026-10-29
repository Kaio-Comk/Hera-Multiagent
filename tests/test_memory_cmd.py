from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from cli.memory_cmd import executar, pasta_padrao, registrar


def montar_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='comando')
    registrar(subparsers)
    return parser


def rodar(argv: list[str], *, entrada_resposta: str = 'n', environ: dict[str, str] | None = None):
    parser = montar_parser()
    args = parser.parse_args(argv)
    saida: list[str] = []
    codigo = executar(
        args,
        saida=saida.append,
        entrada=lambda _: entrada_resposta,
        environ=environ if environ is not None else {},
    )
    return codigo, saida


def test_pasta_padrao_com_variavel(tmp_path: Path) -> None:
    destino = tmp_path / 'mem'
    assert pasta_padrao({'HERA_MEMORY_DIR': str(destino)}) == destino


def test_pasta_padrao_sem_variavel() -> None:
    assert pasta_padrao({}) == Path.home() / '.hera' / 'memoria'


def test_pasta_padrao_variavel_vazia() -> None:
    assert pasta_padrao({'HERA_MEMORY_DIR': ''}) == Path.home() / '.hera' / 'memoria'


def test_add_list_show_search(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    base = ['memory', '--pasta', str(pasta)]

    codigo, saida = rodar(base + ['add', 'nota1', '--descricao', 'primeira', '--tipo', 'projeto', '--corpo', 'conteudo alfa'])
    assert codigo == 0
    assert 'Nota salva: nota1' in saida

    codigo, saida = rodar(base + ['list'])
    assert codigo == 0
    assert any('nota1 | projeto' in linha for linha in saida)

    codigo, saida = rodar(base + ['show', 'nota1'])
    assert codigo == 0
    assert any('conteudo alfa' in linha for linha in saida)

    codigo, saida = rodar(base + ['search', 'alfa'])
    assert codigo == 0
    assert any('nota1' in linha for linha in saida)


def test_add_duplicado(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    base = ['memory', '--pasta', str(pasta)]
    rodar(base + ['add', 'n', '--descricao', 'd', '--tipo', 'projeto', '--corpo', 'x'])
    codigo, saida = rodar(base + ['add', 'n', '--descricao', 'd', '--tipo', 'projeto', '--corpo', 'y'])
    assert codigo == 1
    assert any('ja existe' in linha for linha in saida)


def test_add_sem_sobrescrever(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    base = ['memory', '--pasta', str(pasta)]
    rodar(base + ['add', 'n', '--descricao', 'd', '--tipo', 'projeto', '--corpo', 'x'])
    codigo, _ = rodar(base + ['add', 'n', '--descricao', 'd2', '--tipo', 'projeto', '--corpo', 'y'])
    assert codigo == 1


def test_add_sobrescrever(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    base = ['memory', '--pasta', str(pasta)]
    rodar(base + ['add', 'n', '--descricao', 'd', '--tipo', 'projeto', '--corpo', 'x'])
    codigo, saida = rodar(base + ['add', 'n', '--descricao', 'd2', '--tipo', 'projeto', '--corpo', 'y', '--sobrescrever'])
    assert codigo == 0
    assert 'Nota salva: n' in saida


def test_add_arquivo(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    arquivo = tmp_path / 'corpo.txt'
    arquivo.write_text('texto do arquivo', encoding='utf-8')
    base = ['memory', '--pasta', str(pasta)]
    codigo, saida = rodar(base + ['add', 'n', '--descricao', 'd', '--tipo', 'projeto', '--arquivo', str(arquivo)])
    assert codigo == 0
    codigo, saida = rodar(base + ['show', 'n'])
    assert codigo == 0
    assert any('texto do arquivo' in linha for linha in saida)


def test_segredo_removido(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    chave = 'sk-' + 'a1' * 12
    base = ['memory', '--pasta', str(pasta)]
    codigo, saida = rodar(base + ['add', 'n', '--descricao', 'd', '--tipo', 'projeto', '--corpo', f'token {chave} fim'])
    assert codigo == 0
    assert any('Aviso: segredos foram removidos do texto.' in linha for linha in saida)


def test_forget_confirmacao_negada(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    base = ['memory', '--pasta', str(pasta)]
    rodar(base + ['add', 'n', '--descricao', 'd', '--tipo', 'projeto', '--corpo', 'x'])
    codigo, saida = rodar(base + ['forget', 'n'], entrada_resposta='n')
    assert codigo == 0
    codigo, saida = rodar(base + ['show', 'n'])
    assert codigo == 0


def test_forget_confirmacao_aceita(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    base = ['memory', '--pasta', str(pasta)]
    rodar(base + ['add', 'n', '--descricao', 'd', '--tipo', 'projeto', '--corpo', 'x'])
    codigo, saida = rodar(base + ['forget', 'n'], entrada_resposta='s')
    assert codigo == 0
    codigo, saida = rodar(base + ['show', 'n'])
    assert codigo == 1


def test_forget_sim(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    base = ['memory', '--pasta', str(pasta)]
    rodar(base + ['add', 'n', '--descricao', 'd', '--tipo', 'projeto', '--corpo', 'x'])
    codigo, saida = rodar(base + ['forget', 'n', '--sim'])
    assert codigo == 0
    codigo, _ = rodar(base + ['show', 'n'])
    assert codigo == 1


def test_forget_inexistente(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    base = ['memory', '--pasta', str(pasta)]
    codigo, saida = rodar(base + ['forget', 'naoexiste', '--sim'])
    assert codigo == 1
    assert any('Nota nao encontrada.' in linha for linha in saida)


def test_tipo_invalido(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    base = ['memory', '--pasta', str(pasta)]
    codigo, saida = rodar(base + ['list', '--tipo', 'invalido'])
    assert codigo == 1
    assert any('Tipo invalido' in linha for linha in saida)


def test_nome_invalido(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    base = ['memory', '--pasta', str(pasta)]
    codigo, saida = rodar(base + ['add', '../x', '--descricao', 'd', '--tipo', 'projeto', '--corpo', 'x'])
    assert codigo == 1


def test_export_copia(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    destino = tmp_path / 'saida'
    base = ['memory', '--pasta', str(pasta)]
    rodar(base + ['add', 'n1', '--descricao', 'd', '--tipo', 'projeto', '--corpo', 'x'])
    rodar(base + ['add', 'n2', '--descricao', 'd', '--tipo', 'projeto', '--corpo', 'y'])
    codigo, saida = rodar(base + ['export', str(destino)])
    assert codigo == 0
    assert any('2 nota(s) exportada(s).' in linha for linha in saida)
    assert (destino / 'n1.md').exists()
    assert (destino / 'n2.md').exists()


def test_export_recusa_dentro(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    base = ['memory', '--pasta', str(pasta)]
    rodar(base + ['add', 'n1', '--descricao', 'd', '--tipo', 'projeto', '--corpo', 'x'])
    codigo, saida = rodar(base + ['export', str(pasta / 'sub')])
    assert codigo == 1
    assert any('Destino nao pode' in linha for linha in saida)


def test_list_vazio(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    base = ['memory', '--pasta', str(pasta)]
    codigo, saida = rodar(base + ['list'])
    assert codigo == 0
    assert any('Nenhuma nota na memoria.' in linha for linha in saida)


def test_add_arquivo_que_nao_e_utf8_da_erro_claro(tmp_path: Path) -> None:
    ruim = tmp_path / 'ruim.txt'
    ruim.write_bytes(b'\xff\xfe\x00\x80 conteudo invalido')
    base = ['memory', '--pasta', str(tmp_path / 'memoria')]
    codigo, saida = rodar(base + ['add', 'n1', '--descricao', 'd', '--tipo', 'projeto', '--arquivo', str(ruim)])
    assert codigo == 1
    assert any('ler o arquivo' in linha for linha in saida)


def test_add_arquivo_que_e_pasta_da_erro_claro(tmp_path: Path) -> None:
    base = ['memory', '--pasta', str(tmp_path / 'memoria')]
    codigo, saida = rodar(base + ['add', 'n1', '--descricao', 'd', '--tipo', 'projeto', '--arquivo', str(tmp_path)])
    assert codigo == 1
    assert any('ler o arquivo' in linha for linha in saida)


def test_forget_com_entrada_fechada_cancela_sem_apagar(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    base = ['memory', '--pasta', str(pasta)]
    rodar(base + ['add', 'n1', '--descricao', 'd', '--tipo', 'projeto', '--corpo', 'x'])
    args = montar_parser().parse_args(base + ['forget', 'n1'])
    saida: list[str] = []

    def fechada(_: str) -> str:
        raise EOFError

    assert executar(args, saida=saida.append, entrada=fechada, environ={}) == 0
    assert (pasta / 'n1.md').exists()
    assert any('cancelada' in linha for linha in saida)


def test_export_avisa_quantas_ficaram_de_fora(tmp_path: Path) -> None:
    pasta = tmp_path / 'memoria'
    destino = tmp_path / 'saida'
    base = ['memory', '--pasta', str(pasta)]
    rodar(base + ['add', 'n1', '--descricao', 'd', '--tipo', 'projeto', '--corpo', 'x'])
    rodar(base + ['export', str(destino)])
    codigo, saida = rodar(base + ['export', str(destino)])
    assert codigo == 0
    assert any('0 nota(s) exportada(s)' in linha and '1 ignorada(s)' in linha for linha in saida)


@pytest.mark.parametrize('acao', [['show', '../x'], ['forget', '../x', '--sim'], ['show', 'A']])
def test_nome_invalido_em_show_e_forget_da_erro_claro(tmp_path: Path, acao: list[str]) -> None:
    codigo, saida = rodar(['memory', '--pasta', str(tmp_path / 'memoria')] + acao)
    assert codigo == 1
    assert any('nome invalido' in linha for linha in saida)
