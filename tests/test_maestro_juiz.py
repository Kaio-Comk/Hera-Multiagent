# tests/test_maestro_juiz.py
import asyncio

import pytest

from app.maestro import Maestro, ler_faixa, regra_incerta


DISPONIVEIS = [
    "deepseek-ai/DeepSeek-V4.1-Flash",
    "GLM-5.3",
    "gemini-flash-lite-latest",
]


def _maestro(juiz=None, timeout=8.0):
    async def disponiveis_de():
        return list(DISPONIVEIS)

    m = Maestro(disponiveis_de=disponiveis_de)
    m.juiz = juiz
    m.timeout_juiz = timeout
    return m


# ---------- ler_faixa ----------

def test_ler_faixa_leve():
    assert ler_faixa("leve") == "leve"


def test_ler_faixa_medio_com_ponto():
    assert ler_faixa("Medio.") == "medio"


def test_ler_faixa_prefixo():
    assert ler_faixa("FAIXA: pesado") == "pesado"


def test_ler_faixa_acentuado():
    assert ler_faixa("médio") == "medio"


def test_ler_faixa_invalido():
    assert ler_faixa("") is None
    assert ler_faixa(None) is None
    assert ler_faixa("nao sei") is None


# ---------- regra_incerta ----------

def test_regra_incerta_pedido_comum():
    assert regra_incerta("medio", "pedido comum", "qualquer coisa") is True


def test_regra_incerta_pesado_palavra_curta():
    assert regra_incerta("pesado", "palavra-chave pesada: senha", "senha") is True


def test_regra_incerta_pesado_palavra_longa():
    texto = "x" * 250
    assert regra_incerta("pesado", "palavra-chave pesada: senha", texto) is False


def test_regra_incerta_outros():
    assert regra_incerta("leve", "saudacao", "ola") is False
    assert regra_incerta("medio", "explicar", "explique x") is False


# ---------- escolher ----------

async def test_sem_juiz_resultado_nao_muda():
    m = _maestro(juiz=None)
    e = await m.escolher("crie um sistema de filas")
    assert e.peso == "medio"
    assert e.motivo == "pedido comum"


async def test_juiz_nao_chamado_em_saudacao():
    chamadas = []

    async def juiz(texto):
        chamadas.append(texto)
        return "pesado"

    m = _maestro(juiz=juiz)
    e = await m.escolher("ola")
    assert chamadas == []
    assert e.peso == "leve"


async def test_juiz_chamado_em_pedido_comum():
    chamadas = []

    async def juiz(texto):
        chamadas.append(texto)
        return "medio"

    m = _maestro(juiz=juiz)
    await m.escolher("crie um sistema de filas")
    assert len(chamadas) == 1


async def test_juiz_pesado_escolhe_glm():
    async def juiz(texto):
        return "pesado"

    m = _maestro(juiz=juiz)
    e = await m.escolher("crie um sistema de filas")
    assert e.modelo == "GLM-5.3"
    assert e.peso == "pesado"
    assert "juiz" in e.motivo


async def test_juiz_excecao_mantem_local():
    async def juiz(texto):
        raise RuntimeError("boom")

    m = _maestro(juiz=juiz)
    e = await m.escolher("crie um sistema de filas")
    assert e.peso == "medio"
    assert e.motivo == "pedido comum"


async def test_juiz_timeout_mantem_local():
    async def juiz(texto):
        await asyncio.sleep(1.0)
        return "pesado"

    m = _maestro(juiz=juiz, timeout=0.05)
    e = await m.escolher("crie um sistema de filas")
    assert e.peso == "medio"
    assert e.motivo == "pedido comum"


async def test_juiz_resposta_lixo_mantem_local():
    async def juiz(texto):
        return "nao sei"

    m = _maestro(juiz=juiz)
    e = await m.escolher("crie um sistema de filas")
    assert e.peso == "medio"
    assert e.motivo == "pedido comum"


async def test_forcado_nao_chama_juiz():
    chamadas = []

    async def juiz(texto):
        chamadas.append(texto)
        return "pesado"

    m = _maestro(juiz=juiz)
    m.forcado = "GLM-5.3"
    e = await m.escolher("crie um sistema de filas")
    assert chamadas == []
    assert e.modelo == "GLM-5.3"
    assert e.forcada is True


async def test_desligado_nao_chama_juiz():
    chamadas = []

    async def juiz(texto):
        chamadas.append(texto)
        return "pesado"

    m = _maestro(juiz=juiz)
    m.ativo = False
    e = await m.escolher("crie um sistema de filas")
    assert chamadas == []
    assert e.modelo == "default"
    assert e.motivo == "maestro desligado"
