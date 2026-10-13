"""Proteção dos provedores: disjuntor (circuit breaker), limite por minuto e limites diários.

  * Disjuntor: depois de `threshold` falhas SEGUIDAS (5xx, timeout, rede) o provedor entra em pausa por um tempo que
    dobra a cada nova pausa (30 s, 60 s, 120 s... até 10 min). Um sucesso zera tudo. Se TODOS os candidatos estiverem
    em pausa o roteador tenta mesmo assim (melhor tentar do que falhar sem tentar).
  * Limites (opcionais, por provedor): requisições por minuto (RPM), requisições por dia e tokens por dia. Passou do
    limite, o provedor é pulado como se estivesse indisponível, e o fallback segue.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, Optional

BASE_PAUSE = 30.0
MAX_PAUSE = 600.0


@dataclass
class _State:
    failures: int = 0
    pauses: int = 0
    paused_until: float = 0.0
    last_error: str = ""


class ProviderGuard:
    def __init__(self, threshold: int = 3, clock: Callable[[], float] = time.monotonic):
        self.threshold = max(1, threshold)
        self._clock = clock
        self._state: Dict[str, _State] = defaultdict(_State)
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)
        self.limits: Dict[str, Dict[str, Optional[int]]] = {}       # nome -> {"rpm":, "daily_requests":, "daily_tokens":}

    # ------------------------------------------------------------------ disjuntor
    def is_paused(self, name: str) -> bool:
        return self._state[name].paused_until > self._clock()

    def resume_in(self, name: str) -> int:
        return max(0, int(self._state[name].paused_until - self._clock()))

    def report_success(self, name: str) -> None:
        st = self._state[name]
        st.failures, st.pauses, st.paused_until, st.last_error = 0, 0, 0.0, ""

    def report_failure(self, name: str, error: str = "", counts: bool = True) -> None:
        """`counts=False` para erros do pedido (4xx que não são 429): não é culpa do provedor."""
        st = self._state[name]
        st.last_error = (error or "")[:160]
        if not counts:
            return
        st.failures += 1
        if st.failures >= self.threshold:
            st.pauses += 1
            st.paused_until = self._clock() + min(MAX_PAUSE, BASE_PAUSE * (2 ** (st.pauses - 1)))
            st.failures = 0

    def reset(self, name: str) -> None:
        self._state.pop(name, None)

    def status(self, name: str) -> Dict[str, Any]:
        st = self._state[name]
        return {"state": "paused" if self.is_paused(name) else "ok", "resume_in_s": self.resume_in(name),
                "consecutive_failures": st.failures, "last_error": st.last_error}

    # ------------------------------------------------------------------ limites
    def set_limits(self, limits: Dict[str, Dict[str, Optional[int]]]) -> None:
        self.limits = limits

    def over_limit(self, name: str, today: Callable[[str], Dict[str, int]]) -> Optional[str]:
        """Motivo (texto) se o provedor estourou algum limite; None se pode usar. Só conta o RPM se couber."""
        lim = self.limits.get(name) or {}
        if lim.get("daily_requests") or lim.get("daily_tokens"):
            used = today(name)
            if lim.get("daily_requests") and used["requests"] >= lim["daily_requests"]:
                return f"limite de {lim['daily_requests']} requisições por dia"
            if lim.get("daily_tokens") and used["tokens"] >= lim["daily_tokens"]:
                return f"limite de {lim['daily_tokens']} tokens por dia"
        if lim.get("rpm"):
            now = self._clock()
            dq = self._hits[name]
            while dq and dq[0] <= now - 60:
                dq.popleft()
            if len(dq) >= lim["rpm"]:
                return f"limite de {lim['rpm']} requisições por minuto"
            dq.append(now)
        return None
