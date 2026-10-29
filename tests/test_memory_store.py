"""Testes do MemoryStore."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.memory.schema import TIPOS
from app.memory.store import DuplicateNoteError, MemoryStore


def _fake() -> str:
    """Monta a chave falsa em tempo de execucao."""
    return 'sk-' + 'a1' * 12


def test_criar_e_ler(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    caminho = store.save('nota-um', 'descricao simples', 'usuario', 'corpo da nota')
    assert caminho.exists()
    nota = store.get('nota-um')
    assert nota.nome == 'nota-um'
    assert nota.descricao == 'descricao simples'
    assert nota.tipo == 'usuario'
    assert nota.corpo == 'corpo da nota'
    assert len(nota.data) == 10


def test_frontmatter_correto(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.save('nota-dois', 'minha descricao', 'projeto', 'linha1\nlinha2')
    texto = (tmp_path / 'nota-dois.md').read_text(encoding='utf-8')
    linhas = texto.split('\n')
    assert linhas[0] == '---'
    assert linhas[1] == 'name: nota-dois'
    assert linhas[2] == 'description: minha descricao'
    assert linhas[3] == 'type: projeto'
    assert linhas[4].startswith('date: ')
    assert linhas[5] == '---'
    assert linhas[6] == ''
    assert 'linha1' in texto and 'linha2' in texto


def test_segredo_nao_aparece(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    fake = _fake()
    store.save('nota-seg', f'desc {fake}', 'referencia', f'corpo com {fake} aqui')
    texto = (tmp_path / 'nota-seg.md').read_text(encoding='utf-8')
    assert fake not in texto
    nota = store.get('nota-seg')
    assert fake not in nota.corpo
    assert fake not in nota.descricao


def test_duplicado(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.save('nota-a', 'd', 'usuario', 'mesmo corpo')
    with pytest.raises(DuplicateNoteError):
        store.save('nota-b', 'd', 'usuario', 'mesmo corpo')


def test_mesmo_nome_sem_overwrite(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.save('nota-c', 'd', 'usuario', 'corpo 1')
    with pytest.raises(FileExistsError):
        store.save('nota-c', 'd', 'usuario', 'corpo 2')
    store.save('nota-c', 'd', 'usuario', 'corpo 2', overwrite=True)
    assert store.get('nota-c').corpo == 'corpo 2'


@pytest.mark.parametrize('nome', ['../x', 'a/b', 'A', '', 'x' * 65])
def test_nomes_invalidos(tmp_path: Path, nome: str) -> None:
    store = MemoryStore(tmp_path)
    with pytest.raises(ValueError):
        store.save(nome, 'd', 'usuario', 'corpo')
    assert list(tmp_path.glob('*.md')) == []


def test_tipo_invalido(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    with pytest.raises(ValueError):
        store.save('nota-t', 'd', 'invalido', 'corpo')
    assert 'invalido' not in TIPOS


def test_listar_ordem_e_ignora_txt(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.save('zeta', 'd', 'usuario', 'corpo z')
    store.save('alfa', 'd', 'usuario', 'corpo a')
    (tmp_path / 'ignorar.txt').write_text('nao e nota', encoding='utf-8')
    nomes = [n.nome for n in store.listar()]
    assert nomes == ['alfa', 'zeta']


def test_delete(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.save('nota-del', 'd', 'usuario', 'corpo')
    assert store.delete('nota-del') is True
    assert store.delete('nota-del') is False


def test_descricao_quebra_linha(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    store.save('nota-nl', 'linha1\nlinha2', 'usuario', 'corpo')
    nota = store.get('nota-nl')
    assert '\n' not in nota.descricao
    assert nota.descricao == 'linha1 linha2'


@pytest.mark.parametrize("nome", ["con", "nul", "aux", "prn", "com1", "lpt9", "abc\n"])
def test_nomes_reservados_do_windows_e_quebra_de_linha_sao_recusados(tmp_path, nome):
    store = MemoryStore(tmp_path)
    with pytest.raises(ValueError):
        store.save(nome, "d", "projeto", "corpo")
    assert list(tmp_path.iterdir()) == []


def test_gravar_muitas_notas_nao_e_quadratico(tmp_path):
    """Conta leituras de arquivo em vez de tempo: o Windows e lento para criar arquivos e o tempo oscila."""
    store = MemoryStore(tmp_path)
    leituras = 0
    original = store._ler_nota

    def contando(caminho):
        nonlocal leituras
        leituras += 1
        return original(caminho)

    store._ler_nota = contando
    for i in range(300):
        store.save(f'nota-{i}', f'desc {i}', 'projeto', f'corpo unico numero {i}')
    # com o cache cada save relê no maximo a nota recem-gravada; sem cache seriam ~45 mil leituras
    assert leituras <= 2 * 300
    assert len(store.listar()) == 300


def test_duplicata_continua_detectada_com_cache_e_edicao_externa(tmp_path):
    store = MemoryStore(tmp_path)
    store.save('a', 'd', 'projeto', 'texto original')
    with pytest.raises(DuplicateNoteError):
        store.save('b', 'd', 'projeto', 'texto original')
    # edicao externa (ex.: Obsidian) muda o corpo de 'a': a duplicata deixa de existir
    arq = tmp_path / 'a.md'
    arq.write_text(arq.read_text(encoding='utf-8').replace('texto original', 'texto editado por fora!'), encoding='utf-8')
    store.save('b', 'd', 'projeto', 'texto original')
    # apagar a nota tambem libera o corpo
    store.delete('b')
    store.save('c', 'd', 'projeto', 'texto original')
