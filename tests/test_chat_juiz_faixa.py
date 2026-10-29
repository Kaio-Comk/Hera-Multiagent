import cli.chat_repl as repl
from app.maestro import PROMPT_FAIXA, Maestro


class _Irmao:
    criados: list = []

    def __init__(self, base_url, token, modelo, **kw):
        self.modelo = modelo
        self.token = token
        _Irmao.criados.append(self)

    async def conversar(self, mensagens):
        self.mensagens = mensagens
        yield 'pes'
        yield 'ado'


class _Cliente:
    base_url = 'http://x'
    token = 't'
    modelo = 'conversa'


async def _maestro(disponiveis):
    async def d():
        return disponiveis

    return Maestro(disponiveis_de=d)


async def test_juiz_de_faixa_usa_o_modelo_rapido_sem_mexer_no_da_conversa(monkeypatch):
    monkeypatch.setattr(repl, 'ClienteHera', _Irmao)
    _Irmao.criados.clear()
    cliente = _Cliente()
    juiz = repl.criar_juiz_de_faixa(cliente, await _maestro(['gemini-flash-lite-latest', 'deepseek-ai/DeepSeek-V4.1-Flash']))
    assert await juiz('crie um sistema de filas') == 'pesado'
    assert _Irmao.criados[0].modelo == 'deepseek-ai/DeepSeek-V4.1-Flash'
    assert _Irmao.criados[0].mensagens[-1]['content'] == PROMPT_FAIXA + 'crie um sistema de filas'
    assert cliente.modelo == 'conversa'


async def test_juiz_de_faixa_cai_para_o_proximo_modelo_e_sem_nenhum_devolve_none(monkeypatch):
    monkeypatch.setattr(repl, 'ClienteHera', _Irmao)
    _Irmao.criados.clear()
    m = await _maestro(['gemini-flash-lite-latest'])
    juiz = repl.criar_juiz_de_faixa(_Cliente(), m)
    await juiz('x')
    assert _Irmao.criados[-1].modelo == 'gemini-flash-lite-latest'
    m2 = await _maestro(['outro-modelo'])
    assert await repl.criar_juiz_de_faixa(_Cliente(), m2)('x') is None
