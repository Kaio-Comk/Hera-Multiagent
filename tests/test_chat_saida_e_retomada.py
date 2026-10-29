from cli.chat_comandos import Comandos, EstadoChat
from cli.chat_repl import Sessao, rodar
from cli.chat_ui import Visual


class _Cliente:
    modelo = 'm'

    async def modelos(self):
        return ['m']

    async def conversar(self, mensagens):
        yield 'ok'


def _entradas(*linhas):
    it = iter(linhas)

    async def ler():
        return next(it)

    return ler


def _montar(extrator):
    cliente = _Cliente()
    comandos = Comandos(cliente, EstadoChat())
    comandos.extrator = extrator
    return cliente, comandos


async def _extrator_com_uma_nota(_prompt):
    return (
        '{"notas": [{"nome": "preferencia-testes", "tipo": "feedback", '
        '"descricao": "d", "corpo": "sempre testar antes"}]}'
    )


async def test_sair_com_poucas_perguntas_nao_pergunta_nada():
    cliente, comandos = _montar(_extrator_com_uma_nota)
    saida: list[str] = []
    await rodar(cliente, _entradas('oi', '/exit'), saida.append, comandos=comandos)
    assert 'memoria' not in ''.join(saida)


async def test_sair_com_tres_perguntas_oferece_e_respeita_o_nao():
    cliente, comandos = _montar(_extrator_com_uma_nota)
    saida: list[str] = []
    await rodar(cliente, _entradas('a', 'b', 'c', '/exit', 'n'), saida.append, comandos=comandos)
    texto = ''.join(saida)
    assert 'Salvar o que importa desta conversa na memoria? [s/N]' in texto
    assert 'Previa' not in texto


async def test_sair_aceitando_mostra_a_previa_e_so_grava_com_o_segundo_sim(tmp_path):
    cliente, comandos = _montar(_extrator_com_uma_nota)
    comandos.pasta_memoria = tmp_path
    saida: list[str] = []
    await rodar(cliente, _entradas('a', 'b', 'c', '/exit', 's', 's'), saida.append, comandos=comandos)
    texto = ''.join(saida)
    assert 'Previa do que seria salvo' in texto and 'preferencia-testes' in texto
    assert 'Salvas: preferencia-testes' in texto
    assert (tmp_path / 'preferencia-testes.md').exists()


async def test_sair_aceitando_e_recusando_a_gravacao_nao_cria_nota(tmp_path):
    cliente, comandos = _montar(_extrator_com_uma_nota)
    comandos.pasta_memoria = tmp_path
    saida: list[str] = []
    await rodar(cliente, _entradas('a', 'b', 'c', '/exit', 's', 'n'), saida.append, comandos=comandos)
    assert 'Propostas descartadas' in ''.join(saida)
    assert not list(tmp_path.glob('*.md'))


async def test_falha_ao_resumir_nao_trava_a_saida():
    async def quebrado(_prompt):
        raise RuntimeError('boom')

    cliente, comandos = _montar(quebrado)
    saida: list[str] = []
    await rodar(cliente, _entradas('a', 'b', 'c', '/exit', 's'), saida.append, comandos=comandos)
    assert 'Nada duradouro para lembrar nesta conversa.' in ''.join(saida)  # o resumo trata a falha; sair nao trava


def test_banner_mostra_a_conversa_retomada():
    texto = Visual(cor=False, largura=80).banner(
        versao='1', modelo='m', pasta='/p', modo='', maestro=False,
        retomando='Retomando: Antiga (2 mensagens)',
    )
    assert '▏ Retomando: Antiga (2 mensagens)' in texto
