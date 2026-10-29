from pathlib import Path

from app.compressao import estimar_tokens
from app.imagens import TOKENS_POR_IMAGEM
from app.maestro import Maestro
from app.memory.resumo import _montar_conversa
from cli.chat_imagens import extrair_imagens
from cli.chat_repl import rodar

PNG = b'\x89PNG\r\n\x1a\n' + b'0' * 64


class _Cliente:
    def __init__(self, modelo='deepseek-ai/DeepSeek-V4.1-Flash'):
        self.modelo = modelo
        self.recebidas: list = []
        self.modelos_usados: list = []

    async def modelos(self):
        return ['GLM-5.3', 'deepseek-ai/DeepSeek-V4.1-Flash']

    async def conversar(self, mensagens):
        self.recebidas.append([dict(m) for m in mensagens])
        self.modelos_usados.append(self.modelo)
        yield 'vi a imagem'


def _entradas(*linhas):
    it = iter(linhas)

    async def ler():
        return next(it)

    return ler


def _imagens_fn(pasta: Path):
    return lambda t: extrair_imagens(t, pasta)


async def test_pergunta_com_imagem_chega_ao_modelo_como_conteudo_multiparte(tmp_path):
    (tmp_path / 'erro.png').write_bytes(PNG)
    cliente = _Cliente()
    saida: list[str] = []
    sessao = await rodar(
        cliente, _entradas('o que e isso? @erro.png', '/exit'), saida.append, imagens=_imagens_fn(tmp_path)
    )
    conteudo = cliente.recebidas[0][-1]['content']
    assert isinstance(conteudo, list)
    assert conteudo[0] == {'type': 'text', 'text': 'o que e isso? [imagem: erro.png]'}
    assert conteudo[1]['image_url']['url'].startswith('data:image/png;base64,')
    assert '[imagem: erro.png (1 KB)]' in ''.join(saida)
    assert 'Aviso' not in ''.join(saida)
    assert sessao.mensagens[0]['content'] == conteudo


async def test_sem_imagem_a_mensagem_continua_sendo_texto(tmp_path):
    cliente = _Cliente()
    await rodar(cliente, _entradas('oi', '/exit'), lambda _t: None, imagens=_imagens_fn(tmp_path))
    assert cliente.recebidas[0][-1]['content'] == 'oi'


async def test_arquivo_inexistente_avisa_e_segue_so_com_texto(tmp_path):
    cliente = _Cliente()
    saida: list[str] = []
    await rodar(cliente, _entradas('veja @nada.png', '/exit'), saida.append, imagens=_imagens_fn(tmp_path))
    assert '[imagens: imagem nao encontrada: nada.png]' in ''.join(saida)
    assert isinstance(cliente.recebidas[0][-1]['content'], str)


async def test_maestro_troca_para_modelo_que_enxerga(tmp_path):
    (tmp_path / 'a.png').write_bytes(PNG)

    async def disponiveis():
        return ['GLM-5.3', 'deepseek-ai/DeepSeek-V4.1-Flash']

    maestro = Maestro(
        disponiveis_de=disponiveis,
        preferencias={'leve': ['GLM-5.3'], 'medio': ['GLM-5.3'], 'pesado': ['GLM-5.3']},
    )
    cliente = _Cliente('GLM-5.3')
    saida: list[str] = []
    await rodar(
        cliente, _entradas('descreva @a.png', '/exit'), saida.append, maestro=maestro, imagens=_imagens_fn(tmp_path)
    )
    assert cliente.modelos_usados == ['deepseek-ai/DeepSeek-V4.1-Flash']
    assert '[maestro: imagem -> deepseek-ai/DeepSeek-V4.1-Flash]' in ''.join(saida)


async def test_modelo_forcado_sem_visao_so_avisa(tmp_path):
    (tmp_path / 'a.png').write_bytes(PNG)

    async def disponiveis():
        return ['GLM-5.3', 'deepseek-ai/DeepSeek-V4.1-Flash']

    maestro = Maestro(disponiveis_de=disponiveis, forcado='GLM-5.3')
    cliente = _Cliente('GLM-5.3')
    saida: list[str] = []
    await rodar(
        cliente, _entradas('descreva @a.png', '/exit'), saida.append, maestro=maestro, imagens=_imagens_fn(tmp_path)
    )
    assert cliente.modelos_usados == ['GLM-5.3']
    assert 'Aviso: o modelo GLM-5.3 pode nao enxergar imagens.' in ''.join(saida)


def test_base64_nao_infla_tokens_nem_o_resumo():
    gigante = 'A' * 2_000_000
    msg = {
        'role': 'user',
        'content': [
            {'type': 'text', 'text': 'veja'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + gigante}},
        ],
    }
    assert estimar_tokens([msg]) < TOKENS_POR_IMAGEM + 50
    resumo = _montar_conversa([msg], 12000)
    assert 'AAAA' not in resumo and '[imagem]' in resumo
