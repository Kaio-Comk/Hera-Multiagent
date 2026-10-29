from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from app import skills as sk


def _hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _criar_skill(base: Path, nome: str, frontmatter: str = '', corpo: str = 'corpo') -> Path:
    pasta = base / nome
    pasta.mkdir(parents=True, exist_ok=True)
    texto = ''
    if frontmatter:
        texto = f'---\n{frontmatter}\n---\n'
    texto += corpo
    (pasta / 'SKILL.md').write_text(texto, encoding='utf-8')
    return pasta


def test_separar_frontmatter_yaml():
    fm, corpo = sk.separar_frontmatter('---\nname: foo\ndescription: bar\n---\nconteudo')
    assert fm['name'] == 'foo'
    assert fm['description'] == 'bar'
    assert corpo == 'conteudo'


def test_separar_frontmatter_ausente():
    fm, corpo = sk.separar_frontmatter('sem frontmatter')
    assert fm == {}
    assert corpo == 'sem frontmatter'


def test_separar_frontmatter_yaml_invalido():
    texto = '---\nname: [incompleto\ndescription: x\n---\ncorpo'
    fm, corpo = sk.separar_frontmatter(texto)
    assert isinstance(fm, dict)
    assert corpo == 'corpo'


def test_descobrir_prioridade_e_duplicados(tmp_path: Path):
    a = tmp_path / 'a'
    b = tmp_path / 'b'
    _criar_skill(a, 'foo', 'name: foo\ndescription: da A')
    _criar_skill(b, 'foo', 'name: foo\ndescription: da B')
    _criar_skill(b, 'bar', 'name: bar\ndescription: bar')
    resultado = sk.descobrir_skills([a, b])
    nomes = [s.nome for s in resultado]
    assert nomes == ['bar', 'foo']
    foo = next(s for s in resultado if s.nome == 'foo')
    assert foo.descricao == 'da A'


def test_descobrir_nome_invalido(tmp_path: Path):
    base = tmp_path / 's'
    _criar_skill(base, 'ok', 'name: ok')
    _criar_skill(base, 'ruim', 'name: -invalido')
    resultado = sk.descobrir_skills([base])
    assert [s.nome for s in resultado] == ['ok']


def test_descobrir_pasta_inexistente(tmp_path: Path):
    resultado = sk.descobrir_skills([tmp_path / 'nao_existe'])
    assert resultado == []


def test_descricao_multilinha(tmp_path: Path):
    base = tmp_path / 's'
    _criar_skill(base, 'm', 'name: m\ndescription: |\n  linha um\n  linha dois')
    resultado = sk.descobrir_skills([base])
    assert resultado[0].descricao == 'linha um linha dois'


def test_catalogo_corte_e_omitidas(tmp_path: Path):
    base = tmp_path / 's'
    for i in range(5):
        _criar_skill(base, f'sk{i}', f'name: sk{i}\ndescription: {"x" * 200}')
    skills = sk.descobrir_skills([base])
    texto = sk.catalogo_para_prompt(skills, max_chars=600)
    assert 'Skills disponiveis' in texto
    assert '...' in texto
    assert 'omitidas' in texto


def test_catalogo_vazio():
    assert sk.catalogo_para_prompt([]) == ''


def test_ler_skill_sem_frontmatter(tmp_path: Path):
    base = tmp_path / 's'
    pasta = _criar_skill(base, 'x', '', 'apenas corpo')
    skills = sk.descobrir_skills([base])
    assert sk.ler_skill(skills[0]) == 'apenas corpo'


def test_ler_skill_cortado(tmp_path: Path):
    base = tmp_path / 's'
    _criar_skill(base, 'x', 'name: x', 'a' * 100)
    skills = sk.descobrir_skills([base])
    texto = sk.ler_skill(skills[0], max_chars=10)
    assert 'cortado' in texto


def test_resolver_exato_prefixo_ambiguo(tmp_path: Path):
    base = tmp_path / 's'
    _criar_skill(base, 'alpha', 'name: alpha')
    _criar_skill(base, 'beta', 'name: beta')
    _criar_skill(base, 'alphabet', 'name: alphabet')
    skills = sk.descobrir_skills([base])
    assert sk.resolver_skill('ALPHA', skills).nome == 'alpha'
    assert sk.resolver_skill('bet', skills).nome == 'beta'
    assert sk.resolver_skill('alp', skills) is None


def test_detectar_incompatibilidade():
    texto = '#!/bin/bash\nchmod +x /tmp/x\nsudo apt-get install y\n'
    motivos = sk.detectar_incompatibilidade_windows(texto)
    assert 'usa bash' in motivos
    assert 'usa chmod' in motivos
    assert 'usa /tmp' in motivos
    assert 'usa sudo' in motivos
    assert len(motivos) == len(set(motivos))


