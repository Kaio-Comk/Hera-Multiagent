"""Código de aprovação HUMANA (fase 2 do plano de correções, 12/10/2026).

PROBLEMA: o HITL era um circuito fechado dentro da própria API: o servidor emitia o nonce, o mesmo
servidor assinava o token e o `user_id` vinha do cliente. Quem tivesse o token da API (ou uma
página/extensão que leia a UI) aprovava merges SOZINHO. `confirmed_hitl` do MCP era um booleano
que o próprio cliente enviava.

CORREÇÃO: aprovar exige um código curto que o Hera mostra SÓ no console do servidor (como o prompt
do `sudo`) ou via `python main.py approval-code`. A UI pede ao humano que o digite. O código muda a
cada 5 minutos, cinco erros seguidos bloqueiam novas tentativas por 5 minutos, e a comparação é em
tempo constante.

MODELO DE AMEAÇA (honesto): protege contra quem tem o token da API mas NÃO enxerga o console nem os
arquivos do usuário (site malicioso, extensão, script injetado na UI). Quem já lê os arquivos da
sua conta (malware local) lê também o segredo: isso está fora do escopo.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path
from typing import Callable, Optional


class HumanCodeLockedError(Exception):
    """Muitas tentativas erradas: novas tentativas ficam bloqueadas por `retry_after` segundos."""

    def __init__(self, retry_after: int):
        super().__init__(f"Muitas tentativas inválidas; tente de novo em {retry_after}s.")
        self.retry_after = retry_after


def load_or_create_secret(path: Path) -> bytes:
    """Segredo persistente (32 bytes) em `path`, criado na primeira execução."""
    path = Path(path)
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing.encode("utf-8")
    except OSError:
        pass
    value = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return value.encode("utf-8")


class HumanApprovalCode:
    def __init__(self, secret: bytes, *, period: int = 300, digits: int = 8,
                 max_failures: int = 5, lockout_seconds: int = 300,
                 clock: Callable[[], float] = time.time):
        if len(secret) < 16:
            raise ValueError("segredo curto demais")
        self._secret = secret
        self.period = period
        self.digits = digits
        self.max_failures = max_failures
        self.lockout_seconds = lockout_seconds
        self._clock = clock
        self._failures = 0
        self._locked_until = 0.0

    # ── geração ────────────────────────────────────────────────────────────────
    def _code_for(self, counter: int) -> str:
        mac = hmac.new(self._secret, f"hera-human-approval|{counter}".encode("utf-8"), hashlib.sha256).digest()
        return f"{int.from_bytes(mac[:8], 'big') % (10 ** self.digits):0{self.digits}d}"

    def current(self) -> str:
        return self._code_for(int(self._clock() // self.period))

    def seconds_left(self) -> int:
        return int(self.period - (self._clock() % self.period))

    # ── verificação ────────────────────────────────────────────────────────────
    @staticmethod
    def _normalize(value: Optional[str]) -> str:
        return "".join(ch for ch in (value or "") if ch.isdigit())

    def verify(self, supplied: Optional[str]) -> bool:
        """True se o código é o da janela atual ou da anterior (tolera a virada). Levanta
        `HumanCodeLockedError` se estiver bloqueado. Errar `max_failures` vezes bloqueia."""
        now = self._clock()
        if now < self._locked_until:
            raise HumanCodeLockedError(max(1, int(self._locked_until - now)))
        counter = int(now // self.period)
        given = self._normalize(supplied)
        valid = [self._code_for(counter), self._code_for(counter - 1)]
        ok = bool(given) and any(hmac.compare_digest(given, v) for v in valid)
        if ok:
            self._failures = 0
            return True
        self._failures += 1
        if self._failures >= self.max_failures:
            self._locked_until = now + self.lockout_seconds
            self._failures = 0
        return False
