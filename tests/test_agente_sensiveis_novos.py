from pathlib import Path

from app.agente.caminhos import Raiz


def test_novos_arquivos_sensiveis(tmp_path: Path) -> None:
    raiz = Raiz(tmp_path)
    for nome in ['.credentials.json', '.claude.json', 'ia_suporte.session', 'senhas.kdbx', 'id_ed25519', '.netrc', 'Cookies', 'Login Data']:
        assert raiz.eh_sensivel(tmp_path / nome), nome
    for nome in ['notas.md', 'credito.py', 'sessao.py', 'app.py']:
        assert not raiz.eh_sensivel(tmp_path / nome), nome


def test_appdata_so_e_sensivel_dentro_da_pasta_de_trabalho(tmp_path: Path) -> None:
    raiz = Raiz(tmp_path)
    assert raiz.eh_sensivel(tmp_path / 'AppData' / 'Roaming' / 'x.txt')
    assert raiz.eh_sensivel(tmp_path / '.gnupg' / 'chave')
    # trabalhando DENTRO de um AppData (como o tmp_path do pytest), nada fica bloqueado so por isso
    dentro = tmp_path / 'AppData' / 'Local' / 'projeto'
    dentro.mkdir(parents=True)
    assert not Raiz(dentro).eh_sensivel(dentro / 'main.py')
