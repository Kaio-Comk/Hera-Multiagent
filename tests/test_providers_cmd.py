from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from cli import providers_cmd
from cli.chat_client import ErroDoServidor


class ChamarFalso:
    def __init__(self, respostas=None):
        self.respostas = respostas or {}
        self.chamadas = []

    def __call__(self, metodo, caminho, corpo=None):
        self.chamadas.append((metodo, caminho, corpo))
        chave = (metodo, caminho)
        if chave in self.respostas:
            valor = self.respostas[chave]
            if isinstance(valor, Exception):
                raise valor
            return valor
        return {}


def _args(**kwargs):
    base = {'url': 'http://127.0.0.1:8000'}
    base.update(kwargs)
    return argparse.Namespace(**base)


def _estado(presets=None, providers=None):
    return {'presets': presets or [], 'providers': providers or []}


def test_list_vazio():
    chamar = ChamarFalso({('GET', '/api/router/state'): _estado()})
    saidas = []
    codigo = providers_cmd.executar(
        _args(acao='list', catalogo=False, gratis=False),
        chamar=chamar,
        saida=saidas.append,
    )
    assert codigo == 0
    assert saidas == ['Nenhum provider configurado.']


def test_list_cheio():
    estado = _estado(providers=[
        {
            'name': 'openai',
            'enabled': True,
            'tier': 'cloud_premium',
            'default_model': 'gpt-4o',
            'keys_found': 2,
            'today': {'requests': 3, 'tokens': 100},
        }
    ])
    chamar = ChamarFalso({('GET', '/api/router/state'): estado})
    saidas = []
    codigo = providers_cmd.executar(
        _args(acao='list', catalogo=False, gratis=False),
        chamar=chamar,
        saida=saidas.append,
    )
    assert codigo == 0
    assert saidas == ['openai | cloud_premium | sim | 2 | gpt-4o | 3 req, 100 tok']


def test_catalogo_com_gratis():
    estado = _estado(presets=[
        {'id': 'a', 'label': 'A', 'tier': 'cloud_free', 'default_model': 'm1'},
        {'id': 'b', 'label': 'B', 'tier': 'cloud_premium', 'default_model': 'm2'},
    ])
    chamar = ChamarFalso({('GET', '/api/router/state'): estado})
    saidas = []
    codigo = providers_cmd.executar(
        _args(acao='list', catalogo=True, gratis=False),
        chamar=chamar,
        saida=saidas.append,
    )
    assert codigo == 0
    assert saidas == ['a | cloud_free | A [gratis] | m1', 'b | cloud_premium | B | m2']


def test_catalogo_gratis_filtra():
    estado = _estado(presets=[
        {'id': 'a', 'label': 'A', 'tier': 'cloud_free', 'default_model': 'm1'},
        {'id': 'b', 'label': 'B', 'tier': 'cloud_premium', 'default_model': 'm2'},
    ])
    chamar = ChamarFalso({('GET', '/api/router/state'): estado})
    saidas = []
    codigo = providers_cmd.executar(
        _args(acao='list', catalogo=True, gratis=True),
        chamar=chamar,
        saida=saidas.append,
    )
    assert codigo == 0
    assert saidas == ['a | cloud_free | A [gratis] | m1']


def test_add_completo():
    estado = _estado(presets=[
        {
            'id': 'openai',
            'label': 'OpenAI',
            'tier': 'cloud_premium',
            'default_model': 'gpt-4o',
            'key_env': 'OPENAI_API_KEY',
            'note': 'Dica: use gpt-4o-mini.',
        }
    ])
    chamar = ChamarFalso({
        ('GET', '/api/router/state'): estado,
        ('POST', '/api/router/providers'): {'name': 'meu'},
        ('POST', '/api/router/test'): {'ok': True, 'latency_ms': 42},
    })
    saidas = []
    codigo = providers_cmd.executar(
        _args(
            acao='add',
            preset='openai',
            nome='meu',
            chave=None,
            param=['temperature=0.2'],
            limite_diario=10,
            desligado=False,
        ),
        chamar=chamar,
        pedir_segredo=lambda _p: 'segredo',
        saida=saidas.append,
    )
    assert codigo == 0
    assert chamar.chamadas[1] == (
        'POST',
        '/api/router/providers',
        {
            'preset': 'openai',
            'enabled': True,
            'name': 'meu',
            'daily_request_limit': 10,
            'params': {'temperature': '0.2'},
        },
    )
    assert chamar.chamadas[2] == (
        'POST',
        '/api/router/providers/meu/keys',
        {'keys': ['segredo'], 'mode': 'replace'},
    )
    assert chamar.chamadas[3] == ('POST', '/api/router/test', {'provider': 'meu'})
    assert 'OK em 42 ms' in saidas
    assert 'Dica: use gpt-4o-mini.' in saidas


