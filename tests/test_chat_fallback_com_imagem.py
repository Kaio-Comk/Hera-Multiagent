from pathlib import Path

import pytest

from app.maestro import Maestro
from cli.chat_client import ErroDoServidor
from cli.chat_imagens import extrair_imagens
from cli.chat_repl import rodar

PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 64
DEEPSEEK = 'deepseek-ai/DeepSeek-V4.1-Flash'


class _Cliente:
    """Falha com 503 nos modelos em `falham`; guarda com qual modelo cada chamada saiu."""

    def __init__(self, falham: set[str], modelo: str = 'GLM-5.3'):
        self.modelo = modelo
        self.falham = falham
        self.usados: list[str] = []

    async def modelos(self):
        return []

    async def conversar(self, mensagens):
        self.usados.append(self.modelo)
        if self.modelo in self.falham:
            raise ErroDoServidor(503, 'sobrecarga')
        if self.modelo not in (DEEPSEEK, 'gemini-flash-lite-latest') and isinstance(mensagens[-1]['content'], list):
            raise ErroDoServidor(400, 'Provedor byteplus retornou HTTP 400')   # modelo sem visao recebendo imagem
        yield 'vi a imagem'


def _entradas(*linhas):
    it = iter(linhas)

    async def ler():
        return next(it)

    return ler


def _maestro(disponiveis: list[str]) -> Maestro:
    async def d():
        return disponiveis

    prefs = {'leve': ['GLM-5.3', 'kimi-k2-thinking-251104'], 'medio': ['GLM-5.3', 'kimi-k2-thinking-251104'],
             'pesado': ['GLM-5.3', 'kimi-k2-thinking-251104']}
    return Maestro(disponiveis_de=d, preferencias=prefs)


async def _rodar(tmp_path: Path, cliente: _Cliente, maestro: Maestro) -> str:
    (tmp_path / 'a.png').write_bytes(PNG)
    saida: list[str] = []
    try:
        await rodar(cliente, _entradas('o que e isso? @a.png', '/exit'), saida.append, maestro=maestro,
                    imagens=lambda t: extrair_imagens(t, tmp_path))
    except RuntimeError:
        pass
    return ''.join(saida)


async def test_falha_do_modelo_com_visao_cai_para_outro_que_enxerga_nao_para_o_da_faixa(tmp_path: Path) -> None:
    cliente = _Cliente(falham={DEEPSEEK})
    maestro = _maestro(['GLM-5.3', 'kimi-k2-thinking-251104', DEEPSEEK, 'gemini-flash-lite-latest'])
    saida = await _rodar(tmp_path, cliente, maestro)
    assert cliente.usados == [DEEPSEEK, 'gemini-flash-lite-latest']      # nunca o kimi (sem visao)
    assert f'{DEEPSEEK} indisponivel, tentando gemini-flash-lite-latest' in saida
    assert 'Erro (400)' not in saida and 'vi a imagem' in saida


async def test_sem_nenhum_modelo_com_visao_avisa_em_vez_de_mandar_a_imagem_para_quem_nao_ve(tmp_path: Path) -> None:
    cliente = _Cliente(falham={DEEPSEEK})
    maestro = _maestro(['GLM-5.3', 'kimi-k2-thinking-251104', DEEPSEEK])
    saida = await _rodar(tmp_path, cliente, maestro)
    assert cliente.usados == [DEEPSEEK]
    assert 'nenhum modelo que enxerga imagens' in saida and 'Erro (400)' not in saida


async def test_sem_imagem_o_fallback_continua_igual(tmp_path: Path) -> None:
    cliente = _Cliente(falham={'GLM-5.3'})
    maestro = _maestro(['GLM-5.3', 'kimi-k2-thinking-251104'])
    saida: list[str] = []
    await rodar(cliente, _entradas('oi', '/exit'), saida.append, maestro=maestro)
    assert cliente.usados == ['GLM-5.3', 'kimi-k2-thinking-251104']
    assert 'GLM-5.3 indisponivel, tentando kimi-k2-thinking-251104' in ''.join(saida)
