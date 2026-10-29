from __future__ import annotations

import asyncio

import pytest

from app.goal import (
    MAX_TURNOS_LIMITE,
    ResultadoGoal,
    Veredito,
    montar_prompt_juiz,
    parse_veredito,
    perseguir_objetivo,
    prompt_do_turno,
)


def test_parse_veredito_palavras():
    assert parse_veredito('FEITO\nok').estado == 'feito'
    assert parse_veredito('CONTINUAR\nfalta').estado == 'continuar'
    assert parse_veredito('BLOQUEADO\npreciso de chave').estado == 'bloqueado'


def test_parse_veredito_minusculas():
    assert parse_veredito('feito\nok').estado == 'feito'
    assert parse_veredito('continuar\nfalta').estado == 'continuar'
    assert parse_veredito('bloqueado\npreciso').estado == 'bloqueado'


def test_parse_veredito_acento_pontuacao():
    assert parse_veredito('CONCLUÍDO!\nok').estado == 'feito'
    assert parse_veredito('BLOQUEADO.\npreciso').estado == 'bloqueado'
    assert parse_veredito('CONTINUAR,\nfalta').estado == 'continuar'


def test_parse_veredito_ilegivel():
    v = parse_veredito('talvez\nsei la')
    assert v.estado == 'continuar'
    assert v.motivo == 'veredito ilegivel'


def test_parse_veredito_vazio():
    v = parse_veredito('')
    assert v.estado == 'continuar'
    assert v.motivo == 'veredito ilegivel'


def test_parse_veredito_motivo_primeira_linha():
    v = parse_veredito('FEITO porque sim')
    assert v.estado == 'feito'
    assert v.motivo == 'porque sim'


def test_montar_prompt_juiz_contem_objetivo():
    prompt = montar_prompt_juiz('criar relatorio', 'resposta qualquer')
    assert 'criar relatorio' in prompt
    assert 'resposta qualquer' in prompt


def test_montar_prompt_juiz_corta_resposta():
    resposta = 'x' * 10000
    prompt = montar_prompt_juiz('obj', resposta)
    assert 'x' * 6000 in prompt
    assert 'x' * 6001 not in prompt


def test_prompt_do_turno_primeiro():
    prompt = prompt_do_turno('obj', 1, 5)
    assert 'Objetivo: obj' in prompt
    assert 'autonoma' in prompt


def test_prompt_do_turno_seguintes():
    prompt = prompt_do_turno('obj', 2, 5, 'falta x')
    assert 'turno 2 de 5' in prompt
    assert 'falta x' in prompt


async def test_perseguir_feito_primeiro_turno():
    chamadas_turno = []
    chamadas_juiz = []

    async def turno(prompt: str) -> str:
        chamadas_turno.append(prompt)
        return 'fiz tudo'

    async def julgar(prompt: str) -> str:
        chamadas_juiz.append(prompt)
        return 'FEITO\nok'

    r = await perseguir_objetivo('obj', turno=turno, julgar=julgar)
    assert r.estado == 'feito'
    assert r.turnos == 1
    assert len(chamadas_turno) == 1
    assert len(chamadas_juiz) == 1


async def test_perseguir_continuar_depois_feito():
    respostas = iter(['FEITO\nok'])

    async def turno(prompt: str) -> str:
        return 'resposta'

    async def julgar(prompt: str) -> str:
        return next(respostas, 'FEITO\nok')

    r = await perseguir_objetivo('obj', turno=turno, julgar=julgar)
    assert r.estado == 'feito'


async def test_perseguir_continuar_ate_feito():
    contador = {'n': 0}

    async def turno(prompt: str) -> str:
        return 'resposta'

    async def julgar(prompt: str) -> str:
        contador['n'] += 1
        if contador['n'] < 3:
            return 'CONTINUAR\nfalta'
        return 'FEITO\nok'

    r = await perseguir_objetivo('obj', turno=turno, julgar=julgar)
    assert r.estado == 'feito'
    assert r.turnos == 3


async def test_perseguir_bloqueado():
    async def turno(prompt: str) -> str:
        return 'resposta'

    async def julgar(prompt: str) -> str:
        return 'BLOQUEADO\npreciso de chave'

    r = await perseguir_objetivo('obj', turno=turno, julgar=julgar)
    assert r.estado == 'bloqueado'
    assert r.turnos == 1
    assert r.motivo == 'preciso de chave'


async def test_perseguir_esgota_limite():
    chamadas_turno = []
    chamadas_juiz = []

    async def turno(prompt: str) -> str:
        chamadas_turno.append(prompt)
        return 'resposta'

    async def julgar(prompt: str) -> str:
        chamadas_juiz.append(prompt)
        return 'CONTINUAR\nfalta'

    r = await perseguir_objetivo('obj', turno=turno, julgar=julgar, max_turnos=3)
    assert r.estado == 'limite'
    assert r.turnos == 3
    assert len(chamadas_turno) == 3
    assert len(chamadas_juiz) == 3


async def test_perseguir_clamp_max_turnos():
    chamadas = []

    async def turno(prompt: str) -> str:
        chamadas.append(prompt)
        return 'resposta'

    async def julgar(prompt: str) -> str:
        return 'CONTINUAR\nfalta'

    r0 = await perseguir_objetivo('obj', turno=turno, julgar=julgar, max_turnos=0)
    assert r0.turnos == 1

    chamadas.clear()
    r99 = await perseguir_objetivo('obj', turno=turno, julgar=julgar, max_turnos=99)
    assert r99.turnos == MAX_TURNOS_LIMITE


async def test_perseguir_objetivo_vazio():
    async def turno(prompt: str) -> str:
        return 'x'

    async def julgar(prompt: str) -> str:
        return 'FEITO\nok'

    r = await perseguir_objetivo('', turno=turno, julgar=julgar)
    assert r.estado == 'erro'
    assert r.turnos == 0
    assert r.motivo == 'objetivo vazio'


async def test_perseguir_erro_no_turno():
    async def turno(prompt: str) -> str:
        raise ValueError('falhou')

    async def julgar(prompt: str) -> str:
        return 'FEITO\nok'

    r = await perseguir_objetivo('obj', turno=turno, julgar=julgar)
    assert r.estado == 'erro'
    assert r.turnos == 1
    assert 'ValueError' in r.motivo


async def test_perseguir_erro_no_juiz_para_tudo():
    chamadas_turno = []

    async def turno(prompt: str) -> str:
        chamadas_turno.append(prompt)
        return 'resposta'

    async def julgar(prompt: str) -> str:
        raise RuntimeError('juiz caiu')

    r = await perseguir_objetivo('obj', turno=turno, julgar=julgar, max_turnos=5)
    assert r.estado == 'erro'
    assert r.turnos == 1
    assert len(chamadas_turno) == 1
    assert 'RuntimeError' in r.motivo


async def test_perseguir_cancelled_propaga():
    async def turno(prompt: str) -> str:
        raise asyncio.CancelledError()

    async def julgar(prompt: str) -> str:
        return 'FEITO\nok'

    with pytest.raises(asyncio.CancelledError):
        await perseguir_objetivo('obj', turno=turno, julgar=julgar)


async def test_perseguir_mensagens_escritas():
    mensagens = []

    async def turno(prompt: str) -> str:
        return 'resposta'

    async def julgar(prompt: str) -> str:
        return 'FEITO\nok'

    await perseguir_objetivo(
        'obj', turno=turno, julgar=julgar, escrever=mensagens.append
    )
    assert '[goal: turno 1 de 5]\n' in mensagens
    assert any(m.startswith('[goal: feito') for m in mensagens)
