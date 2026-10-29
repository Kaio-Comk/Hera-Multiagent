import argparse
from pathlib import Path

import pytest

from app.agente.caminhos import CaminhoNegado, Raiz
from app.agente.checkpoints import Checkpoints
from app.agente.escrita import Escrita
from app.agente.leitura import Leitura
from cli import chat_cmd
from cli.chat_comandos import Comandos, ConfigAgente, EstadoChat
from cli.chat_repl import Sessao


def _pastas(tmp_path: Path):
    trabalho = tmp_path / 'trabalho'
    outra = tmp_path / 'outra'
    trabalho.mkdir()
    outra.mkdir()
    (outra / 'player.js').write_text('function tocar() {}\n', encoding='utf-8')
    (outra / '.env').write_text('TOKEN=segredo\n', encoding='utf-8')
    return trabalho, outra


def test_pasta_extra_so_para_leitura(tmp_path: Path) -> None:
    trabalho, outra = _pastas(tmp_path)
    raiz = Raiz(trabalho)
    with pytest.raises(CaminhoNegado):
        raiz.resolver(outra / 'player.js', leitura=True)  # ainda nao liberada
    raiz.permitir_leitura(outra)
    assert raiz.resolver(outra / 'player.js', leitura=True) == (outra / 'player.js').resolve()
    with pytest.raises(CaminhoNegado):
        raiz.resolver(outra / 'player.js')  # escrita/resolucao normal continua confinada
    leitura = Leitura(raiz)
    assert leitura.ler_arquivo(str(outra / 'player.js')).ok
    assert not leitura.ler_arquivo(str(outra / '.env')).ok  # arquivo sensivel segue recusado
    assert leitura.listar_pasta(str(outra)).ok


def test_nao_libera_pasta_inexistente_nem_sensivel(tmp_path: Path) -> None:
    trabalho, _ = _pastas(tmp_path)
    raiz = Raiz(trabalho)
    with pytest.raises(CaminhoNegado):
        raiz.permitir_leitura(tmp_path / 'nao_existe')
    ssh = tmp_path / '.ssh'
    ssh.mkdir()
    with pytest.raises(CaminhoNegado):
        raiz.permitir_leitura(ssh)


def test_escrita_fora_da_raiz_continua_negada(tmp_path: Path) -> None:
    trabalho, outra = _pastas(tmp_path)
    raiz = Raiz(trabalho)
    raiz.permitir_leitura(outra)
    escrita = Escrita(raiz, Checkpoints(tmp_path / 'ck', raiz), 'workspace')
    with pytest.raises(CaminhoNegado):
        raiz.resolver(outra / 'novo.js')
    assert escrita is not None


async def test_comando_pasta_lista_e_libera(tmp_path: Path) -> None:
    trabalho, outra = _pastas(tmp_path)
    raiz = Raiz(trabalho)
    cfg = ConfigAgente(
        raiz=raiz, checkpoints=Checkpoints(tmp_path / 'ck', raiz), leitura=Leitura(raiz),
        escrita=Escrita(raiz, Checkpoints(tmp_path / 'ck2', raiz), 'guarded'), modo='guarded', skills=None,
    )

    class _C:
        modelo = 'm'

    cmd = Comandos(_C(), EstadoChat(), agente_cfg=cfg)
    saida: list[str] = []
    await cmd.executar(f'/pasta "{outra}"', Sessao(modelo='m'), saida.append)
    assert f'Liberada so para leitura: {outra.resolve()}' in ''.join(saida)
    saida.clear()
    await cmd.executar('/pasta', Sessao(modelo='m'), saida.append)
    texto = ''.join(saida)
    assert str(trabalho.resolve()) in texto and str(outra.resolve()) in texto
    saida.clear()
    await cmd.executar(f'/pasta {tmp_path / "nada"}', Sessao(modelo='m'), saida.append)
    assert 'Nao foi possivel liberar a pasta' in ''.join(saida)


def test_opcao_pasta_do_parser() -> None:
    parser = argparse.ArgumentParser()
    chat_cmd.registrar(parser.add_subparsers())
    assert parser.parse_args(['chat']).pasta == []
    assert parser.parse_args(['chat', '--pasta', 'B:/x', '--pasta', 'C:/y']).pasta == ['B:/x', 'C:/y']