def test_add_preset_inexistente():
    estado = _estado(presets=[{'id': 'openai', 'label': 'OpenAI', 'tier': 'cloud_premium'}])
    chamar = ChamarFalso({('GET', '/api/router/state'): estado})
    saidas = []
    codigo = providers_cmd.executar(
        _args(acao='add', preset='openaii', nome=None, chave=None, param=[], limite_diario=None, desligado=False),
        chamar=chamar,
        saida=saidas.append,
    )
    assert codigo == 1
    assert 'openai' in saidas[0]


def test_add_sem_chave_avisa():
    estado = _estado(presets=[
        {'id': 'openai', 'label': 'OpenAI', 'tier': 'cloud_premium', 'key_env': 'OPENAI_API_KEY'}
    ])
    chamar = ChamarFalso({
        ('GET', '/api/router/state'): estado,
        ('POST', '/api/router/providers'): {'name': 'openai'},
        ('POST', '/api/router/test'): {'ok': True, 'latency_ms': 5},
    })
    saidas = []
    codigo = providers_cmd.executar(
        _args(acao='add', preset='openai', nome=None, chave=None, param=[], limite_diario=None, desligado=False),
        chamar=chamar,
        pedir_segredo=lambda _p: '',
        saida=saidas.append,
    )
    assert codigo == 0
    caminhos = [c[1] for c in chamar.chamadas]
    assert '/api/router/providers/openai/keys' not in caminhos
    assert any('sem chave' in s for s in saidas)


def test_add_params_pedidos():
    estado = _estado(presets=[
        {
            'id': 'local',
            'label': 'Local',
            'tier': 'local',
            'params': [{'key': 'base_url', 'label': 'URL base'}],
        }
    ])
    chamar = ChamarFalso({
        ('GET', '/api/router/state'): estado,
        ('POST', '/api/router/providers'): {'name': 'local'},
        ('POST', '/api/router/test'): {'ok': True, 'latency_ms': 1},
    })
    saidas = []
    codigo = providers_cmd.executar(
        _args(acao='add', preset='local', nome=None, chave=None, param=[], limite_diario=None, desligado=False),
        chamar=chamar,
        pedir_segredo=lambda _p: '',
        perguntar=lambda _p: 'http://127.0.0.1:1234',
        saida=saidas.append,
    )
    assert codigo == 0
    corpo = chamar.chamadas[1][2]
    assert corpo['params'] == {'base_url': 'http://127.0.0.1:1234'}


def test_chave_nunca_na_saida():
    estado = _estado(presets=[
        {'id': 'openai', 'label': 'OpenAI', 'tier': 'cloud_premium', 'key_env': 'OPENAI_API_KEY'}
    ])
    chamar = ChamarFalso({
        ('GET', '/api/router/state'): estado,
        ('POST', '/api/router/providers'): {'name': 'openai'},
        ('POST', '/api/router/test'): {'ok': True, 'latency_ms': 5},
    })
    saidas = []
    codigo = providers_cmd.executar(
        _args(acao='add', preset='openai', nome=None, chave='sk-secreta', param=[], limite_diario=None, desligado=False),
        chamar=chamar,
        saida=saidas.append,
    )
    assert codigo == 0
    texto = '\n'.join(str(s) for s in saidas)
    assert 'sk-secreta' not in texto
    assert 'sk-' not in texto


def test_key_acrescentar():
    chamar = ChamarFalso({('POST', '/api/router/providers/openai/keys'): {}})
    saidas = []
    codigo = providers_cmd.executar(
        _args(acao='key', nome='openai', chave='abc', acrescentar=True),
        chamar=chamar,
        saida=saidas.append,
    )
    assert codigo == 0
    assert chamar.chamadas[0] == (
        'POST',
        '/api/router/providers/openai/keys',
        {'keys': ['abc'], 'mode': 'append'},
    )
    assert 'abc' not in '\n'.join(str(s) for s in saidas)


def test_remove_cancela_com_n():
    chamar = ChamarFalso({})
    saidas = []
    codigo = providers_cmd.executar(
        _args(acao='remove', nome='openai', sim=False),
        chamar=chamar,
        confirmar=lambda _p: False,
        saida=saidas.append,
    )
    assert codigo == 0
    assert chamar.chamadas == []
    assert saidas == ['Cancelado.']


