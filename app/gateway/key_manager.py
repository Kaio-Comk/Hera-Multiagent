"""Gerenciador de chaves de provedores de nuvem (plano mestre v3, seção 2.5).

Regras (do plano):
  * as chaves vêm SÓ de variáveis de ambiente; nunca do config.yaml nem da API, e nunca são logadas;
  * o rodízio é apenas entre credenciais que o PRÓPRIO USUÁRIO configurou (várias chaves
    autorizadas do mesmo provedor), para distribuir carga, e NÃO para driblar limites: uma chave
    que recebeu 429 fica em espera pelo `Retry-After` do provedor e não é usada até lá;
  * chave rejeitada (401/403) é desativada até o próximo reinício;
  * limite diário opcional por chave (contado por dia UTC).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Iterable, List, Mapping, Optional

DEFAULT_COOLDOWN_S = 60.0
MAX_COOLDOWN_S = 3600.0


class NoKeyAvailableError(Exception):
    """Nenhuma chave utilizável agora. `retry_after` (s) é None quando todas estão desativadas."""

    def __init__(self, retry_after: Optional[float]):
        super().__init__("nenhuma chave disponível")
        self.retry_after = retry_after


@dataclass(frozen=True)
class KeyLease:
    key_id: str          # rótulo seguro (ex.: DEEPSEEK_API_KEY#2); NUNCA o segredo
    secret: str


@dataclass
class _KeyState:
    key_id: str
    secret: str
    cooldown_until: float = 0.0
    disabled: bool = False
    used_today: int = 0
    day: str = ""


def parse_retry_after(value: Optional[str], now: Optional[float] = None) -> Optional[float]:
    """`Retry-After` em segundos ou como data HTTP; None se ausente/ilegível."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        base = datetime.fromtimestamp(now if now is not None else time.time(), tz=timezone.utc)
        return max(0.0, (when - base).total_seconds())
    except (TypeError, ValueError):
        return None


def load_keys_from_env(env_names: Iterable[str], environ: Mapping[str, str]) -> List[str]:
    """Lê as variáveis de ambiente; cada uma pode ter várias chaves separadas por vírgula."""
    found: List[str] = []
    for name in env_names:
        for part in (environ.get(name) or "").split(","):
            part = part.strip()
            if part and part not in found:
                found.append(part)
    return found


class KeyManager:
    def __init__(self, secrets: Iterable[str], *, label: str = "key", daily_limit: Optional[int] = None,
                 clock: Callable[[], float] = time.time):
        uniq: List[str] = []
        for s in secrets:
            s = (s or "").strip()
            if s and s not in uniq:
                uniq.append(s)
        self.label = label
        self.daily_limit = daily_limit
        self._clock = clock
        self._keys = [_KeyState(f"{label}#{i + 1}", s) for i, s in enumerate(uniq)]
        self._cursor = 0

    def __len__(self) -> int:
        return len(self._keys)

    # ── uso ────────────────────────────────────────────────────────────────────
    def _today(self, now: float) -> str:
        return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")

    def _roll_day(self, key: _KeyState, now: float) -> None:
        today = self._today(now)
        if key.day != today:
            key.day, key.used_today = today, 0

    def _seconds_to_next_day(self, now: float) -> float:
        return 86400.0 - (now % 86400.0)

    def acquire(self) -> KeyLease:
        """Próxima chave utilizável (rodízio). Levanta `NoKeyAvailableError` se não houver."""
        now = self._clock()
        n = len(self._keys)
        soonest: Optional[float] = None
        any_waiting = False
        for step in range(n):
            key = self._keys[(self._cursor + step) % n]
            self._roll_day(key, now)
            if key.disabled:
                continue
            any_waiting = True
            if key.cooldown_until > now:
                wait = key.cooldown_until - now
                soonest = wait if soonest is None else min(soonest, wait)
                continue
            if self.daily_limit is not None and key.used_today >= self.daily_limit:
                wait = self._seconds_to_next_day(now)
                soonest = wait if soonest is None else min(soonest, wait)
                continue
            self._cursor = (self._cursor + step + 1) % n
            key.used_today += 1
            return KeyLease(key.key_id, key.secret)
        raise NoKeyAvailableError(soonest if any_waiting else None)

    def _find(self, key_id: str) -> Optional[_KeyState]:
        return next((k for k in self._keys if k.key_id == key_id), None)

    def report_rate_limited(self, key_id: str, retry_after: Optional[float]) -> None:
        key = self._find(key_id)
        if key:
            wait = DEFAULT_COOLDOWN_S if retry_after is None else retry_after
            key.cooldown_until = self._clock() + min(max(wait, 1.0), MAX_COOLDOWN_S)

    def report_auth_failure(self, key_id: str) -> None:
        key = self._find(key_id)
        if key:
            key.disabled = True

    # ── observabilidade (sem segredos) ─────────────────────────────────────────
    def status(self) -> List[dict]:
        now = self._clock()
        out = []
        for key in self._keys:
            self._roll_day(key, now)
            if key.disabled:
                state = "disabled"
            elif key.cooldown_until > now:
                state = "cooldown"
            elif self.daily_limit is not None and key.used_today >= self.daily_limit:
                state = "daily_limit"
            else:
                state = "ok"
            out.append({"id": key.key_id, "state": state,
                        "cooldown_remaining_s": max(0, int(key.cooldown_until - now)),
                        "used_today": key.used_today})
        return out
