from __future__ import annotations

import os
from pathlib import Path

import pytest

from app.agente.caminhos import CaminhoNegado, Raiz, limpar_saida
from app.agente.leitura import Leitura


def _raiz(tmp_path: Path) -> Raiz:
    return Raiz(tmp_path)


def test_resolver_relativo_e_absoluto_dentro(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    (tmp_path / 'a.txt').write_text('oi', encoding='utf-8')
    assert raiz.resolver('a.txt') == (tmp_path / 'a.txt').resolve()
    assert raiz.resolver(tmp_path / 'a.txt') == (tmp_path / 'a.txt').resolve()


def test_resolver_fora_negado(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    with pytest.raises(CaminhoNegado):
        raiz.resolver('..')


def test_resolver_bytes_nulos(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    with pytest.raises(CaminhoNegado):
        raiz.resolver('a\x00b')


def test_resolver_symlink_fora_negado(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    fora = tmp_path.parent / 'fora_alvo.txt'
    fora.write_text('x', encoding='utf-8')
    link = tmp_path / 'link.txt'
    try:
        os.symlink(fora, link)
    except (OSError, NotImplementedError):
        pytest.skip('SO nao permite criar symlink')
    with pytest.raises(CaminhoNegado):
        raiz.resolver('link.txt')


def test_eh_sensivel_nomes_e_pastas(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    assert raiz.eh_sensivel(tmp_path / '.env')
    assert raiz.eh_sensivel(tmp_path / 'chave.pem')
    assert raiz.eh_sensivel(tmp_path / 'id_rsa')
    assert raiz.eh_sensivel(tmp_path / '.git' / 'config')
    assert raiz.eh_sensivel(tmp_path / '.ssh' / 'known_hosts')
    assert not raiz.eh_sensivel(tmp_path / 'normal.txt')


def test_exigir_leitura_nega_env(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    (tmp_path / '.env').write_text('X=1', encoding='utf-8')
    with pytest.raises(CaminhoNegado):
        raiz.exigir_leitura('.env')


def test_limpar_saida_redige_e_corta() -> None:
    chave = 'sk-' + 'a1' * 12
    saida = limpar_saida(f'token={chave}')
    assert chave not in saida
    grande = 'x' * 300
    cortado = limpar_saida(grande, max_chars=100)
    assert '[cortado:' in cortado


def test_ler_arquivo_numerado_e_intervalo(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    leitura = Leitura(raiz)
    (tmp_path / 'a.txt').write_text('um\ndois\ntres\n', encoding='utf-8')
    r = leitura.ler_arquivo('a.txt')
    assert r.ok
    assert '    1\tum' in r.texto
    r2 = leitura.ler_arquivo('a.txt', inicio=2, fim=2)
    assert r2.ok
    assert 'dois' in r2.texto
    assert 'um' not in r2.texto


def test_ler_arquivo_trunca_400_linhas(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    leitura = Leitura(raiz)
    conteudo = '\n'.join(f'l{i}' for i in range(1, 501))
    (tmp_path / 'grande.txt').write_text(conteudo, encoding='utf-8')
    r = leitura.ler_arquivo('grande.txt')
    assert r.ok
    assert 'mostrando linhas' in r.texto


def test_ler_arquivo_binario(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    leitura = Leitura(raiz)
    (tmp_path / 'b.bin').write_bytes(b'abc\x00def')
    r = leitura.ler_arquivo('b.bin')
    assert not r.ok


def test_ler_arquivo_grande_demais(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    leitura = Leitura(raiz)
    (tmp_path / 'g.txt').write_bytes(b'a' * (2 * 1024 * 1024 + 10))
    r = leitura.ler_arquivo('g.txt')
    assert not r.ok
    assert 'buscar' in r.texto


def test_ler_arquivo_inexistente(tmp_path: Path) -> None:
    leitura = Leitura(_raiz(tmp_path))
    r = leitura.ler_arquivo('nao_existe.txt')
    assert not r.ok


def test_ler_arquivo_sensivel(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    leitura = Leitura(raiz)
    (tmp_path / '.env').write_text('X=1', encoding='utf-8')
    r = leitura.ler_arquivo('.env')
    assert not r.ok


def test_listar_pasta_ordem_barras_ocultas(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    leitura = Leitura(raiz)
    (tmp_path / 'b.txt').write_text('b', encoding='utf-8')
    (tmp_path / 'a.txt').write_text('a', encoding='utf-8')
    (tmp_path / 'sub').mkdir()
    (tmp_path / '.git').mkdir()
    (tmp_path / 'node_modules').mkdir()
    r = leitura.listar_pasta('.')
    assert r.ok
    linhas = r.texto.splitlines()
    assert linhas[0] == 'a.txt'
    assert 'sub/' in linhas
    assert '.git' not in r.texto
    assert 'node_modules' not in r.texto


def test_listar_pasta_corte_200(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    leitura = Leitura(raiz)
    for i in range(250):
        (tmp_path / f'f{i:03d}.txt').write_text('x', encoding='utf-8')
    r = leitura.listar_pasta('.')
    assert r.ok
    assert 'mostrando' in r.texto


def test_buscar_achado(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    leitura = Leitura(raiz)
    (tmp_path / 'a.txt').write_text('linha alvo aqui\n', encoding='utf-8')
    r = leitura.buscar('alvo')
    assert r.ok
    assert 'a.txt:1:' in r.texto


def test_buscar_regex_invalida(tmp_path: Path) -> None:
    leitura = Leitura(_raiz(tmp_path))
    r = leitura.buscar('(')
    assert not r.ok


def test_buscar_glob(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    leitura = Leitura(raiz)
    (tmp_path / 'a.txt').write_text('alvo\n', encoding='utf-8')
    (tmp_path / 'b.md').write_text('alvo\n', encoding='utf-8')
    r = leitura.buscar('alvo', glob='*.md')
    assert r.ok
    assert 'b.md' in r.texto
    assert 'a.txt' not in r.texto


def test_buscar_pula_binario_sensivel_e_venv(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    leitura = Leitura(raiz)
    (tmp_path / 'b.bin').write_bytes(b'alvo\x00')
    (tmp_path / '.env').write_text('alvo', encoding='utf-8')
    (tmp_path / '.venv').mkdir()
    (tmp_path / '.venv' / 'c.txt').write_text('alvo', encoding='utf-8')
    r = leitura.buscar('alvo')
    assert r.ok
    assert r.texto == 'nenhuma ocorrencia'


def test_buscar_limite_100(tmp_path: Path) -> None:
    raiz = _raiz(tmp_path)
    leitura = Leitura(raiz)
    conteudo = '\n'.join('alvo' for _ in range(150))
    (tmp_path / 'a.txt').write_text(conteudo, encoding='utf-8')
    r = leitura.buscar('alvo')
    assert r.ok
    assert len(r.texto.splitlines()) <= 100


def test_buscar_sem_resultado(tmp_path: Path) -> None:
    leitura = Leitura(_raiz(tmp_path))
    r = leitura.buscar('inexistente_xyz')
    assert r.ok
    assert r.texto == 'nenhuma ocorrencia'


@pytest.mark.parametrize('nome', ['servidor.pem ', 'servidor.pem.', 'chave.key  ', '.env. ', 'x.pem:fluxo', 'x.key::$DATA', 'id_rsa .'])
def test_nomes_do_windows_que_escapam_do_filtro_sao_sensiveis(tmp_path, nome):
    """Windows ignora ponto/espaco no fim do nome e aceita fluxos alternativos (:fluxo), entao 'x.pem ' abre 'x.pem'."""
    from app.agente.caminhos import Raiz
    assert Raiz(tmp_path).eh_sensivel(tmp_path / nome), nome
