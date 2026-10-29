from __future__ import annotations

import threading
from pathlib import Path

from app.memory.hashes import IndiceDeHash, hash_do_corpo


def _escritor(base: Path, contador: list[int]):
    def ler(caminho: Path) -> str | None:
        contador[0] += 1
        try:
            return caminho.read_text(encoding='utf-8')
        except OSError:
            return None
    return ler


def _criar(base: Path, nome: str, corpo: str) -> Path:
    p = base / nome
    p.write_text(corpo, encoding='utf-8')
    return p


def test_duplicata_encontrada(tmp_path: Path) -> None:
    _criar(tmp_path, 'a.md', 'corpo igual')
    _criar(tmp_path, 'b.md', 'outro')
    contador = [0]
    idx = IndiceDeHash(tmp_path, _escritor(tmp_path, contador))
    assert idx.buscar_duplicata(hash_do_corpo('corpo igual')) == 'a'


def test_excluir_ignora_proprio(tmp_path: Path) -> None:
    p = _criar(tmp_path, 'a.md', 'corpo igual')
    contador = [0]
    idx = IndiceDeHash(tmp_path, _escritor(tmp_path, contador))
    assert idx.buscar_duplicata(hash_do_corpo('corpo igual'), excluir=p) is None


def test_segunda_chamada_nao_rele(tmp_path: Path) -> None:
    _criar(tmp_path, 'a.md', 'x')
    contador = [0]
    idx = IndiceDeHash(tmp_path, _escritor(tmp_path, contador))
    idx.buscar_duplicata(hash_do_corpo('x'))
    leituras = contador[0]
    idx.buscar_duplicata(hash_do_corpo('x'))
    assert contador[0] == leituras


def test_arquivo_modificado_rele(tmp_path: Path) -> None:
    p = _criar(tmp_path, 'a.md', 'antigo')
    contador = [0]
    idx = IndiceDeHash(tmp_path, _escritor(tmp_path, contador))
    assert idx.buscar_duplicata(hash_do_corpo('antigo')) == 'a'
    p.write_text('conteudo bem maior e diferente', encoding='utf-8')
    assert idx.buscar_duplicata(hash_do_corpo('antigo')) is None
    assert idx.buscar_duplicata(hash_do_corpo('conteudo bem maior e diferente')) == 'a'


def test_arquivo_apagado_libera_hash(tmp_path: Path) -> None:
    p = _criar(tmp_path, 'a.md', 'x')
    contador = [0]
    idx = IndiceDeHash(tmp_path, _escritor(tmp_path, contador))
    assert idx.buscar_duplicata(hash_do_corpo('x')) == 'a'
    p.unlink()
    assert idx.buscar_duplicata(hash_do_corpo('x')) is None


def test_ilegivel_nunca_bate(tmp_path: Path) -> None:
    _criar(tmp_path, 'a.md', 'x')

    def ler(caminho: Path) -> str | None:
        return None

    idx = IndiceDeHash(tmp_path, ler)
    assert idx.buscar_duplicata(hash_do_corpo('x')) is None


def test_hash_do_corpo_normaliza() -> None:
    assert hash_do_corpo('  a ') == hash_do_corpo('a')
    assert hash_do_corpo('a') != hash_do_corpo('b')


def test_muitos_arquivos_sem_releitura(tmp_path: Path) -> None:
    for i in range(500):
        _criar(tmp_path, f'n{i}.md', f'corpo {i}')
    contador = [0]
    idx = IndiceDeHash(tmp_path, _escritor(tmp_path, contador))
    idx.buscar_duplicata(hash_do_corpo('inexistente'))
    leituras = contador[0]
    for _ in range(100):
        idx.buscar_duplicata(hash_do_corpo('inexistente'))
    assert contador[0] == leituras


def test_invalidar_forca_releitura(tmp_path: Path) -> None:
    p = _criar(tmp_path, 'a.md', 'x')
    contador = [0]
    idx = IndiceDeHash(tmp_path, _escritor(tmp_path, contador))
    idx.buscar_duplicata(hash_do_corpo('x'))
    leituras = contador[0]
    idx.invalidar(p)
    idx.buscar_duplicata(hash_do_corpo('x'))
    assert contador[0] > leituras


def test_threads_simultaneas(tmp_path: Path) -> None:
    for i in range(20):
        _criar(tmp_path, f'n{i}.md', f'corpo {i}')
    contador = [0]
    idx = IndiceDeHash(tmp_path, _escritor(tmp_path, contador))
    erros: list[BaseException] = []

    def worker() -> None:
        try:
            for _ in range(20):
                idx.buscar_duplicata(hash_do_corpo('corpo 1'))
        except BaseException as exc:
            erros.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not erros
