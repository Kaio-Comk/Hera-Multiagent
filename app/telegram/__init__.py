from __future__ import annotations

from app.telegram.autofix_service import AutoFixService
from app.telegram.customer_support import CustomerSupportService
from app.telegram.facts_loader import get_support_facts
from app.telegram.guardrails import aplicar_guardrails, sanitizar_segredos, verificar_tentativa_liberar_plano
from app.telegram.notifier import enviar_telegram_kaio, limpar_estilo, notificar_cliente
from app.telegram.routes import init_telegram_services, router as telegram_router
from app.telegram.second_brain import ObsidianSecondBrain
from app.telegram.schemas import (
    AutoHealResponse,
    CustomerMessageRequest,
    CustomerMessageResponse,
    SystemErrorAlert,
    TelegramStatusResponse,
)

__all__ = [
    "AutoFixService",
    "CustomerSupportService",
    "ObsidianSecondBrain",
    "aplicar_guardrails",
    "sanitizar_segredos",
    "verificar_tentativa_liberar_plano",
    "get_support_facts",
    "enviar_telegram_kaio",
    "limpar_estilo",
    "notificar_cliente",
    "init_telegram_services",
    "telegram_router",
    "CustomerMessageRequest",
    "CustomerMessageResponse",
    "SystemErrorAlert",
    "AutoHealResponse",
    "TelegramStatusResponse",
]
