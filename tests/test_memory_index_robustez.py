from __future__ import annotations

import pytest

from app.memory.index import IndiceIndisponivel, MemoryIndex, termos_da_consulta
from app.memory.store import MemoryStore


def _criar(tmp_path):
    pasta = tmp_path / 'memoria'
    pasta.mkdir()
    store = MemoryStore(pasta)
    return pasta, store, MemoryIndex(store, pasta / 'x.sqlite')


def test_termos_descarta_curtas_stopwords_e_repeticao() -> None:
    termos = termos_da_consulta('de a o que para revisar revisar modulo pagamento do projeto')
    assert termos == ['revisar', 'modulo', 'pagamento', 'projeto']


def test_termos_limita_a_oito_mais_longos_na_ordem_original() -> None:
    consulta = 'aaa bbbb cccc dddd eeee ffff gggg hhhh iiiiiiii jjjjjjjj'
    termos = termos_da_consulta(consulta)
    assert len(termos) == 8
    assert 'aaa' not in termos and 'hhhh' not in termos
    assert termos[-2:] == ['iiiiiiii', 'jjjjjjjj']


def test_termos_vazio() -> None:
    assert termos_da_consulta('') == []
    assert termos_da_consulta('de o a') == []


def test_busca_com_tabela_apagada_levanta_indice_indisponivel(tmp_path) -> None:
    _, store, indice = _criar(tmp_path)
    store.save('nota-um', 'descricao', 'usuario', 'corpo com pagamento')
    indice.sincronizar()
    indice._conn.execute('DROP TABLE busca')
    indice._conn.commit()
    with pytest.raises(IndiceIndisponivel):
        indice.buscar('pagamento')


def test_nota_que_ficou_ilegivel_some_do_indice(tmp_path) -> None:
    pasta, store, indice = _criar(tmp_path)
    store.save('nota-um', 'descricao', 'usuario', 'corpo com pagamento')
    indice.sincronizar()
    assert [r.nome for r in indice.buscar('pagamento')] == ['nota-um']
    (pasta / 'nota-um.md').write_bytes(b'\xff\xfe---')
    resumo = indice.sincronizar()
    assert resumo['removidas'] == 1
    assert indice.buscar('pagamento') == []


def test_instrucao_longa_acha_a_nota_certa(tmp_path) -> None:
    _, store, indice = _criar(tmp_path)
    store.save('pagamento', 'modulo de pagamento', 'projeto', 'codigo do modulo de pagamento')
    store.save('ruido', 'outra coisa', 'usuario', 'texto sobre jardinagem de rosas')
    indice.sincronizar()
    nomes = [r.nome for r in indice.buscar('preciso de ajuda para revisar o modulo de pagamento do projeto')]
    assert nomes[0] == 'pagamento'
