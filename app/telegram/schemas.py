from __future__ import annotations

from typing import Any, List, Optional
from pydantic import BaseModel, Field


class CustomerMessageRequest(BaseModel):
    chat_id: int
    texto: str = Field(default="", max_length=5000)
    nome: str = Field(default="", max_length=150)
    username: str = Field(default="", max_length=100)
    edicao: str = Field(default="br", max_length=10)
    anexo: bool = False
    historico: List[Any] = Field(default_factory=list)
    # Resumo da conta do cliente montado pelo NetHub (04/11/2026), SEM dado pessoal: plano, ativo, valido_ate,
    # tem_conta_site e pedidos [{titulo, status}]. Vazio quando o NetHub não conseguiu ler a conta.
    contexto: dict[str, Any] = Field(default_factory=dict)


class CustomerMessageResponse(BaseModel):
    status: str  # "answered", "bug_investigating", "escalated", "ignored"
    resposta_cliente: str
    motivo: str  # "duvida", "bug", "pagamento", "anexo", "cumprimento", "fechamento", "nao_sei"
    task_id: Optional[str] = None
    detalhes: Optional[str] = None


class SystemErrorAlert(BaseModel):
    servico: str = Field(default="desconhecido", max_length=100)
    edicao: str = Field(default="br", max_length=10)
    nivel: str = Field(default="ERROR", max_length=20)
    arquivo: Optional[str] = None
    linha: Optional[int] = None
    logger: Optional[str] = None
    mensagem: str = Field(default="", max_length=10000)
    traceback: Optional[str] = None
    timestamp: Optional[str] = None


class AutoHealResponse(BaseModel):
    status: str  # "started", "ignored_transient", "deduplicated", "failed"
    task_id: Optional[str] = None
    mensagem: str


class TelegramStatusResponse(BaseModel):
    status: str
    active_tasks: int
    recent_fixes: List[dict[str, Any]] = Field(default_factory=list)
