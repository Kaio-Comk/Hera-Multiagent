import re

from cli.chat_ui import Visual

_ANSI = re.compile(r'\x1b\[[0-9;]*[A-Za-z]|\r')


def _visivel(t: str) -> str:
    return _ANSI.sub('', t)


def test_chamadas_de_ferramenta_ficam_uma_por_linha():
    v = Visual(cor=False, largura=80)
    v.novo_turno()
    saida = v.estilizar('Vou procurar o projeto.')
    saida += v.estilizar('-> listar_pasta C:\\x')
    saida += v.estilizar('-> ler_arquivo C:\\x\\a.py')
    assert saida == '● Vou procurar o projeto.\n  ↳ listar_pasta C:\\x\n  ↳ ler_arquivo C:\\x\\a.py\n'


def test_texto_depois_da_ferramenta_continua_sem_novo_marcador():
    v = Visual(cor=False, largura=80)
    v.novo_turno()
    v.estilizar('Antes.\n')
    v.estilizar('-> buscar player')
    assert v.estilizar('Achei.\n') == '  Achei.\n'


def test_indicador_volta_depois_da_ferramenta_e_some_com_o_texto():
    v = Visual(cor=True, largura=80)
    v.novo_turno()
    v.marcar_pensando()
    saida = v.estilizar('-> ler_arquivo a.py')
    assert 'Pensando' in saida and v.pensando_visivel is True
    texto = v.estilizar('Pronto.\n')
    assert texto.startswith('\r\x1b[2K') and 'Pensando' not in texto and v.pensando_visivel is False


def test_diff_nao_vira_lista_de_markdown():
    v = Visual(cor=True, largura=80)
    v.novo_turno()
    diff = '--- a/x.js\n+++ b/x.js\n@@ -1,2 +1,2 @@\n- velha\n+ nova\n'
    saida = v.estilizar(diff)
    assert '•' not in saida
    assert '\x1b[31m' in saida and '\x1b[32m' in saida
    assert '- velha' in _visivel(saida) and '+ nova' in _visivel(saida)
