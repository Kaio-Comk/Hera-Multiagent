from __future__ import annotations

import pytest

from app.maestro import (
    PREFERENCIAS_PADRAO,
    Escolha,
    Maestro,
    classificar_local,
    escolher_modelo,
)


class RelogioFalso:
    """Relogio controlavel para testes."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def avancar(self, dt: float) -> None:
        self.t += dt


def test_classificar_saudacao() -> None:
    peso, _ = classificar_local('oi')
    assert peso == 'leve'


def test_classificar_pergunta_curta() -> None:
    peso, _ = classificar_local('qual a capital da Franca?')
    assert peso == 'leve'


def test_classificar_refatore() -> None:
    peso, _ = classificar_local('refatore o modulo de pagamentos')
    assert peso == 'pesado'


def test_classificar_texto_longo() -> None:
    peso, _ = classificar_local('a' * 1600)
    assert peso == 'pesado'


def test_classificar_bloco_codigo_grande() -> None:
    bloco = '```\n' + '\n'.join(f'linha {i}' for i in range(50)) + '\n```'
    peso, _ = classificar_local(bloco)
    assert peso == 'pesado'


def test_classificar_palavra_chave_com_acento() -> None:
    peso, _ = classificar_local('preciso de seguranca no sistema')
    assert peso == 'pesado'


def test_classificar_palavra_chave_sem_acento() -> None:
    peso, _ = classificar_local('preciso de seguranca no sistema')
    assert peso == 'pesado'


def test_escolher_modelo_preferencia() -> None:
    disponiveis = ['gpt-4o-mini', 'gemini-flash-lite-latest']
    modelo = escolher_modelo('leve', disponiveis)
    assert modelo == 'gemini-flash-lite-latest'


def test_escolher_modelo_vizinha() -> None:
    disponiveis = ['gpt-oss-120b']
    modelo = escolher_modelo('leve', disponiveis)
    assert modelo == 'gpt-oss-120b'


def test_escolher_modelo_indisponivel() -> None:
    disponiveis = ['gemini-flash-lite-latest', 'gpt-4o-mini']
    modelo = escolher_modelo(
        'leve', disponiveis, indisponiveis=['gemini-flash-lite-latest']
    )
    assert modelo == 'gpt-4o-mini'


def test_escolher_modelo_nenhum() -> None:
    modelo = escolher_modelo('leve', [])
    assert modelo is None


def test_perfil_economico() -> None:
    from app.maestro import PERFIS

    assert PERFIS['economico']['leve'] == PREFERENCIAS_PADRAO['leve'][:2]
    assert PERFIS['economico']['medio'] == PREFERENCIAS_PADRAO['leve'][:2]
    assert PERFIS['economico']['pesado'] == PREFERENCIAS_PADRAO['medio']


def test_perfil_qualidade() -> None:
    from app.maestro import PERFIS

    assert PERFIS['qualidade']['leve'] == PREFERENCIAS_PADRAO['medio']
    assert PERFIS['qualidade']['medio'] == PREFERENCIAS_PADRAO['pesado']
    assert PERFIS['qualidade']['pesado'] == PREFERENCIAS_PADRAO['pesado']


async def test_maestro_forcado() -> None:
    async def disp() -> list[str]:
        return ['gpt-4o-mini']

    m = Maestro(disp, forcado='meu-modelo')
    e = await m.escolher('oi')
    assert e.modelo == 'meu-modelo'
    assert e.forcada is True


async def test_maestro_desligado() -> None:
    async def disp() -> list[str]:
        return ['gpt-4o-mini']

    m = Maestro(disp, ativo=False)
    e = await m.escolher('refatore tudo')
    assert e.modelo == 'default'
    assert e.motivo == 'maestro desligado'


async def test_maestro_cache_ttl() -> None:
    chamadas = {'n': 0}

    async def disp() -> list[str]:
        chamadas['n'] += 1
        return ['gemini-flash-lite-latest']

    relogio = RelogioFalso()
    m = Maestro(disp, ttl=60.0, relogio=relogio)
    await m.escolher('oi')
    await m.escolher('oi')
    assert chamadas['n'] == 1
    relogio.avancar(61.0)
    await m.escolher('oi')
    assert chamadas['n'] == 2


async def test_maestro_erro_usa_cache() -> None:
    estado = {'falhar': False}

    async def disp() -> list[str]:
        if estado['falhar']:
            raise RuntimeError('boom')
        return ['gemini-flash-lite-latest']

    relogio = RelogioFalso()
    m = Maestro(disp, ttl=10.0, relogio=relogio)
    await m.escolher('oi')
    estado['falhar'] = True
    relogio.avancar(11.0)
    e = await m.escolher('oi')
    assert e.modelo == 'gemini-flash-lite-latest'


async def test_maestro_marcar_indisponivel_expira() -> None:
    async def disp() -> list[str]:
        return ['gemini-flash-lite-latest', 'gpt-4o-mini']

    relogio = RelogioFalso()
    m = Maestro(disp, ttl=1000.0, relogio=relogio)
    m.marcar_indisponivel('gemini-flash-lite-latest', segundos=5.0)
    e = await m.escolher('oi')
    assert e.modelo == 'gpt-4o-mini'
    relogio.avancar(6.0)
    e2 = await m.escolher('oi')
    assert e2.modelo == 'gemini-flash-lite-latest'


async def test_maestro_escalar() -> None:
    async def disp() -> list[str]:
        return ['gpt-oss-120b']

    m = Maestro(disp)
    e = await m.escolher('oi')
    assert e.peso == 'leve'
    e2 = m.escalar(e)
    assert e2 is not None
    assert e2.peso == 'medio'


async def test_maestro_escalar_forcada() -> None:
    async def disp() -> list[str]:
        return ['x']

    m = Maestro(disp, forcado='x')
    e = await m.escolher('oi')
    assert m.escalar(e) is None


async def test_maestro_escalar_pesado() -> None:
    async def disp() -> list[str]:
        return ['GLM-5.3']

    m = Maestro(disp)
    e = await m.escolher('refatore tudo')
    assert e.peso == 'pesado'
    assert m.escalar(e) is None


def test_maestro_perfil_invalido() -> None:
    async def disp() -> list[str]:
        return []

    with pytest.raises(ValueError):
        Maestro(disp, perfil='inexistente')


async def test_maestro_sem_candidato() -> None:
    async def disp() -> list[str]:
        return []

    m = Maestro(disp)
    e = await m.escolher('oi')
    assert e.modelo == 'default'
    assert e.motivo == 'nenhum modelo da faixa disponivel'


async def test_escolher_faixa_faixas_e_invalido() -> None:
    async def disp() -> list[str]:
        return [
            'gemini-flash-lite-latest',
            'deepseek-ai/DeepSeek-V4.1-Flash',
            'GLM-5.3',
        ]

    m = Maestro(disp)
    e_leve = await m.escolher_faixa('leve')
    assert e_leve.modelo == 'deepseek-ai/DeepSeek-V4.1-Flash'  # primeiro da faixa leve desde 29/10/2026
    assert e_leve.peso == 'leve'

    e_pesado = await m.escolher_faixa('pesado')
    assert e_pesado.modelo == 'GLM-5.3'
    assert e_pesado.peso == 'pesado'

    e_invalido = await m.escolher_faixa('super_pesado')
    assert e_invalido.peso == 'medio'
    assert e_invalido.modelo == 'deepseek-ai/DeepSeek-V4.1-Flash'


async def test_escolher_faixa_perfil() -> None:
    async def disp() -> list[str]:
        return [
            'gemini-flash-lite-latest',
            'deepseek-ai/DeepSeek-V4.1-Flash',
            'GLM-5.3',
        ]

    m_econ = Maestro(disp, perfil='economico')
    e_econ = await m_econ.escolher_faixa('pesado')
    assert e_econ.modelo == 'deepseek-ai/DeepSeek-V4.1-Flash'

    m_qual = Maestro(disp, perfil='qualidade')
    e_qual = await m_qual.escolher_faixa('medio')
    assert e_qual.modelo == 'GLM-5.3'


async def test_escolher_faixa_indisponivel() -> None:
    async def disp() -> list[str]:
        return ['gemini-flash-lite-latest', 'gpt-4o-mini']

    relogio = RelogioFalso()
    m = Maestro(disp, relogio=relogio)
    m.marcar_indisponivel('gemini-flash-lite-latest', segundos=60.0)

    e = await m.escolher_faixa('leve')
    assert e.modelo == 'gpt-4o-mini'


async def test_escolher_faixa_desligado() -> None:
    async def disp() -> list[str]:
        return ['gemini-flash-lite-latest']

    m = Maestro(disp, ativo=False)
    e = await m.escolher_faixa('leve')
    assert e.modelo == 'default'
    assert e.motivo == 'maestro desligado'


async def test_escolher_faixa_forcado_ignorado() -> None:
    async def disp() -> list[str]:
        return ['gemini-flash-lite-latest', 'gpt-4o-mini']

    m = Maestro(disp, forcado='meu-modelo-forcado')
    e = await m.escolher_faixa('leve')
    assert e.modelo == 'gemini-flash-lite-latest'
    assert e.forcada is False
