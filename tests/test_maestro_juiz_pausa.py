import asyncio

from app.maestro import Maestro


def _maestro(juiz, relogio):
    async def disponiveis():
        return ['deepseek-ai/DeepSeek-V4.1-Flash', 'GLM-5.3']

    m = Maestro(disponiveis_de=disponiveis, relogio=relogio)
    m.juiz = juiz
    m.timeout_juiz = 0.05
    return m


async def test_juiz_que_estoura_o_tempo_descansa_e_depois_volta():
    agora = [1000.0]
    chamadas: list[str] = []

    async def lento(texto):
        chamadas.append(texto)
        await asyncio.sleep(1)
        return 'pesado'

    m = _maestro(lento, lambda: agora[0])
    pedido = 'crie um sistema de filas'
    assert (await m.escolher(pedido)).peso == 'medio'
    assert len(chamadas) == 1
    await m.escolher(pedido)
    assert len(chamadas) == 1  # em pausa: nao gasta mais 8 s por pedido
    agora[0] += 301
    await m.escolher(pedido)
    assert len(chamadas) == 2  # pausa acabou


async def test_juiz_com_resposta_lixo_nao_entra_em_pausa():
    chamadas: list[str] = []

    async def lixo(texto):
        chamadas.append(texto)
        return 'nao sei'

    m = _maestro(lixo, lambda: 1000.0)
    await m.escolher('crie um sistema de filas')
    await m.escolher('crie um sistema de filas')
    assert len(chamadas) == 2
