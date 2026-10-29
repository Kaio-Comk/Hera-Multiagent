from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.agente.caminhos import Raiz
from app.agente.checkpoints import Checkpoints
from app.agente.escrita import Escrita, classificar_comando, redigir


def _raiz(tmp_path: Path) -> Raiz:
    return Raiz(tmp_path)


def _ckpt(tmp_path: Path) -> Checkpoints:
    return Checkpoints(tmp_path / 'dados', _raiz(tmp_path))


def _escrita(tmp_path: Path, modo: str = 'guarded', executar=None) -> Escrita:
    return Escrita(_raiz(tmp_path), _ckpt(tmp_path), modo=modo, executar=executar)


def test_edicao_unica(tmp_path: Path) -> None:
    f = tmp_path / 'a.txt'
    f.write_text('linha um\nlinha dois\n', encoding='utf-8')
    e = _escrita(tmp_path)
    r = e.propor_edicao('a.txt', 'dois', 'DOIS')
    assert r.ok
    assert 'a/a.txt' in r.texto and 'b/a.txt' in r.texto


def test_edicao_duplicada(tmp_path: Path) -> None:
    f = tmp_path / 'a.txt'
    f.write_text('x\nx\n', encoding='utf-8')
    e = _escrita(tmp_path)
    r = e.propor_edicao('a.txt', 'x', 'y')
    assert not r.ok
    assert '2' in r.texto


def test_edicao_inexistente(tmp_path: Path) -> None:
    e = _escrita(tmp_path)
    r = e.propor_edicao('nao.txt', 'a', 'b')
    assert not r.ok


def test_edicao_todas(tmp_path: Path) -> None:
    f = tmp_path / 'a.txt'
    f.write_text('x\nx\n', encoding='utf-8')
    e = _escrita(tmp_path)
    r = e.propor_edicao('a.txt', 'x', 'y', todas=True)
    assert r.ok
    pid = r.texto.splitlines()[0]
    assert e.aplicar(pid).ok
    assert f.read_text(encoding='utf-8') == 'y\ny\n'


def test_escrita_nova(tmp_path: Path) -> None:
    e = _escrita(tmp_path)
    r = e.propor_escrita('novo.txt', 'conteudo\n')
    assert r.ok
    pid = r.texto.splitlines()[0]
    assert e.aplicar(pid).ok
    assert (tmp_path / 'novo.txt').read_text(encoding='utf-8') == 'conteudo\n'


def test_escrita_sobrescreve(tmp_path: Path) -> None:
    f = tmp_path / 'a.txt'
    f.write_text('velho\n', encoding='utf-8')
    e = _escrita(tmp_path)
    r = e.propor_escrita('a.txt', 'novo\n')
    assert r.ok
    pid = r.texto.splitlines()[0]
    assert e.aplicar(pid).ok
    assert f.read_text(encoding='utf-8') == 'novo\n'


def test_env_negado(tmp_path: Path) -> None:
    (tmp_path / '.env').write_text('X=1\n', encoding='utf-8')
    e = _escrita(tmp_path)
    assert not e.propor_escrita('.env', 'Y=2\n').ok
    assert not e.propor_edicao('.env', 'X', 'Y').ok


def test_read_only(tmp_path: Path) -> None:
    (tmp_path / 'a.txt').write_text('x\n', encoding='utf-8')
    e = _escrita(tmp_path, modo='read-only')
    assert not e.propor_edicao('a.txt', 'x', 'y').ok
    assert not e.propor_escrita('b.txt', 'y').ok
    assert not e.propor_comando('git status').ok


def test_hash_mudou(tmp_path: Path) -> None:
    f = tmp_path / 'a.txt'
    f.write_text('x\n', encoding='utf-8')
    e = _escrita(tmp_path)
    r = e.propor_edicao('a.txt', 'x', 'y')
    pid = r.texto.splitlines()[0]
    f.write_text('outro\n', encoding='utf-8')
    res = e.aplicar(pid)
    assert not res.ok
    assert 'mudou' in res.texto


def test_aplicar_cria_checkpoint(tmp_path: Path) -> None:
    f = tmp_path / 'a.txt'
    f.write_text('x\n', encoding='utf-8')
    ck = _ckpt(tmp_path)
    e = Escrita(_raiz(tmp_path), ck)
    r = e.propor_edicao('a.txt', 'x', 'y')
    pid = r.texto.splitlines()[0]
    assert e.aplicar(pid).ok
    assert f.read_text(encoding='utf-8') == 'y\n'


def test_classificar_bloqueado() -> None:
    assert classificar_comando('rm -rf /')[0] == 'bloqueado'
    assert classificar_comando('git push origin main')[0] == 'bloqueado'
    assert classificar_comando('curl http://x')[0] == 'bloqueado'


def test_classificar_operadores() -> None:
    assert classificar_comando('echo a && echo b')[0] == 'bloqueado'
    assert classificar_comando('echo a | findstr a')[0] == 'bloqueado'
    assert classificar_comando('echo a > b')[0] == 'bloqueado'


def test_classificar_leitura() -> None:
    assert classificar_comando('git status')[0] == 'leitura'
    assert classificar_comando('pytest -q')[0] == 'normal'      # executa codigo do repositorio: pede aprovacao (1x por sessao, via lembrar)
    assert classificar_comando('python --version')[0] == 'leitura'


def test_classificar_normal() -> None:
    assert classificar_comando('python script.py')[0] == 'normal'


def test_precisa_aprovacao_modos(tmp_path: Path) -> None:
    (tmp_path / 'a.txt').write_text('x\n', encoding='utf-8')
    for modo, esperado in (('guarded', True), ('workspace', False)):
        e = _escrita(tmp_path, modo=modo)
        r = e.propor_edicao('a.txt', 'x', 'y')
        pid = r.texto.splitlines()[0]
        p = [x for x in e.pendentes() if x.id == pid][0]
        assert e.precisa_aprovacao(p) is esperado


