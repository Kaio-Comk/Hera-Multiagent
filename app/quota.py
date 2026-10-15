"""Limite de uso das IAs por assinatura (Claude Pro, Antigravity...): detectar, lembrar e desviar.

Os motores por assinatura não têm consulta de saldo: o limite só aparece quando o passo já falhou ("usage limit reached",
"resets 3pm", "RESOURCE_EXHAUSTED"...). Então o Hera reconhece essa falha, marca o motor como ESGOTADO por um tempo e o
roteador/planejador passam a evitá-lo; o passo que falhou é refeito em outro motor.
"""
from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Optional

DEFAULT_COOLDOWN = 30 * 60          # sem horário de volta na mensagem: tenta de novo em 30 min
MIN_COOLDOWN = 5 * 60
MAX_COOLDOWN = 6 * 3600

_QUOTA = re.compile(
    r"usage limit|limit reached|hit your (?:usage |rate )?limit|you(?:'|’)ve hit|rate[ _-]?limit|quota|resource[_ ]exhausted|"
    r"too many requests|\b429\b|credit balance|out of credits|exceeded your (?:current )?(?:quota|usage|limit)|plan limit|"
    r"5-hour limit|weekly limit", re.IGNORECASE)
_RESETS_AT = re.compile(r"reset[s]?\s*(?:at|em|às)?\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", re.IGNORECASE)
_RESETS_IN = re.compile(r"(?:reset[s]?|try again|volte|retry)\s*(?:in|em|after)?\s*(?:about\s*)?(\d+)\s*(second|minute|hour|min|h|s)", re.IGNORECASE)


def is_quota_error(text: str) -> bool:
    return bool(text) and bool(_QUOTA.search(text))


def seconds_until_reset(text: str, now: Optional[datetime] = None) -> int:
    """Quanto falta para o limite voltar, segundo a mensagem ("resets 3pm", "try again in 2 hours"); padrão 30 min."""
    now = now or datetime.now()
    m = _RESETS_IN.search(text or "")
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        secs = n * (3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1)
        return max(MIN_COOLDOWN, min(MAX_COOLDOWN, secs))
    m = _RESETS_AT.search(text or "")
    if m:
        hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
        if ampm == "pm" and hour < 12:
            hour += 12
        if ampm == "am" and hour == 12:
            hour = 0
        if 0 <= hour < 24:
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return max(MIN_COOLDOWN, min(MAX_COOLDOWN, int((target - now).total_seconds())))
    return DEFAULT_COOLDOWN


class QuotaTracker:
    def __init__(self) -> None:
        self._until: Dict[str, float] = {}
        self._why: Dict[str, str] = {}
        self._lock = threading.Lock()

    def mark(self, engine: str, seconds: int, reason: str = "") -> None:
        with self._lock:
            self._until[engine] = time.time() + max(MIN_COOLDOWN, min(MAX_COOLDOWN, int(seconds)))
            self._why[engine] = (reason or "")[:200]

    def clear(self, engine: str) -> None:
        with self._lock:
            self._until.pop(engine, None)
            self._why.pop(engine, None)

    def blocked(self, engine: str) -> bool:
        with self._lock:
            until = self._until.get(engine)
            if until is None:
                return False
            if until <= time.time():
                self._until.pop(engine, None)
                self._why.pop(engine, None)
                return False
            return True

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        """engine -> {until (epoch), minutes (quanto falta), reason}, só dos motores ainda esgotados."""
        now = time.time()
        with self._lock:
            return {e: {"until": u, "minutes": max(1, int((u - now) // 60)), "reason": self._why.get(e, "")}
                    for e, u in self._until.items() if u > now}
