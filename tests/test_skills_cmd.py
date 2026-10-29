from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from app.skills import Skill, descobrir_skills
from cli import skills_cmd


def _criar_skill(pasta: Path, nome: str, descricao: str = 'desc', corpo: str = 'corpo') -> Path:
    '''Cria uma skill em disco e retorna o caminho do arquivo.'''
    (pasta / nome).mkdir(parents=True, exist_ok=True)
    arquivo = pasta / nome / 'SKILL.md'
    arquivo.write_text(f'---\nname: {nome}\ndescription: {descricao}\n---\n{corpo}\n', encoding='utf-8')
    return arquivo


def _args(**kwargs) -> argparse.Namespace:
    '''Cria um Namespace simples para testes.'''
    return argparse.Namespace(**kwargs)


def test_pastas_de_skills_ordem(tmp_path: Path) -> None:
    cwd = tmp_path / 'proj'
    home = tmp_path / 'home'
    pastas = skills_cmd.pastas_de_skills(cwd, home)
    assert pastas == [cwd / '.hera' / 'skills', home / '.hera' / 'skills']


def test_list_vazio(tmp_path: Path) -> None:
    linhas: list[str] = []
    codigo = skills_cmd.executar(_args(acao='list'), saida=linhas.append, cwd=tmp_path, home=tmp_path)
    assert codigo == 0
    assert linhas == ['Nenhuma skill. Importe as do Claude com: hera skills import']


def test_list_cheio_com_aviso(tmp_path: Path) -> None:
    pasta = tmp_path / '.hera' / 'skills'
    _criar_skill(pasta, 'alpha', 'Primeira skill')
    linhas: list[str] = []
    codigo = skills_cmd.executar(_args(acao='list'), saida=linhas.append, cwd=tmp_path, home=tmp_path)
    assert codigo == 0
    assert any(linha.startswith('alpha | Primeira skill |') for linha in linhas)


