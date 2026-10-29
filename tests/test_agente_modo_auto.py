import argparse
from pathlib import Path

import pytest

from app.agente.caminhos import Raiz
from app.agente.checkpoints import Checkpoints
from app.agente.escrita import Escrita
from cli import chat_cmd
from cli.chat_comandos import Comandos, ConfigAgente, EstadoChat
from cli.chat_repl import CICLO_DE_MODOS, Sessao, proximo_modo
from cli.chat_ui import Visual


def _escrita(tmp_path: Path, modo: str = 'auto', acesso_total: bool = True) -> Escrita:
    trabalho = tmp_path / 'trabalho'
    trabalho.mkdir(exist_ok=True)
    raiz = Raiz(trabalho, acesso_total=acesso_total)
    return Escrita(raiz, Checkpoints(tmp_path / 'dados', raiz), modo)


def _pendencia(e: Escrita, resultado):
    pid = resultado.texto.splitlines()[0]
    return [x for x in e.pendentes() if x.id == pid][0]


def test_auto_grava_sem_perguntar_dentro_da_pasta_e_pergunta_fora(tmp_path: Path) -> None:
    e = _escrita(tmp_path)
    (tmp_path / 'trabalho' / 'a.txt').write_text('x\n', encoding='utf-8')
    (tmp_path / 'fora.txt').write_text('x\n', encoding='utf-8')
    dentro = _pendencia(e, e.propor_edicao(str(tmp_path / 'trabalho' / 'a.txt'), 'x', 'y'))
    fora = _pendencia(e, e.propor_edicao(str(tmp_path / 'fora.txt'), 'x', 'y'))
    assert e.precisa_aprovacao(dentro) is False
    assert e.precisa_aprovacao(fora) is True


@pytest.mark.parametrize('comando', ['pytest -q', 'python -m pytest tests', 'npm test', 'npm run lint', 'node --check a.js'])
def test_auto_roda_verificacoes_sem_perguntar(tmp_path: Path, comando: str) -> None:
    e = _escrita(tmp_path)
    p = _pendencia(e, e.propor_comando(comando))
    assert e.precisa_aprovacao(p) is False


@pytest.mark.parametrize('comando', ['npm run build', 'pip install requests', 'git commit -m x', 'node app.js', 'python script.py'])
def test_auto_continua_perguntando_o_resto(tmp_path: Path, comando: str) -> None:
    e = _escrita(tmp_path)
    p = _pendencia(e, e.propor_comando(comando))
    assert e.precisa_aprovacao(p) is True


@pytest.mark.parametrize('comando', ['git push', 'rm -rf x', 'curl http://x', 'powershell -c x', 'pytest && del a'])
def test_auto_nunca_libera_o_que_e_bloqueado(tmp_path: Path, comando: str) -> None:
    e = _escrita(tmp_path)
    assert not e.propor_comando(comando).ok


def test_guarded_nao_muda_e_pytest_pergunta(tmp_path: Path) -> None:
    e = _escrita(tmp_path, modo='guarded')
    assert e.precisa_aprovacao(_pendencia(e, e.propor_comando('pytest -q'))) is True


def test_somente_leitura_nao_propoe_nada(tmp_path: Path) -> None:
    e = _escrita(tmp_path, modo='read-only')
    (tmp_path / 'trabalho' / 'a.txt').write_text('x\n', encoding='utf-8')
    assert not e.propor_edicao(str(tmp_path / 'trabalho' / 'a.txt'), 'x', 'y').ok


def test_ciclo_do_shift_tab() -> None:
    assert CICLO_DE_MODOS == ('guarded', 'auto', 'read-only')
    assert proximo_modo('guarded') == 'auto'
    assert proximo_modo('auto') == 'read-only'
    assert proximo_modo('read-only') == 'guarded'
    assert proximo_modo('workspace') == 'guarded'  # modo fora do ciclo volta ao primeiro


def test_rodape_destaca_o_auto_mode_como_no_claude_code() -> None:
    v = Visual(cor=True, largura=100)
    auto = v.rodape(modelo='m', modo='auto', maestro=True, pasta='/p', todo_pc=True)
    assert 'auto mode ligado' in auto and '\x1b[33m' in auto and 'todo o PC' in auto
    manual = v.rodape(modelo='m', modo='guarded', maestro=True, pasta='/p')
    assert 'aprovação manual' in manual and '\x1b[33m' not in manual
    assert 'somente leitura' in v.rodape(modelo='m', modo='read-only', maestro=True, pasta='/p')
    sem = Visual(cor=False).rodape(modelo='m', modo='auto', maestro=True, pasta='/p')
    assert '>> auto mode ligado' in sem and '\x1b' not in sem


async def test_comando_modo_aceita_auto_e_a_opcao_do_chat(tmp_path: Path) -> None:
    raiz = Raiz(tmp_path, acesso_total=True)
    ck = Checkpoints(tmp_path / 'ck', raiz)
    cfg = ConfigAgente(raiz=raiz, checkpoints=ck, leitura=None, escrita=Escrita(raiz, ck, 'guarded'), modo='guarded', skills=None)

    class _C:
        modelo = 'm'

    saida: list[str] = []
    await Comandos(_C(), EstadoChat(), agente_cfg=cfg).executar('/modo auto', Sessao(modelo='m'), saida.append)
    assert cfg.modo == 'auto' and cfg.escrita.modo == 'auto' and 'Modo alterado para auto.' in ''.join(saida)
    parser = argparse.ArgumentParser()
    chat_cmd.registrar(parser.add_subparsers())
    assert parser.parse_args(['chat', '--modo', 'auto']).modo == 'auto'
