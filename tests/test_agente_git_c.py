import pytest

from app.agente.escrita import classificar_comando


@pytest.mark.parametrize('comando', [
    r'git -C "B:\Projetos\NetHub\NetHub BR" status --short',
    'git -C x log --oneline',
    'git -C x diff',
    'git -C x -C y status',
    'git status',
])
def test_git_com_C_so_de_leitura_nao_pergunta(comando: str) -> None:
    assert classificar_comando(comando)[0] == 'leitura'


@pytest.mark.parametrize('comando', [
    'git -c core.pager=calc status',     # -c minusculo injeta configuracao: nunca e leitura
    'git -C x commit -m a',
    'git -C x log --output=f',
    'git -C x branch -D a',
    'git -C x show HEAD:arquivo',
    'git -C x checkout .',
])
def test_git_com_C_que_grava_ou_executa_continua_pedindo(comando: str) -> None:
    assert classificar_comando(comando)[0] == 'normal'


@pytest.mark.parametrize('comando', [
    'git -C x push',
    'git -C x -c user.name=a push origin master',
    'git --no-pager push',
    'git -C x clean -fd',
    'git -C x reset --hard HEAD~1',
    'git --git-dir=.git push',
    'git push',
])
def test_git_push_clean_e_reset_hard_bloqueados_com_ou_sem_opcoes_globais(comando: str) -> None:
    assert classificar_comando(comando)[0] == 'bloqueado'


def test_mensagem_de_commit_com_a_palavra_push_nao_e_bloqueada() -> None:
    assert classificar_comando('git commit -m "ajusta o push do relatorio"')[0] == 'normal'
