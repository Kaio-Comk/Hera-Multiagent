from __future__ import annotations

import logging
from pathlib import Path

import pytest

from app.memory import integracao
from app.memory.integracao import contexto_da_memoria
from app.memory.store import MemoryStore


def _preparar(pasta: Path, nome: str = 'nota', corpo: str = 'conteudo relevante') -> None:
    store = MemoryStore(pasta)
    store.save(nome, 'descricao', 'projeto', corpo)


def test_desligado_por_padrao_nao_cria_indice(tmp_path: Path) -> None:
    pasta = tmp_path / 'mem'
    pasta.mkdir()
    _preparar(pasta)
    out = contexto_da_memoria('pergunta', environ={}, pasta=pasta)
    assert out == ''
    assert not (pasta / '.indice.sqlite').exists()


@pytest.mark.parametrize('valor', ['1', 'TRUE', ' sim ', 'on'])
def test_ligado_com_valores_verdadeiros(tmp_path: Path, valor: str) -> None:
    pasta = tmp_path / 'mem'
    pasta.mkdir()
    _preparar(pasta, nome='projeto', corpo='usa pytest e python')
    out = contexto_da_memoria(
        'pytest', environ={'HERA_MEMORY_SNAPSHOT': valor}, pasta=pasta
    )
    assert out != ''
    assert 'projeto' in out
    assert 'usa pytest e python' in out


def test_pasta_inexistente_nao_cria(tmp_path: Path) -> None:
    pasta = tmp_path / 'nao_existe'
    out = contexto_da_memoria(
        'pergunta', environ={'HERA_MEMORY_SNAPSHOT': '1'}, pasta=pasta
    )
    assert out == ''
    assert not pasta.exists()


def test_segredo_editado_por_fora_e_sanitizado(tmp_path: Path) -> None:
    pasta = tmp_path / 'mem'
    pasta.mkdir()
    _preparar(pasta, nome='chave', corpo='placeholder')
    chave = 'sk-' + 'a1' * 12
    arquivo = pasta / 'chave.md'
    texto = arquivo.read_text(encoding='utf-8')
    arquivo.write_text(texto + '\n' + chave + '\n', encoding='utf-8')
    out = contexto_da_memoria(
        'chave', environ={'HERA_MEMORY_SNAPSHOT': '1'}, pasta=pasta
    )
    assert '[REDACTED_SECRET' in out
    assert chave not in out


def test_excecao_interna_devolve_vazio_e_loga(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    pasta = tmp_path / 'mem'
    pasta.mkdir()
    _preparar(pasta)

    def _boom(*args, **kwargs):
        raise RuntimeError('banco travado')

    monkeypatch.setattr(integracao, 'montar_snapshot', _boom)
    with caplog.at_level(logging.WARNING, logger=integracao.__name__):
        out = contexto_da_memoria(
            'pergunta', environ={'HERA_MEMORY_SNAPSHOT': '1'}, pasta=pasta
        )
    assert out == ''
    assert any('falha' in r.message for r in caplog.records)


def test_instrucao_vazia_devolve_vazio(tmp_path: Path) -> None:
    pasta = tmp_path / 'mem'
    pasta.mkdir()
    _preparar(pasta)
    assert (
        contexto_da_memoria(
            '   ', environ={'HERA_MEMORY_SNAPSHOT': '1'}, pasta=pasta
        )
        == ''
    )


def test_pasta_vem_de_hera_memory_dir(tmp_path: Path) -> None:
    pasta = tmp_path / 'mem'
    pasta.mkdir()
    _preparar(pasta, nome='viaenv', corpo='corpo via env')
    out = contexto_da_memoria(
        'viaenv',
        environ={'HERA_MEMORY_SNAPSHOT': '1', 'HERA_MEMORY_DIR': str(pasta)},
    )
    assert 'viaenv' in out
    assert 'corpo via env' in out


@pytest.mark.parametrize('valor', ['nao', '0'])
def test_valores_falsos_mantem_desligado(tmp_path: Path, valor: str) -> None:
    pasta = tmp_path / 'mem'
    pasta.mkdir()
    _preparar(pasta)
    out = contexto_da_memoria(
        'pergunta', environ={'HERA_MEMORY_SNAPSHOT': valor}, pasta=pasta
    )
    assert out == ''
    assert not (pasta / '.indice.sqlite').exists()


# ---- injecao nos passos do servidor/DAG (planos do Claude e painel) ------------------------------------------------

def _pasta_com_nota(tmp_path):
    pasta = tmp_path / 'mem'
    MemoryStore(pasta).save('preferencia-testes', 'como testar', 'usuario', 'sempre escrever o teste antes do codigo')
    return pasta


def test_injeta_memoria_nos_passos_so_quando_ligado(tmp_path):
    from app.memory.integracao import injetar_memoria_nos_passos
    pasta = _pasta_com_nota(tmp_path)
    passos = [{'id': 'a', 'instruction': 'preciso de testes para o modulo'}]
    assert injetar_memoria_nos_passos(passos, environ={}, pasta=pasta) == 0
    assert passos[0]['instruction'] == 'preciso de testes para o modulo'
    assert injetar_memoria_nos_passos(passos, environ={'HERA_MEMORY_SNAPSHOT': '1'}, pasta=pasta) == 1
    assert 'sempre escrever o teste antes do codigo' in passos[0]['instruction']
    assert passos[0]['instruction'].endswith('preciso de testes para o modulo')      # a tarefa original fica inteira, no fim


def test_injecao_respeita_o_limite_de_instrucao_e_preserva_a_tarefa(tmp_path):
    from app.memory.integracao import injetar_memoria_nos_passos
    pasta = _pasta_com_nota(tmp_path)
    tarefa = 'testes ' + 'x' * 3900
    passos = [{'id': 'a', 'instruction': tarefa}]
    injetar_memoria_nos_passos(passos, environ={'HERA_MEMORY_SNAPSHOT': '1'}, pasta=pasta, max_total=4000)
    assert len(passos[0]['instruction']) <= 4000 and passos[0]['instruction'].endswith(tarefa)


def test_sem_espaco_para_a_memoria_nao_mexe_no_passo(tmp_path):
    from app.memory.integracao import injetar_memoria_nos_passos
    pasta = _pasta_com_nota(tmp_path)
    tarefa = 'testes ' + 'x' * 3990
    passos = [{'id': 'a', 'instruction': tarefa}]
    assert injetar_memoria_nos_passos(passos, environ={'HERA_MEMORY_SNAPSHOT': '1'}, pasta=pasta, max_total=4000) == 0
    assert passos[0]['instruction'] == tarefa


def test_injecao_nunca_levanta_e_ignora_passos_estranhos(tmp_path):
    from app.memory.integracao import injetar_memoria_nos_passos
    passos = [{'id': 'a'}, 'texto', None, {'id': 'b', 'instruction': ''}]
    assert injetar_memoria_nos_passos(passos, environ={'HERA_MEMORY_SNAPSHOT': '1'}, pasta=tmp_path / 'nao-existe') == 0


def test_servidor_chama_a_injecao_depois_de_normalizar_os_passos():
    import pathlib
    src = (pathlib.Path(__file__).resolve().parent.parent / 'app' / 'server.py').read_text(encoding='utf-8')
    assert src.index('orchestration.normalize_steps(raw_steps') < src.index('injetar_memoria_nos_passos(steps)')