def test_enable_disable():
    chamar = ChamarFalso({
        ('POST', '/api/router/providers/openai/enabled'): {},
    })
    saidas = []
    codigo = providers_cmd.executar(
        _args(acao='enable', nome='openai'),
        chamar=chamar,
        saida=saidas.append,
    )
    assert codigo == 0
    assert chamar.chamadas[0] == (
        'POST',
        '/api/router/providers/openai/enabled',
        {'enabled': True},
    )
    chamar2 = ChamarFalso({('POST', '/api/router/providers/openai/enabled'): {}})
    codigo2 = providers_cmd.executar(
        _args(acao='disable', nome='openai'),
        chamar=chamar2,
        saida=saidas.append,
    )
    assert codigo2 == 0
    assert chamar2.chamadas[0] == (
        'POST',
        '/api/router/providers/openai/enabled',
        {'enabled': False},
    )


def test_models():
    chamar = ChamarFalso({
        ('POST', '/api/router/providers/openai/discover'): {'models': ['m1', 'm2']},
    })
    saidas = []
    codigo = providers_cmd.executar(
        _args(acao='models', nome='openai'),
        chamar=chamar,
        saida=saidas.append,
    )
    assert codigo == 0
    assert saidas == ['m1', 'm2']


def test_limits_so_envia_informado():
    chamar = ChamarFalso({('POST', '/api/router/providers/openai/limits'): {}})
    saidas = []
    codigo = providers_cmd.executar(
        _args(acao='limits', nome='openai', prioridade=1, rpm=None, diario_req=100, diario_tokens=None),
        chamar=chamar,
        saida=saidas.append,
    )
    assert codigo == 0
    assert chamar.chamadas[0] == (
        'POST',
        '/api/router/providers/openai/limits',
        {'priority': 1, 'daily_requests': 100},
    )


def test_erro_servidor_codigo_1():
    chamar = ChamarFalso({
        ('GET', '/api/router/state'): ErroDoServidor(500, 'falhou'),
    })
    saidas = []
    codigo = providers_cmd.executar(
        _args(acao='list', catalogo=False, gratis=False),
        chamar=chamar,
        saida=saidas.append,
    )
    assert codigo == 1
    assert saidas == ['Erro (500): falhou']


def test_registrar_cria_subcomandos():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='acao')
    providers_cmd.registrar(sub)
    args = parser.parse_args(['providers', 'list', '--catalogo'])
    assert args.acao == 'list'
    assert args.catalogo is True
    args2 = parser.parse_args(['providers', 'add', 'openai', '--param', 'a=b'])
    assert args2.acao == 'add'
    assert args2.param == ['a=b']


def test_raiz_padrao_e_a_pasta_do_projeto_para_ler_o_token(monkeypatch):
    """Regressao: sem `raiz`, o token nunca era lido e o comando real recebia 401."""
    from pathlib import Path
    recebido = {}

    def falso(raiz, url):
        recebido['raiz'] = raiz
        return lambda metodo, caminho, corpo=None: {'presets': [], 'providers': []}

    monkeypatch.setattr(providers_cmd, '_chamar_padrao', falso)
    codigo = providers_cmd.executar(_args(acao='list', catalogo=False, gratis=False), saida=lambda *_a: None)
    assert codigo == 0
    assert isinstance(recebido['raiz'], Path) and (recebido['raiz'] / 'main.py').exists()


def _lista_com(estado_extra, providers=None):
    base = {'presets': [], 'providers': providers or [{'name': 'river', 'tier': 'cloud_cheap', 'enabled': True, 'keys_found': 1,
                                                       'default_model': 'm', 'today': {'requests': 1, 'tokens': 2}}]}
    base.update(estado_extra)
    chamar = ChamarFalso({('GET', '/api/router/state'): base})
    saidas = []
    providers_cmd.executar(_args(acao='list', catalogo=False, gratis=False), chamar=chamar, saida=saidas.append)
    return saidas


def test_list_mostra_teto_gasto_e_aderencia():
    saidas = _lista_com({'daily_budget_usd': 1.5, 'today_cost_usd': 0.0421, 'sticky_minutes': 15})
    texto = '\n'.join(saidas)
    assert 'Teto diario: US$ 1.50' in texto and 'gasto hoje: US$ 0.0421' in texto and 'aderencia: 15 min' in texto


def test_list_sem_teto_diz_sem_teto_e_mostra_zero_como_so_gratis():
    assert 'sem teto' in '\n'.join(_lista_com({'daily_budget_usd': None, 'today_cost_usd': 0.0, 'sticky_minutes': 0}))
    texto = '\n'.join(_lista_com({'daily_budget_usd': 0.0, 'today_cost_usd': 0.0, 'sticky_minutes': 0}))
    assert 'so gratuitos e locais' in texto and 'aderencia: desligada' in texto


def test_list_tolera_servidor_antigo_sem_os_campos():
    assert not any('Teto diario' in s for s in _lista_com({}))
