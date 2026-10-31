from __future__ import annotations

import logging
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Request

from app.telegram.autofix_service import AutoFixService
from app.telegram.customer_support import CustomerSupportService
from app.telegram.schemas import (
    AutoHealResponse,
    CustomerMessageRequest,
    CustomerMessageResponse,
    SystemErrorAlert,
    TelegramStatusResponse,
)

log = logging.getLogger("hera.telegram.routes")

router = APIRouter(prefix="/api/telegram", tags=["telegram"])

_autofix_service: Optional[AutoFixService] = None
_support_service: Optional[CustomerSupportService] = None


def get_autofix_service() -> AutoFixService:
    global _autofix_service
    if _autofix_service is None:
        _autofix_service = AutoFixService()
    return _autofix_service


def get_support_service() -> CustomerSupportService:
    global _support_service
    if _support_service is None:
        _support_service = CustomerSupportService(autofix_service=get_autofix_service())
    return _support_service


def init_telegram_services(autofix: AutoFixService, support: CustomerSupportService) -> None:
    global _autofix_service, _support_service
    _autofix_service = autofix
    _support_service = support


@router.post("/customer-message", response_model=CustomerMessageResponse)
async def handle_customer_message(
    req: CustomerMessageRequest,
    support: CustomerSupportService = Depends(get_support_service),
) -> CustomerMessageResponse:
    """Recebe mensagem de cliente do Telegram (via ia_suporte.py) e retorna resposta contextual ou aciona auto-fix."""
    try:
        return await support.processar_mensagem(req)
    except Exception as exc:
        log.exception("[telegram_routes] Erro ao processar mensagem do cliente %s: %s", req.chat_id, exc)
        return CustomerMessageResponse(
            status="error",
            resposta_cliente="Vou verificar e já te respondo.",
            motivo="nao_sei",
            detalhes=str(exc),
        )


@router.post("/system-error", response_model=AutoHealResponse)
async def handle_system_error(
    alert: SystemErrorAlert,
    autofix: AutoFixService = Depends(get_autofix_service),
) -> AutoHealResponse:
    """Recebe alerta de erro ou crash de serviço do NetHub (rastreio_erros.py) e dispara auto-cura autônoma imediata."""
    try:
        status, msg = await autofix.agendar_autoheal_sistema(alert)
        return AutoHealResponse(status=status, mensagem=msg)
    except Exception as exc:
        log.exception("[telegram_routes] Erro ao acionar auto-cura de sistema: %s", exc)
        return AutoHealResponse(status="failed", mensagem=f"Falha ao agendar auto-cura: {exc}")


@router.post("/auto-heal", response_model=AutoHealResponse)
async def trigger_autoheal(
    alert: SystemErrorAlert,
    autofix: AutoFixService = Depends(get_autofix_service),
) -> AutoHealResponse:
    """Endpoint direto para acionar auto-cura com payload de SystemErrorAlert."""
    return await handle_system_error(alert, autofix)


@router.get("/status", response_model=TelegramStatusResponse)
async def get_telegram_status(
    autofix: AutoFixService = Depends(get_autofix_service),
) -> TelegramStatusResponse:
    """Retorna estado e métricas da ponte Telegram e auto-fix do Hera."""
    dados = autofix.status()
    return TelegramStatusResponse(
        status=dados.get("status", "online"),
        active_tasks=dados.get("active_tasks", 0),
        recent_fixes=dados.get("recent_fixes", []),
    )