def test_importar_copia_e_nao_altera_origem(tmp_path: Path):
    origem = tmp_path / 'origem'
    destino = tmp_path / 'destino'
    pasta = _criar_skill(origem, 'foo', 'name: foo\ndescription: d')
    (pasta / 'extra.txt').write_text('dados', encoding='utf-8')
    antes = {p: _hash(p) for p in pasta.rglob('*') if p.is_file()}
    rel = sk.importar_do_claude(origem, destino)
    assert 'foo' in rel.copiadas
    depois = {p: _hash(p) for p in pasta.rglob('*') if p.is_file()}
    assert antes == depois
    assert (destino / 'foo' / 'SKILL.md').is_file()


def test_importar_ignora_existente(tmp_path: Path):
    origem = tmp_path / 'origem'
    destino = tmp_path / 'destino'
    _criar_skill(origem, 'foo', 'name: foo')
    (destino / 'foo').mkdir(parents=True)
    (destino / 'foo' / 'SKILL.md').write_text('antigo', encoding='utf-8')
    rel = sk.importar_do_claude(origem, destino)
    assert 'foo' in rel.ignoradas
    assert (destino / 'foo' / 'SKILL.md').read_text(encoding='utf-8') == 'antigo'


def test_importar_sobrescreve(tmp_path: Path):
    origem = tmp_path / 'origem'
    destino = tmp_path / 'destino'
    _criar_skill(origem, 'foo', 'name: foo', 'novo')
    (destino / 'foo').mkdir(parents=True)
    (destino / 'foo' / 'SKILL.md').write_text('antigo', encoding='utf-8')
    rel = sk.importar_do_claude(origem, destino, sobrescrever=True)
    assert 'foo' in rel.sobrescritas
    assert 'novo' in (destino / 'foo' / 'SKILL.md').read_text(encoding='utf-8')


def test_importar_pula_env_e_grande(tmp_path: Path):
    origem = tmp_path / 'origem'
    destino = tmp_path / 'destino'
    pasta = _criar_skill(origem, 'foo', 'name: foo')
    (pasta / '.env').write_text('X=1', encoding='utf-8')
    grande = pasta / 'grande.bin'
    grande.write_bytes(b'0' * (1024 * 1024 + 10))
    sk.importar_do_claude(origem, destino)
    assert not (destino / 'foo' / '.env').exists()
    assert not (destino / 'foo' / 'grande.bin').exists()


def test_importar_pula_symlink(tmp_path: Path):
    origem = tmp_path / 'origem'
    destino = tmp_path / 'destino'
    pasta = _criar_skill(origem, 'foo', 'name: foo')
    alvo = tmp_path / 'externo.txt'
    alvo.write_text('externo', encoding='utf-8')
    link = pasta / 'link.txt'
    try:
        os.symlink(alvo, link)
    except (OSError, NotImplementedError):
        pytest.skip('symlink nao suportado')
    sk.importar_do_claude(origem, destino)
    assert not (destino / 'foo' / 'link.txt').exists()


def test_importar_segredo_nao_copiado(tmp_path: Path):
    origem = tmp_path / 'origem'
    destino = tmp_path / 'destino'
    pasta = _criar_skill(origem, 'foo', 'name: foo')
    segredo = 'sk-' + 'a1' * 12
    (pasta / 'config.txt').write_text(f'token={segredo}', encoding='utf-8')
    rel = sk.importar_do_claude(origem, destino)
    assert not (destino / 'foo' / 'config.txt').exists()
    assert any('segredo' in a for a in rel.avisos)


def test_importar_origem_inexistente(tmp_path: Path):
    rel = sk.importar_do_claude(tmp_path / 'nao', tmp_path / 'dest')
    assert rel.copiadas == ()
    assert rel.avisos


def test_importar_nao_copia_ambientes_virtuais_nem_caches(tmp_path: Path):
    origem = tmp_path / 'claude'
    skill = origem / 'pesada'
    (skill / '.venv' / 'Lib' / 'site-packages' / 'lib').mkdir(parents=True)
    (skill / '.venv' / 'Lib' / 'site-packages' / 'lib' / 'x.py').write_text('x = 1', encoding='utf-8')
    (skill / 'venv').mkdir()
    (skill / 'venv' / 'y.py').write_text('y = 1', encoding='utf-8')
    (skill / 'site-packages').mkdir()
    (skill / 'site-packages' / 'z.py').write_text('z = 1', encoding='utf-8')
    (skill / '.pytest_cache').mkdir()
    (skill / '.pytest_cache' / 'c').write_text('c', encoding='utf-8')
    (skill / 'SKILL.md').write_text('---\nname: pesada\ndescription: d\n---\ncorpo\n', encoding='utf-8')
    (skill / 'script.py').write_text('print(1)', encoding='utf-8')
    destino = tmp_path / 'hera'
    sk.importar_do_claude(origem, destino)
    copiado = destino / 'pesada'
    assert (copiado / 'SKILL.md').exists() and (copiado / 'script.py').exists()
    for pasta in ('.venv', 'venv', 'site-packages', '.pytest_cache'):
        assert not (copiado / pasta).exists(), pasta
