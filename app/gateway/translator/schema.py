"""Constantes de formato (dados puros). Espelha `open-sse/translator/schema`."""

ROLE_USER, ROLE_ASSISTANT, ROLE_TOOL, ROLE_SYSTEM = "user", "assistant", "tool", "system"

FINISH_STOP, FINISH_LENGTH, FINISH_TOOL_CALLS, FINISH_CONTENT_FILTER = "stop", "length", "tool_calls", "content_filter"

CLAUDE_END_TURN, CLAUDE_MAX_TOKENS, CLAUDE_TOOL_USE, CLAUDE_STOP_SEQUENCE, CLAUDE_REFUSAL = (
    "end_turn", "max_tokens", "tool_use", "stop_sequence", "refusal")

CLAUDE_TOOL_CHOICE_TYPES = frozenset({"auto", "any", "tool", "none"})

DEFAULT_MAX_TOKENS = 64000
DEFAULT_MIN_TOKENS = 32000   # com ferramentas, evita argumentos truncados
