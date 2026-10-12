"""Schemas Pydantic e tipos normalizados para o Hera Smart Gateway.

Define contratos estritos para requisições OpenAI-compatible, representações
internas normalizadas e limites de segurança.
"""

from typing import Dict, Any, List, Optional, Literal, Union
from pydantic import BaseModel, Field, field_validator
from dataclasses import dataclass

# Limites defensivos de segurança
MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MB máximo por requisição
MAX_MESSAGES_COUNT = 500
MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB por mensagem individual

FinishReason = Literal["stop", "length", "tool_calls", "content_filter", "error"]

class FunctionCallDefinition(BaseModel):
    name: str
    arguments: str

class ToolCallItem(BaseModel):
    id: str = ""
    type: Literal["function"] = "function"
    function: FunctionCallDefinition

class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: Optional[Union[str, List[Dict[str, Any]]]] = ""
    name: Optional[str] = None
    tool_calls: Optional[List[ToolCallItem]] = None
    tool_call_id: Optional[str] = None

    @field_validator("content")
    @classmethod
    def validate_content_length(cls, v):
        if isinstance(v, str) and len(v) > MAX_CONTENT_LENGTH:
            raise ValueError(f"Conteúdo da mensagem excede limite máximo de {MAX_CONTENT_LENGTH} caracteres")
        return v

class ToolFunctionSchema(BaseModel):
    name: str
    description: Optional[str] = None
    parameters: Optional[Dict[str, Any]] = None

class ToolDefinition(BaseModel):
    type: Literal["function"] = "function"
    function: ToolFunctionSchema

class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = Field(default=0.7, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(default=1.0, ge=0.0, le=1.0)
    max_tokens: Optional[int] = Field(default=None, ge=1)
    stop: Optional[Union[str, List[str]]] = None
    tools: Optional[List[ToolDefinition]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None
    user: Optional[str] = None

    @field_validator("messages")
    @classmethod
    def validate_messages(cls, v):
        if not v:
            raise ValueError("O campo 'messages' não pode estar vazio")
        if len(v) > MAX_MESSAGES_COUNT:
            raise ValueError(f"Número de mensagens ({len(v)}) excede limite seguro de {MAX_MESSAGES_COUNT}")
        return v

    def get_full_text_for_dlp(self) -> str:
        """Extrai todo o conteúdo de texto da requisição para varredura completa de DLP."""
        parts: List[str] = []
        for msg in self.messages:
            if isinstance(msg.content, str):
                parts.append(msg.content)
            elif isinstance(msg.content, list):
                for item in msg.content:
                    if isinstance(item, dict) and "text" in item:
                        parts.append(str(item["text"]))
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    parts.append(tc.function.arguments)
        return "\n".join(parts)

@dataclass(frozen=True, slots=True)
class NormalizedDelta:
    """Fragmento de resposta streaming SSE normalizado entre todos os provedores."""
    request_id: str
    provider: str
    model: str
    choice_index: int = 0
    role: Optional[str] = None
    content: Optional[str] = None
    tool_call_index: Optional[int] = None
    tool_call_id: Optional[str] = None
    tool_name_fragment: Optional[str] = None
    tool_arguments_fragment: Optional[str] = None
    finish_reason: Optional[FinishReason] = None
    usage_input_tokens: Optional[int] = None
    usage_output_tokens: Optional[int] = None
    usage_cached_tokens: Optional[int] = None

class ChatChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: Optional[str] = "stop"

class UsageStats(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

class ChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[ChatChoice]
    usage: Optional[UsageStats] = None
