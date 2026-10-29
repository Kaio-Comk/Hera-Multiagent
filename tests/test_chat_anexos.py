from __future__ import annotations

from pathlib import Path

import pytest

from app.agente.caminhos import Raiz
from cli.chat_anexos import Anexo, expandir_anexos, resumo_anexos


@pytest.fixture()
def raiz(tmp_path: Path) -> Raiz:
    return Raiz(str(tmp_path))


def _criar(raiz: Raiz, nome: str, conteudo: str) -> Path:
    p = Path(raiz.pasta) / nome
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(conteudo, encoding='utf-8')
    return p


def test_arquivo_inteiro(raiz: Raiz) -> None:
    _criar(raiz, 'a.py', 'linha1\nlinha2\n')
    texto, avisos, anexos = expandir_anexos('veja @a.py', raiz)
    assert avisos == []
    assert len(anexos) == 1
    assert anexos[0].linhas == 'todas'
    assert 'linha1' in texto
    assert texto.startswith('veja @a.py')


def test_intervalo_ab(raiz: Raiz) -> None:
    _criar(raiz, 'a.py', 'l1\nl2\nl3\nl4\n')
    _, _, anexos = expandir_anexos('@a.py:2-3', raiz)
    assert anexos[0].linhas == '2-3'


def test_linha_unica(raiz: Raiz) -> None:
    _criar(raiz, 'a.py', 'l1\nl2\nl3\n')
    _, _, anexos = expandir_anexos('@a.py:2', raiz)
    assert anexos[0].linhas == '2-2'


def test_pasta(raiz: Raiz) -> None:
    _criar(raiz, 'sub/x.py', 'x')
    _, _, anexos = expandir_anexos('@sub', raiz)
    assert len(anexos) == 1
    assert anexos[0].linhas == 'todas'


def test_dois_tokens(raiz: Raiz) -> None:
    _criar(raiz, 'a.py', 'a')
    _criar(raiz, 'b.py', 'b')
    _, _, anexos = expandir_anexos('@a.py e @b.py', raiz)
    assert len(anexos) == 2


def test_token_repetido(raiz: Raiz) -> None:
    _criar(raiz, 'a.py', 'a')
    _, _, anexos = expandir_anexos('@a.py @a.py', raiz)
    assert len(anexos) == 1


def test_email_ignorado(raiz: Raiz) -> None:
    texto, avisos, anexos = expandir_anexos('contato a@b.com', raiz)
    assert anexos == []
    assert avisos == []
    assert texto == 'contato a@b.com'


def test_usuario_sem_caminho_ignorado(raiz: Raiz) -> None:
    texto, avisos, anexos = expandir_anexos('oi @usuario', raiz)
    assert anexos == []
    assert avisos == []
    assert texto == 'oi @usuario'


def test_naoexiste_com_aviso(raiz: Raiz) -> None:
    _, avisos, anexos = expandir_anexos('@naoexiste.py', raiz)
    assert anexos == []
    assert any('nao encontrado' in a for a in avisos)


def test_fora_da_raiz(raiz: Raiz) -> None:
    _, avisos, anexos = expandir_anexos('@../fora.py', raiz)
    assert anexos == []
    assert any('fora da pasta de trabalho' in a for a in avisos)


def test_sensivel_recusado(raiz: Raiz) -> None:
    _criar(raiz, '.env', 'SEGREDO=1')
    _, avisos, anexos = expandir_anexos('@.env', raiz)
    assert anexos == []
    assert any('sensivel' in a for a in avisos)


def test_limite_anexos(raiz: Raiz) -> None:
    for i in range(7):
        _criar(raiz, f'f{i}.py', 'x')
    _, avisos, anexos = expandir_anexos(
        ' '.join(f'@f{i}.py' for i in range(7)), raiz, max_anexos=5
    )
    assert len(anexos) == 5
    assert any('limite de anexos' in a for a in avisos)


def test_corte_max_chars(raiz: Raiz) -> None:
    _criar(raiz, 'grande.py', 'x' * 5000)
    texto, _, _ = expandir_anexos('@grande.py', raiz, max_chars=100)
    assert '[cortado]' in texto


def test_aspas_com_espaco(raiz: Raiz) -> None:
    _criar(raiz, 'com espaco.py', 'ok')
    _, _, anexos = expandir_anexos('@"com espaco.py"', raiz)
    assert len(anexos) == 1


def test_texto_original_intacto(raiz: Raiz) -> None:
    _criar(raiz, 'a.py', 'a')
    original = 'pergunta @a.py fim'
    texto, _, _ = expandir_anexos(original, raiz)
    assert texto.startswith(original)


def test_sem_anexos_identico(raiz: Raiz) -> None:
    original = 'nada aqui'
    texto, avisos, anexos = expandir_anexos(original, raiz)
    assert texto == original
    assert avisos == []
    assert anexos == []


def test_secret_redigido(raiz: Raiz) -> None:
    chave = 'sk-' + 'a1' * 12
    _criar(raiz, 'cfg.py', f'KEY = "{chave}"')
    texto, _, _ = expandir_anexos('@cfg.py', raiz)
    assert '[REDACTED_SECRET' in texto


def test_resumo_anexos(raiz: Raiz) -> None:
    _criar(raiz, 'a.py', 'a')
    _criar(raiz, 'b.py', 'b')
    _, _, anexos = expandir_anexos('@a.py @b.py:10-30', raiz)
    r = resumo_anexos(anexos)
    assert r.startswith('[anexado:')
    assert 'a.py (todas)' in r
    assert 'b.py (10-30)' in r


def test_resumo_vazio() -> None:
    assert resumo_anexos([]) == ''