def test_precisa_aprovacao_comando(tmp_path: Path) -> None:
    e = _escrita(tmp_path)
    r = e.propor_comando('git status')
    pid = r.texto.splitlines()[0]
    p = [x for x in e.pendentes() if x.id == pid][0]
    assert e.precisa_aprovacao(p) is False
    r2 = e.propor_comando('python script.py')
    pid2 = r2.texto.splitlines()[0]
    p2 = [x for x in e.pendentes() if x.id == pid2][0]
    assert e.precisa_aprovacao(p2) is True
    e.lembrar(p2)
    assert e.precisa_aprovacao(p2) is False


def test_bloqueado_nao_vira_pendencia(tmp_path: Path) -> None:
    e = _escrita(tmp_path)
    r = e.propor_comando('rm -rf /')
    assert not r.ok
    assert e.pendentes() == []


def test_saida_com_chave_redigida(tmp_path: Path) -> None:
    chave = 'sk-' + 'a1' * 12
    assert chave not in redigir(f'token={chave}')


def test_env_filtrado(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('MINHA_API_KEY', 'segredo')
    monkeypatch.setenv('NORMAL', 'ok')
    from app.agente.escrita import _env_limpo
    env = _env_limpo()
    assert 'MINHA_API_KEY' not in env
    assert env.get('NORMAL') == 'ok'


def test_pendencia_consumida(tmp_path: Path) -> None:
    f = tmp_path / 'a.txt'
    f.write_text('x\n', encoding='utf-8')
    e = _escrita(tmp_path)
    r = e.propor_edicao('a.txt', 'x', 'y')
    pid = r.texto.splitlines()[0]
    assert e.aplicar(pid).ok
    assert e.pendentes() == []
    assert not e.aplicar(pid).ok


def test_rejeitar(tmp_path: Path) -> None:
    f = tmp_path / 'a.txt'
    f.write_text('x\n', encoding='utf-8')
    e = _escrita(tmp_path)
    r = e.propor_edicao('a.txt', 'x', 'y')
    pid = r.texto.splitlines()[0]
    assert e.rejeitar(pid).ok
    assert e.pendentes() == []
    assert f.read_text(encoding='utf-8') == 'x\n'


@pytest.mark.parametrize('cmd', [
    'git branch -D main', 'git branch -d x', 'git branch novo', 'git branch -m a b',
    'git diff --output=x.txt', 'git log --output=x.txt', 'git diff --ext-diff', 'git log --textconv',
    'git show HEAD:config.yaml', 'git show origin/master:data/.hera_token',
    'pytest', 'python -m pytest -x', 'py -m pytest',
])
def test_comandos_que_gravam_executam_ou_leem_segredos_exigem_aprovacao(cmd):
    """Estes NAO podem ser 'leitura' (executar sem perguntar): apagam branch, gravam arquivo, rodam programa ou codigo do repo."""
    from app.agente.escrita import classificar_comando
    assert classificar_comando(cmd)[0] == 'normal', cmd


@pytest.mark.parametrize('cmd', ['git status', 'git status -s', 'git diff', 'git diff --stat', 'git log --oneline -5', 'git show HEAD --stat',
                                  'git branch', 'git branch -a', 'git branch --show-current', 'python --version', 'pip list', 'where python'])
def test_comandos_realmente_de_leitura_continuam_livres(cmd):
    from app.agente.escrita import classificar_comando
    assert classificar_comando(cmd)[0] == 'leitura', cmd


def test_aplicar_edicao_pode_ser_desfeita_pelo_rollback(tmp_path: Path) -> None:
    """Regressao: aplicar() confirmava o checkpoint com o id da pendencia (nao o do checkpoint) e engolia o KeyError."""
    f = tmp_path / 'a.txt'
    f.write_text('antes\n', encoding='utf-8')
    ck = _ckpt(tmp_path)
    e = Escrita(_raiz(tmp_path), ck)
    pid = e.propor_edicao('a.txt', 'antes', 'depois').texto.splitlines()[0]
    assert e.aplicar(pid).ok
    assert f.read_text(encoding='utf-8') == 'depois\n'
    [entrada] = ck.listar()
    assert entrada.hash_depois, 'o estado final precisa ser registrado'
    rel = ck.reverter()
    assert rel.restaurados == (entrada.id,), rel
    assert f.read_text(encoding='utf-8') == 'antes\n'


def test_escrita_de_arquivo_novo_pode_ser_desfeita(tmp_path: Path) -> None:
    ck = _ckpt(tmp_path)
    e = Escrita(_raiz(tmp_path), ck)
    pid = e.propor_escrita('novo.txt', 'oi\n').texto.splitlines()[0]
    assert e.aplicar(pid).ok and (tmp_path / 'novo.txt').exists()
    assert ck.reverter().restaurados
    assert not (tmp_path / 'novo.txt').exists()


def test_falha_ao_confirmar_checkpoint_nao_passa_em_silencio(tmp_path: Path) -> None:
    class CkQuebrado:
        def salvar(self, alvo):
            return 'abc'

        def confirmar(self, cid, alvo):
            raise RuntimeError('disco cheio')

    f = tmp_path / 'a.txt'
    f.write_text('x\n', encoding='utf-8')
    e = Escrita(_raiz(tmp_path), CkQuebrado())
    pid = e.propor_edicao('a.txt', 'x', 'y').texto.splitlines()[0]
    r = e.aplicar(pid)
    assert r.ok and 'aviso' in r.texto and 'desfazer' in r.texto
