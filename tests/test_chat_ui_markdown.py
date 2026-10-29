from cli.chat_ui import Visual


def test_markdown_vira_estilo_so_quando_a_linha_completa():
    v = Visual(cor=True, largura=80)
    v.novo_turno()
    assert v.estilizar('## Tit') == ''
    saida = v.estilizar('ulo\n')
    assert '\x1b[1;38;5;37mTitulo' in saida and '##' not in saida


def test_linha_parcial_e_descarregada_antes_de_um_erro():
    v = Visual(cor=False, largura=80)
    v.novo_turno()
    v.estilizar('texto sem quebra')
    saida = v.estilizar('Erro (500): caiu\n')
    assert saida.startswith('● texto sem quebra\n') and saida.endswith('Erro (500): caiu\n')


def test_linha_de_markdown_com_colchete_nao_vira_status():
    v = Visual(cor=False, largura=80)
    v.novo_turno()
    v.estilizar('Veja:\n')
    assert v.estilizar('[doc](http://x)\n') == '  [doc](http://x)\n'


def test_codigo_dentro_de_negrito_e_desenhado():
    v = Visual(cor=True, largura=80)
    v.novo_turno()
    saida = v.estilizar('**1. `heapq` em vez de lista**\n')
    assert '`' not in saida and '38;5;180mheapq' in saida


def test_resposta_do_modelo_em_pedacos_picados_mantem_as_linhas():
    v = Visual(cor=False, largura=80)
    v.novo_turno()
    pedacos = ['Ol', 'a!\n\nSegu', 'e ', 'a lista:\n- um\n- do', 'is\n', '\n']
    saida = ''.join(v.estilizar(p) for p in pedacos)
    assert saida == '● Ola!\n\n  Segue a lista:\n  - um\n  - dois\n\n'


def test_banner_lista_os_arquivos_de_instrucoes():
    texto = Visual(cor=False, largura=80).banner(
        versao='1', modelo='m', pasta='/p', modo='', maestro=False,
        instrucoes=1, arquivos=('~/.hera/HERA.md',),
    )
    assert '1 arquivo(s) de instrucoes carregado(s): ~/.hera/HERA.md' in texto
