"""Canal de progresso: os motores "falam" o que estao fazendo enquanto trabalham (console ao vivo do Hera).

Quem executa um passo (o escalonador do plano ou o motor de tarefas) liga o canal para AQUELE passo com `bind(...)`; o
adaptador so chama `say(...)`, sem saber para onde vai. Sem canal ligado, `say` nao faz nada (testes, CLI).
Os eventos usam `contextvars`: cada passo concorrente tem o seu canal, sem se misturar.
"""
from __future__ import annotations

import contextvars
from typing import Any, Callable, Optional

Sink = Callable[..., None]
_SINK: contextvars.ContextVar[Optional[Sink]] = contextvars.ContextVar("hera_progress_sink", default=None)

# tipos de evento que os motores emitem
TEXT, TOOL, TOOL_RESULT, STATUS, THINKING = "text", "tool", "tool_result", "status", "thinking"


def bind(sink: Optional[Sink]):
    """Liga o canal para o contexto atual. Devolve o token para `unbind`."""
    return _SINK.set(sink)


def unbind(token) -> None:
    _SINK.reset(token)


def say(kind: str, text: str = "", **meta: Any) -> None:
    """Emite um evento de progresso do passo em andamento. Nunca levanta (o console nao pode derrubar um motor)."""
    sink = _SINK.get()
    if sink is None:
        return
    try:
        sink(kind, text, **meta)
    except Exception:       # noqa: BLE001 - falha no console nao pode matar a tarefa
        pass


def tool_summary(name: str, args: Any) -> str:
    """Resumo curto e legivel de uma chamada de ferramenta (o que o agente esta fazendo)."""
    if not isinstance(args, dict):
        return f"{name}"
    for key in ("command", "CommandLine", "file_path", "path", "AbsolutePath", "pattern", "query", "url", "description"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return f"{name}: {value.strip()[:300]}"
    return f"{name}: {str(args)[:200]}"
