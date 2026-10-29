import subprocess
import sys
from pathlib import Path

from app.agente.caminhos import Raiz
from app.agente.checkpoints import Checkpoints
from app.agente.escrita import Escrita, classificar_comando


def test_aspas_externas_saem_dos_argumentos() -> None:
    classe, args = classificar_comando(r'git -C "B:\Projetos\Meu App" log --oneline')
    assert classe == 'leitura'
    assert args == ('git', '-C', r'B:\Projetos\Meu App', 'log', '--oneline')
    assert classificar_comando("python -c 'x'")[1][-1] == 'x'
    assert classificar_comando('echo "a b"')[1] == ('echo', 'a b')


def test_aspas_no_meio_do_argumento_ficam() -> None:
    assert classificar_comando('pytest -k "a or b"')[1] == ('pytest', '-k', 'a or b')
    assert classificar_comando('git commit -m "x y"')[1][-1] == 'x y'


def test_git_com_caminho_com_espaco_roda_de_verdade(tmp_path: Path) -> None:
    repo = tmp_path / 'meu projeto'
    repo.mkdir()
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    raiz = Raiz(tmp_path, acesso_total=True)
    e = Escrita(raiz, Checkpoints(tmp_path / 'ck', raiz), 'auto')
    r = e.propor_comando(f'git -C "{repo}" status --short')
    assert r.ok
    pid = r.texto.splitlines()[0]
    assert [p for p in e.pendentes() if p.id == pid][0] is not None
    assert e.precisa_aprovacao([p for p in e.pendentes() if p.id == pid][0]) is False  # leitura: nao pergunta
    resultado = e.aplicar(pid)
    assert resultado.ok and 'fatal' not in resultado.texto and 'cannot change' not in resultado.texto


def test_python_com_argumento_entre_aspas_chega_inteiro(tmp_path: Path) -> None:
    raiz = Raiz(tmp_path, acesso_total=True)
    e = Escrita(raiz, Checkpoints(tmp_path / 'ck', raiz), 'auto')
    r = e.propor_comando(f'"{sys.executable}" --version')
    assert r.ok
