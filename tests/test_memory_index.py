from __future__ import annotations

import threading
import time

import pytest

from app.memory.index import MemoryIndex
from app.memory.store import MemoryStore


def _store(tmp_path) -> MemoryStore:
    return MemoryStore(tmp_path)


def test_sincronizar_adiciona_e_idempotente(tmp_path):
    store = _store(tmp_path)
    store.save('nota-a', 'desc a', 'projeto', 'corpo com palavra')
    idx = MemoryIndex(store)
    r1 = idx.sincronizar()
    assert r1['adicionadas'] == 1
    assert r1['atualizadas'] == 0
    assert r1['removidas'] == 0
    r2 = idx.sincronizar()
    assert r2 == {'adicionadas': 0, 'atualizadas': 0, 'removidas': 0}
    idx.close()


def test_busca_palavra_do_corpo(tmp_path):
    store = _store(tmp_path)
    store.save('nota-a', 'desc', 'projeto', 'contem palavra especial')
    with MemoryIndex(store) as idx:
        res = idx.buscar('palavra')
        assert len(res) == 1
        assert res[0].nome == 'nota-a'


def test_descricao_tem_peso_maior(tmp_path):
    store = _store(tmp_path)
    store.save('nota-desc', 'palavra na descricao', 'projeto', 'corpo neutro')
    store.save('nota-corpo', 'desc neutra', 'projeto', 'palavra no corpo')
    with MemoryIndex(store) as idx:
        res = idx.buscar('palavra')
        assert len(res) == 2
        assert res[0].nome == 'nota-desc'


def test_busca_sem_acento(tmp_path):
    store = _store(tmp_path)
    store.save('nota-a', 'desc', 'projeto', 'Ação importante')
    with MemoryIndex(store) as idx:
        res = idx.buscar('acao')
        assert len(res) == 1
        assert res[0].nome == 'nota-a'


def test_maiusculas_nao_importam(tmp_path):
    store = _store(tmp_path)
    store.save('nota-a', 'desc', 'projeto', 'PALAVRA grande')
    with MemoryIndex(store) as idx:
        assert len(idx.buscar('palavra')) == 1
        assert len(idx.buscar('PALAVRA')) == 1


def test_overwrite_reflete_texto_novo(tmp_path):
    store = _store(tmp_path)
    store.save('nota-a', 'desc', 'projeto', 'texto antigo')
    with MemoryIndex(store) as idx:
        assert len(idx.buscar('antigo')) == 1
        time.sleep(0.01)
        store.save('nota-a', 'desc', 'projeto', 'texto novo', overwrite=True)
        assert len(idx.buscar('novo')) == 1
        assert len(idx.buscar('antigo')) == 0


def test_delete_remove_da_busca(tmp_path):
    store = _store(tmp_path)
    store.save('nota-a', 'desc', 'projeto', 'palavra unica')
    with MemoryIndex(store) as idx:
        assert len(idx.buscar('unica')) == 1
        store.delete('nota-a')
        assert len(idx.buscar('unica')) == 0


@pytest.mark.parametrize('consulta', ['"', 'a AND', '-x', '*', 'NOT', '()'])
def test_consultas_com_operadores_nao_quebram(tmp_path, consulta):
    store = _store(tmp_path)
    store.save('nota-a', 'desc', 'projeto', 'corpo qualquer')
    with MemoryIndex(store) as idx:
        idx.buscar(consulta)


def test_consulta_vazia_ou_simbolos(tmp_path):
    store = _store(tmp_path)
    store.save('nota-a', 'desc', 'projeto', 'corpo')
    with MemoryIndex(store) as idx:
        assert idx.buscar('') == []
        assert idx.buscar('   ') == []
        assert idx.buscar('!!!') == []


def test_limite_respeitado(tmp_path):
    store = _store(tmp_path)
    for i in range(10):
        store.save(f'nota-{i}', 'desc', 'projeto', f'palavra comum {i}')
    with MemoryIndex(store) as idx:
        assert len(idx.buscar('palavra', limite=3)) == 3


def test_200_notas_busca_rapida(tmp_path):
    store = _store(tmp_path)
    for i in range(200):
        store.save(f'nota-{i}', f'desc {i}', 'projeto', f'corpo palavra{i} comum')
    with MemoryIndex(store) as idx:
        idx.sincronizar()
        t0 = time.perf_counter()
        idx.buscar('palavra')
        dt = time.perf_counter() - t0
        assert dt < 0.2


def test_duas_threads_buscando(tmp_path):
    store = _store(tmp_path)
    for i in range(20):
        store.save(f'nota-{i}', 'desc', 'projeto', f'palavra comum {i}')
    with MemoryIndex(store) as idx:
        erros: list[Exception] = []

        def worker():
            try:
                for _ in range(10):
                    idx.buscar('palavra')
            except Exception as e:
                erros.append(e)

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert erros == []


def test_db_path_arquivo_persiste(tmp_path):
    store = _store(tmp_path)
    store.save('nota-a', 'desc', 'projeto', 'palavra persistente')
    db = tmp_path / 'idx.db'
    with MemoryIndex(store, db_path=db) as idx:
        assert idx.sincronizar()['adicionadas'] == 1
    with MemoryIndex(store, db_path=db) as idx:
        r = idx.sincronizar()
        assert r == {'adicionadas': 0, 'atualizadas': 0, 'removidas': 0}
        assert len(idx.buscar('persistente')) == 1


def test_busca_pelo_nome_da_nota(tmp_path):
    store = _store(tmp_path)
    store.save('preferencia-testes-primeiro', 'sem relacao', 'usuario', 'corpo neutro aqui')
    store.save('outra-nota', 'nada a ver', 'usuario', 'texto diferente')
    with MemoryIndex(store) as idx:
        achados = [r.nome for r in idx.buscar('testes primeiro')]
    assert achados == ['preferencia-testes-primeiro']


def test_indice_de_versao_antiga_e_reconstruido(tmp_path):
    import sqlite3
    store = _store(tmp_path)
    store.save('nota-a', 'desc a', 'projeto', 'palavra rara zeta')
    db = tmp_path / 'velho.sqlite'
    con = sqlite3.connect(db)
    con.execute("CREATE VIRTUAL TABLE busca USING fts5(nome UNINDEXED, tipo UNINDEXED, descricao, corpo)")
    con.execute("CREATE TABLE meta (nome TEXT PRIMARY KEY, mtime_ns INTEGER, tamanho INTEGER)")
    con.execute("INSERT INTO meta VALUES ('nota-a', 1, 1)")  # meta antigo diria "ja indexada"
    con.commit()
    con.close()
    with MemoryIndex(store, db) as idx:
        assert [r.nome for r in idx.buscar('zeta')] == ['nota-a']
