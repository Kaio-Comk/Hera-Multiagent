from __future__ import annotations

import logging
import re
from typing import Tuple

log = logging.getLogger("hera.telegram.guardrails")

# ─────────────────────────────────────────────────────────────────────────────
# GUARDRAIL 1: ANTI-PLANO GRÁTIS / ANTI-VIP GRÁTIS
# NUNCA liberar plano gratuito, vip gratis, estender dias ou conceder acesso
# ─────────────────────────────────────────────────────────────────────────────
_PADRAO_CONCESSAO_VIP = re.compile(
    r"\b("
    r"(liberei|ativei|concedi|adicionei|coloquei|dei|renovei)\s+(seu\s+|o\s+)?(?:\d+\s+)?(vip|plano|acesso|premium|dias?)"
    r"|(seu\s+)?(vip|plano|acesso|premium)\s+(foi\s+)?(ativado|liberado|renovado)\s+(gratis|gratuito|sem\s+custo)?"
    r"|libera[rç][aã]o\s+gratuita\s+(do\s+)?(vip|plano)"
    r"|dias\s+gr[aá]tis\s+(adicionados|liberados|concedidos)"
    r"|te\s+dei\s+\d+\s+dias"
    r"|adicionei\s+\d+\s+dias"
    r")\b",
    re.IGNORECASE,
)

RESPOSTA_SEGURA_ASSINATURA = {
    "pt": (
        "O acesso ao NetHub é disponibilizado mediante assinatura ativa. "
        "Você pode assinar ou renovar diretamente pelo bot oficial @NetHub_Filme_Bot com o comando /assinatura "
        "ou tocando em Assinar no menu para gerar o código Pix."
    ),
    "es": (
        "El acceso a NetHub está disponible mediante suscripción activa. "
        "Puedes suscribirte o renovar directamente en el bot oficial @NetHub_Peliculas_Bot con el comando /suscripcion "
        "o tocando en Suscribirse en el menú para ver los planes."
    ),
}


def verificar_tentativa_liberar_plano(texto: str, idioma: str = "pt") -> Tuple[bool, str]:
    """Valida se o texto contém alegação indevida de liberação ou concessão de VIP/plano grátis.

    Retorna (violou: bool, texto_tratado: str).
    """
    if not texto:
        return False, texto

    if _PADRAO_CONCESSAO_VIP.search(texto):
        log.warning("[guardrails] Tentativa de liberação indevida de VIP interceptada: %r", texto[:100])
        subst = RESPOSTA_SEGURA_ASSINATURA.get(idioma, RESPOSTA_SEGURA_ASSINATURA["pt"])
        return True, subst

    return False, texto


# ─────────────────────────────────────────────────────────────────────────────
# GUARDRAIL 3: ANTI-VAZAMENTO DE SEGREDOS / DADOS CONFIDENCIAIS
# Redação estrita de tokens, credenciais, senhas e chaves internas
# ─────────────────────────────────────────────────────────────────────────────
_PADROES_SEGREDOS = [
    # Telegram Bot Token (ex.: 123456789:AAHfdjsf...)
    re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{28,50}\b"),
    # JWT / Supabase Anon / Service Role Key
    re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    # Stripe Secret Keys
    re.compile(r"\b[rs]k_(live|test)_[A-Za-z0-9]{20,}\b"),
    # PostgreSQL / Database URLs
    re.compile(r"postgres(?:ql)?:\/\/[^\s\"'>]+", re.IGNORECASE),
    # Chaves de API genéricas longas com label
    re.compile(r'(?:api[_-]?key|token|secret|password|senha)\s*[:=]\s*["\']?([A-Za-z0-9_\-\.]{16,})["\']?', re.IGNORECASE),
    # Telethon Session String
    re.compile(r"\b1[A-Za-z0-9+/=]{120,}\b"),
]


def sanitizar_segredos(texto: str) -> str:
    """Higieniza o texto removendo qualquer vazamento acidental de tokens, chaves ou senhas."""
    if not texto:
        return ""

    limpo = texto
    for padrao in _PADROES_SEGREDOS:
        limpo = padrao.sub("[DADO CONFIDENCIAL PROTEGIDO]", limpo)

    return limpo


# ─────────────────────────────────────────────────────────────────────────────
# GUARDRAIL 4: VALIDAÇÃO DE SEGURANÇA GERAL DO CONTEÚDO
# ─────────────────────────────────────────────────────────────────────────────
_PADRAO_DESTRUTIVO = re.compile(
    r"\b(drop\s+table|delete\s+from\s+usuarios|rm\s+-rf|format\s+[a-z]:)\b",
    re.IGNORECASE,
)


def validar_seguranca_geral(texto: str) -> Tuple[bool, str]:
    """Garante que a resposta não contenha comandos destrutivos ou violações de integridade."""
    if not texto:
        return True, ""

    if _PADRAO_DESTRUTIVO.search(texto):
        log.error("[guardrails] Comando destrutivo detectado no texto de resposta: %r", texto[:100])
        return False, "Operação não permitida por diretrizes de segurança do NetHub."

    return True, texto


def aplicar_guardrails(texto: str, idioma: str = "pt") -> str:
    """Executa a cadeia completa de guardrails de segurança sobre o texto."""
    if not texto:
        return ""

    # 1. Sanitizar qualquer segredo vazado
    texto_seguro = sanitizar_segredos(texto)

    # 2. Verificar e barrar concessão de VIP gratuito
    violou_vip, texto_seguro = verificar_tentativa_liberar_plano(texto_seguro, idioma)

    # 3. Validar contra comandos destrutivos
    ok_destr, texto_seguro = validar_seguranca_geral(texto_seguro)

    return texto_seguro