def test_import_copia_e_preserva_origem(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    origem = home / '.claude' / 'skills'
    _criar_skill(origem, 'beta', 'Skill beta')
    antes = sorted(p.name for p in origem.iterdir())

    linhas: list[str] = []
    codigo = skills_cmd.executar(_args(acao='import', origem=None, destino=None, sobrescrever=False), saida=linhas.append, cwd=tmp_path, home=home)

    assert codigo == 0
    assert 'Copiadas: 1' in linhas
    assert 'Ignoradas (ja existiam): 0' in linhas
    assert 'Sobrescritas: 0' in linhas
    assert any(linha.startswith('As originais em ') and linha.endswith('nao foram alteradas.') for linha in linhas)
    assert sorted(p.name for p in origem.iterdir()) == antes


def test_import_ignora_existente_e_sobrescreve(tmp_path: Path) -> None:
    home = tmp_path / 'home'
    origem = home / '.claude' / 'skills'
    destino = home / '.hera' / 'skills'
    _criar_skill(origem, 'gama', 'Nova')
    _criar_skill(destino, 'gama', 'Antiga')

    linhas: list[str] = []
    codigo = skills_cmd.executar(_args(acao='import', origem=None, destino=None, sobrescrever=False), saida=linhas.append, cwd=tmp_path, home=home)
    assert codigo == 0
    assert 'Ignoradas (ja existiam): 1' in linhas

    linhas2: list[str] = []
    codigo2 = skills_cmd.executar(_args(acao='import', origem=None, destino=None, sobrescrever=True), saida=linhas2.append, cwd=tmp_path, home=home)
    assert codigo2 == 0
    assert 'Sobrescritas: 1' in linhas2


def test_show_existente(tmp_path: Path) -> None:
    pasta = tmp_path / '.hera' / 'skills'
    _criar_skill(pasta, 'delta', 'Skill delta', corpo='conteudo delta')
    linhas: list[str] = []
    codigo = skills_cmd.executar(_args(acao='show', nome='delta'), saida=linhas.append, cwd=tmp_path, home=tmp_path)
    assert codigo == 0
    assert any(linha == 'Nome: delta' for linha in linhas)
    assert any('conteudo delta' in linha for linha in linhas)


def test_show_inexistente_com_sugestoes(tmp_path: Path) -> None:
    pasta = tmp_path / '.hera' / 'skills'
    _criar_skill(pasta, 'epsilon', 'Skill epsilon')
    linhas: list[str] = []
    codigo = skills_cmd.executar(_args(acao='show', nome='epsilom'), saida=linhas.append, cwd=tmp_path, home=tmp_path)
    assert codigo == 1
    assert any(linha.startswith('Skill nao encontrada:') for linha in linhas)
    assert any('epsilon' in linha for linha in linhas)


def test_texto_para_comando_skill_com_e_sem_argumentos(tmp_path: Path) -> None:
    pasta = tmp_path / '.hera' / 'skills'
    _criar_skill(pasta, 'zeta', 'Skill zeta', corpo='instrucoes zeta')
    skills = descobrir_skills([pasta])

    sem = skills_cmd.texto_para_comando_skill('zeta', skills)
    assert sem is not None
    assert 'Use a skill "zeta"' in sem
    assert 'instrucoes zeta' in sem
    assert 'Pedido do usuario: siga a skill' in sem

    com = skills_cmd.texto_para_comando_skill('zeta', skills, 'faca algo')
    assert com is not None
    assert 'Pedido do usuario: faca algo' in com


def test_texto_para_comando_skill_inexistente(tmp_path: Path) -> None:
    pasta = tmp_path / '.hera' / 'skills'
    _criar_skill(pasta, 'eta', 'Skill eta')
    skills = descobrir_skills([pasta])
    assert skills_cmd.texto_para_comando_skill('naoexiste', skills) is None


def test_registrar_cria_subcomandos() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest='comando')
    skills_cmd.registrar(subparsers)

    args_list = parser.parse_args(['skills', 'list'])
    assert args_list.acao == 'list'

    args_import = parser.parse_args(['skills', 'import', '--sobrescrever'])
    assert args_import.acao == 'import'
    assert args_import.sobrescrever is True

    args_show = parser.parse_args(['skills', 'show', 'nome'])
    assert args_show.acao == 'show'
    assert args_show.nome == 'nome'


def test_import_resume_avisos_quando_sao_muitos(tmp_path: Path) -> None:
    from app.skills import RelatorioImportacao
    relatorio = RelatorioImportacao(copiadas=('a',), ignoradas=(), sobrescritas=(), avisos=tuple(f'aviso {i}' for i in range(50)))
    linhas: list[str] = []
    import cli.skills_cmd as modulo
    original = modulo.importar_do_claude
    modulo.importar_do_claude = lambda *a, **k: relatorio
    try:
        codigo = skills_cmd.executar(_args(acao='import', origem=None, destino=None, sobrescrever=False),
                                     saida=linhas.append, cwd=tmp_path, home=tmp_path)
    finally:
        modulo.importar_do_claude = original
    assert codigo == 0
    avisos_impressos = [l for l in linhas if l.startswith('aviso ')]
    assert len(avisos_impressos) <= 10
    assert any('mais 40' in l for l in linhas)


def test_list_mostra_se_a_skill_e_do_projeto_ou_do_usuario_sem_caminhos_duplicados(tmp_path: Path) -> None:
    cwd = tmp_path / 'proj'
    home = tmp_path / 'home'
    _criar_skill(cwd / '.hera' / 'skills', 'local', 'So deste projeto')
    _criar_skill(home / '.hera' / 'skills', 'global', 'Do usuario')
    linhas: list[str] = []
    skills_cmd.executar(_args(acao='list'), saida=linhas.append, cwd=cwd, home=home)
    por_nome = {l.split(' | ')[0]: l for l in linhas if ' | ' in l}
    assert por_nome['local'].endswith(' | projeto'), por_nome['local']
    assert por_nome['global'].endswith(' | usuario'), por_nome['global']
    assert str(tmp_path) not in '\n'.join(linhas)
