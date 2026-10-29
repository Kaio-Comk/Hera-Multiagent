import argparse
from pathlib import Path

import pytest

from app.agente.caminhos import CaminhoNegado, Raiz
from app.agente.checkpoints import Checkpoints
from app.agente.escrita import Escrita
from app.agente.leitura import Leitura
from cli import chat_cmd


def _cenario(tmp_path: Path):
    trabalho = tmp_path / 'trabalho'
    fora = tmp_path / 'fora'
    trabalho.mkdir()
    fora.mkdir()
    (fora / 'player.js').write_text('function tocar() { return 1 }\n', encoding='utf-8')
    (fora / '.env').write_text('TOKEN=segredo\n', encoding='utf-8')
    return trabalho, fora


def test_sem_acesso_total_continua_confinado(tmp_path: Path) -> None:
    trabalho, fora = _cenario(tmp_path)
    with pytest.raises(CaminhoNegado):
        Raiz(trabalho).resolver(fora / 'player.js')


def test_acesso_total_le_qualquer_pasta_mas_nao_segredos(tmp_path: Path) -> None:
    trabalho, fora = _cenario(tmp_path)
    leitura = Leitura(Raiz(trabalho, acesso_total=True))
    assert 'tocar' in leitura.ler_arquivo(str(fora / 'player.js')).texto
    assert leitura.listar_pasta(str(fora)).ok
    assert not leitura.ler_arquivo(str(fora / '.env')).ok  # segredo segue recusado
    assert leitura.buscar('tocar', str(fora)).ok


def test_appdata_fora_da_pasta_segue_protegido_no_acesso_total(tmp_path: Path) -> None:
    trabalho, _ = _cenario(tmp_path)
    raiz = Raiz(trabalho, acesso_total=True)
    assert raiz.eh_sensivel(Path('C:/pessoa/AppData/Roaming/Mozilla/x.txt'))
    assert raiz.eh_sensivel(Path('C:/pessoa/AppData/Local/Google/Chrome/Default/Preferences'))
    assert not raiz.eh_sensivel(Path('C:/pessoa/AppData/Local/Temp/saida.txt'))  # Temp nao guarda credenciais


def _escrita(tmp_path: Path, trabalho: Path, modo: str) -> Escrita:
    raiz = Raiz(trabalho, acesso_total=True)
    return Escrita(raiz, Checkpoints(tmp_path / 'dados', raiz), modo)


def test_edita_fora_da_pasta_com_checkpoint_e_rollback(tmp_path: Path) -> None:
    trabalho, fora = _cenario(tmp_path)
    alvo = fora / 'player.js'
    e = _escrita(tmp_path, trabalho, 'guarded')
    r = e.propor_edicao(str(alvo), 'return 1', 'return 2')
    assert r.ok, r.texto
    pid = r.texto.splitlines()[0]
    p = [x for x in e.pendentes() if x.id == pid][0]
    assert e.precisa_aprovacao(p) is True
    assert e.aplicar(pid).ok
    assert 'return 2' in alvo.read_text(encoding='utf-8')
    rel = e.checkpoints.reverter()
    assert not rel.erros if hasattr(rel, 'erros') else True
    assert 'return 1' in alvo.read_text(encoding='utf-8')


def test_workspace_so_grava_sem_perguntar_dentro_da_pasta(tmp_path: Path) -> None:
    trabalho, fora = _cenario(tmp_path)
    (trabalho / 'a.txt').write_text('x\n', encoding='utf-8')
    e = _escrita(tmp_path, trabalho, 'workspace')
    dentro = e.propor_edicao(str(trabalho / 'a.txt'), 'x', 'y').texto.splitlines()[0]
    longe = e.propor_edicao(str(fora / 'player.js'), 'return 1', 'return 3').texto.splitlines()[0]
    pend = {x.id: x for x in e.pendentes()}
    assert e.precisa_aprovacao(pend[dentro]) is False
    assert e.precisa_aprovacao(pend[longe]) is True


def test_segredo_fora_da_pasta_nao_pode_ser_editado(tmp_path: Path) -> None:
    trabalho, fora = _cenario(tmp_path)
    e = _escrita(tmp_path, trabalho, 'guarded')
    assert not e.propor_edicao(str(fora / '.env'), 'segredo', 'x').ok


def test_chat_abre_com_acesso_total_por_padrao_e_so_esta_pasta_confina() -> None:
    parser = argparse.ArgumentParser()
    chat_cmd.registrar(parser.add_subparsers())
    assert parser.parse_args(['chat']).so_esta_pasta is False
    assert parser.parse_args(['chat', '--so-esta-pasta']).so_esta_pasta is True


async def test_executar_monta_raiz_com_acesso_total(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    recebido: dict = {}

    async def principal(*a, **kw):
        recebido.update(kw)

    for so_pasta in (False, True):
        args = argparse.Namespace(
            url='http://127.0.0.1:8000', modelo='default', resume=None, listar=False,
            agente=True, modo='guarded', pasta=[], so_esta_pasta=so_pasta,
        )
        await chat_cmd.executar(
            args, raiz=Path('.'), garantir=lambda u, r: True, principal=principal, token_de=lambda r: 't'
        )
        assert recebido['agente_cfg'].raiz.acesso_total is (not so_pasta)
