from __future__ import annotations

import pytest

from app.memory.index import MemoryIndex
from app.memory.recall import Snapshot, estimar_tokens, montar_snapshot
from app.memory.store import MemoryStore


@pytest.fixture()
def store(tmp_path):
    return MemoryStore(str(tmp_path))


@pytest.fixture()
def index(store):
    return MemoryIndex(store)


def _popular(store):
    store.save('alfa', 'nota sobre python', 'projeto', 'corpo alfa ' + 'x' * 50)
    store.save('beta', 'nota sobre testes', 'referencia', 'corpo beta ' + 'y' * 50)
    store.save('gama', 'nota sobre deploy', 'feedback', 'corpo gama ' + 'z' * 50)


def test_estimar_tokens():
    assert estimar_tokens('') == 0
    assert estimar_tokens('abcd') == 1
    assert estimar_tokens('abcde') == 2


def test_ativo_false_nao_busca():
    class Falso:
        def buscar(self, consulta, limite=5):
            raise AssertionError('nao deveria buscar')

    snap = montar_snapshot(Falso(), 'qualquer', ativo=False)
    assert snap == Snapshot('', (), False)


def test_max_chars_zero_devolve_vazio(index):
    snap = montar_snapshot(index, 'python', max_chars=0)
    assert snap == Snapshot('', (), False)


def test_sem_resultados_devolve_vazio(index):
    snap = montar_snapshot(index, 'inexistente_xyz')
    assert snap == Snapshot('', (), False)


def test_teto_chars_e_tokens(index, store):
    _popular(store)
    snap = montar_snapshot(index, 'python', max_chars=200, max_tokens=50)
    assert len(snap.texto) <= 200
    assert estimar_tokens(snap.texto) <= 50


def test_ordem_relevancia(index, store):
    store.save('relevante', 'python python python', 'projeto', 'corpo relevante')
    store.save('outro', 'assunto diverso', 'projeto', 'corpo outro')
    snap = montar_snapshot(index, 'python')
    assert snap.origens
    assert snap.origens[0].startswith('relevante')


def test_origens_citam_nome_e_data(index, store):
    _popular(store)
    snap = montar_snapshot(index, 'python')
    for origem in snap.origens:
        assert '(' in origem and origem.endswith(')')


def test_truncado_true_quando_corta(index, store):
    store.save('grande', 'python', 'projeto', 'a' * 5000)
    snap = montar_snapshot(index, 'python', max_chars=300, max_tokens=100)
    assert snap.truncado is True
    assert len(snap.texto) <= 300
    assert estimar_tokens(snap.texto) <= 100


def test_truncado_false_quando_cabe(index, store):
    store.save('pequena', 'python', 'projeto', 'curto')
    snap = montar_snapshot(index, 'python')
    assert snap.truncado is False
    assert snap.origens


def test_acentos_e_emoji_nao_quebram(index, store):
    store.save('acentuada', 'python', 'projeto', 'corpo com acao e emoji ' + 'a' * 500)
    snap = montar_snapshot(index, 'python', max_chars=120, max_tokens=40)
    assert len(snap.texto) <= 120
    assert estimar_tokens(snap.texto) <= 40


def test_nota_removida_do_disco_ignorada(store):
    store.save('presente', 'python', 'projeto', 'corpo presente')
    store.save('ausente', 'python', 'projeto', 'corpo ausente')

    class StoreFalso:
        def __init__(self, real):
            self._real = real

        def get(self, nome):
            if nome == 'ausente':
                raise FileNotFoundError(nome)
            return self._real.get(nome)

    class IndexFalso:
        def __init__(self, real, store_falso):
            self._real = real
            self.store = store_falso

        def buscar(self, consulta, limite=5):
            return self._real.buscar(consulta, limite=limite)

    real = MemoryIndex(store)
    falso = IndexFalso(real, StoreFalso(store))
    snap = montar_snapshot(falso, 'python')
    assert all('ausente' not in o for o in snap.origens)


@pytest.mark.parametrize('max_chars', [1, 20, 50])
def test_teto_pequeno_nunca_estoura_nem_devolve_so_cabecalho(store, index, max_chars):
    _popular(store)
    snap = montar_snapshot(index, 'corpo', max_chars=max_chars)
    assert len(snap.texto) <= max_chars
    assert snap.origens == ()
    assert snap.truncado is True
    assert snap.texto == ''


def test_sem_nota_legivel_nao_devolve_so_cabecalho(store, index):
    _popular(store)
    index.sincronizar()
    index.store.get = lambda nome: (_ for _ in ()).throw(FileNotFoundError(nome))
    snap = montar_snapshot(index, 'corpo')
    assert snap.texto == ''
    assert snap.origens == ()
