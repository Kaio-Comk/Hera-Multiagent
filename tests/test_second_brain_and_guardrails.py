from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from pathlib import Path

from app.telegram.guardrails import (
    aplicar_guardrails,
    sanitizar_segredos,
    validar_seguranca_geral,
    verificar_tentativa_liberar_plano,
)
from app.telegram.second_brain import ObsidianSecondBrain
from app.telegram.customer_support import CustomerSupportService
from app.telegram.schemas import CustomerMessageRequest


def test_guardrails_bloqueio_vip_gratis():
    # 1. Tentativa de liberar VIP
    violou, texto = verificar_tentativa_liberar_plano("pronto, liberei seu vip gratis por 30 dias")
    assert violou is True
    assert "mediante assinatura ativa" in texto
    assert "/assinatura" in texto

    # 2. Concessão de dias
    violou_dias, texto_dias = verificar_tentativa_liberar_plano("adicionei 7 dias no seu plano")
    assert violou_dias is True
    assert "/assinatura" in texto_dias

    # 3. Frase legítima não viola
    violou_ok, texto_ok = verificar_tentativa_liberar_plano("O plano completo custa R$ 9,90 por mês")
    assert violou_ok is False
    assert "9,90" in texto_ok


def test_guardrails_sanitizacao_segredos():
    # 1. Telegram Bot Token
    texto_token = "O token do bot é 8110665027:AAHdfjsdfhsdjfhsdfjhsdjhfskjdhfk aproveite"
    sanitizado = sanitizar_segredos(texto_token)
    assert "8110665027:" not in sanitizado
    assert "[DADO CONFIDENCIAL PROTEGIDO]" in sanitizado

    # 2. Database Connection URL
    texto_db = "Conecte no banco em postgresql://postgres:senhaUltraSecreta123@localhost:5432/nethub"
    sanitizado_db = sanitizar_segredos(texto_db)
    assert "senhaUltraSecreta123" not in sanitizado_db
    assert "[DADO CONFIDENCIAL PROTEGIDO]" in sanitizado_db

    # 3. Stripe Secret Key
    texto_stripe = "Chave sk_live_51OzKjhdf78234678234628374628"
    sanitizado_stripe = sanitizar_segredos(texto_stripe)
    assert "sk_live_" not in sanitizado_stripe
    assert "[DADO CONFIDENCIAL PROTEGIDO]" in sanitizado_stripe


def test_guardrails_bloqueio_comandos_destrutivos():
    ok, msg = validar_seguranca_geral("drop table usuarios; select 1;")
    assert ok is False
    assert "não permitida" in msg

    ok_limpo, _ = validar_seguranca_geral("Para assistir na TV use o app Magic Player")
    assert ok_limpo is True


def test_obsidian_second_brain_busca(tmp_path):
    # Criar um vault temporário para teste unitário
    vault_dir = tmp_path / "Vault"
    pasta_nethub = vault_dir / "NetHub"
    pasta_decisoes = pasta_nethub / "04 Decisões"
    pasta_decisoes.mkdir(parents=True)

    nota_roku = pasta_decisoes / "2026-11-07 — Suporte Roku TV.md"
    nota_roku.write_text(
        "# Suporte Roku TV\nNa Roku TV use o app Magic Player com a URL m3u_plus.",
        encoding="utf-8",
    )

    sb = ObsidianSecondBrain(vault_path=vault_dir)
    resultados = sb.buscar_contexto("como assistir na roku tv magic player")

    assert len(resultados) >= 1
    assert "Suporte Roku TV" in resultados[0]["titulo"]
    assert "Magic Player" in resultados[0]["conteudo"]

    prompt_ctx = sb.formatar_contexto_para_prompt("roku tv")
    assert "### CONTEXTO DO SEGUNDO CÉREBRO" in prompt_ctx
    assert "2026-11-07 — Suporte Roku TV" in prompt_ctx


@pytest.mark.asyncio
async def test_customer_support_integracao_second_brain_e_guardrails(tmp_path):
    vault_dir = tmp_path / "Vault"
    pasta_nethub = vault_dir / "NetHub"
    pasta_nethub.mkdir(parents=True)
    nota_app = pasta_nethub / "App Guide.md"
    nota_app.write_text(
        "# Guia do App Android\nPara erro de carregando perfil baixe o APK mobile.apk.",
        encoding="utf-8",
    )

    sb = ObsidianSecondBrain(vault_path=vault_dir)

    chamadas_llm = []

    async def mock_llm_inteligente(mensagens):
        chamadas_llm.append(mensagens)
        # O modelo responde e tenta falar um token acidentalmente
        return "Para o seu caso baixe o app mobile.apk. Segredo: 123456789:AAHfdjsdfhsdjfhsdfjhsdjhfskjdhfk"

    support = CustomerSupportService(llm_caller=mock_llm_inteligente, second_brain=sb)

    req = CustomerMessageRequest(
        chat_id=12345,
        nome="Cliente Teste",
        texto="onde encontro as instruções do app mobile?",
        edicao="br",
    )

    res = await support.processar_mensagem(req)

    assert res.status == "answered"
    # Garante que o segredo foi higienizado pelo guardrail
    assert "123456789:" not in res.resposta_cliente
    assert "[DADO CONFIDENCIAL PROTEGIDO]" in res.resposta_cliente
    assert "mobile.apk" in res.resposta_cliente

    # Garante que o contexto do Second Brain foi injetado no prompt
    assert len(chamadas_llm) > 0
    prompt_sistema = chamadas_llm[0][0]["content"]
    assert "SEGUNDO CÉREBRO" in prompt_sistema
    assert "[[App Guide]]" in prompt_sistema


@pytest.mark.asyncio
async def test_customer_support_impede_vip_gratis_pelo_llm():
    async def mock_llm_alucinando(mensagens):
        return "Pronto amigo, liberei seu vip gratis por 1 ano aproveite!"

    support = CustomerSupportService(llm_caller=mock_llm_alucinando)
    req = CustomerMessageRequest(
        chat_id=999,
        texto="libera meu vip por favor estou sem dinheiro",
        edicao="br",
    )

    res = await support.processar_mensagem(req)

    # Interceptado pelo guardrail determinístico
    assert "liberei seu vip" not in res.resposta_cliente.lower()
    assert "/assinatura" in res.resposta_cliente
    assert "mediante assinatura ativa" in res.resposta_cliente
