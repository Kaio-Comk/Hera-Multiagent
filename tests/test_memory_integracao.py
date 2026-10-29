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
