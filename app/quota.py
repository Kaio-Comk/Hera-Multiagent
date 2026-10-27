"""Limite de uso das IAs por assinatura e APIs cloud: detectar, prever, lembrar e desviar.

Combina reconhecimento de erros, parse de headers de rate-limit (Anthropic, OpenAI, DeepSeek),
persistencia opcional de estado em disco e calculo preditivo de saude (healthy, degraded, survival)
para comutacao de playbooks (Anthropic CMA).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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


def parse_ratelimit_headers(headers: Dict[str, Any]) -> Dict[str, Any]:
    """Extrai metricas oficiais de rate-limit de headers HTTP padrao ou de provedores (Anthropic, OpenAI)."""
    norm = {str(k).lower(): str(v) for k, v in headers.items()}
    res = {
        "remaining_requests": None,
        "limit_requests": None,
        "remaining_tokens": None,
        "limit_tokens": None,
        "reset_seconds": None,
    }

    # Anthropic / OpenAI headers
    for req_key in ("anthropic-ratelimit-requests-remaining", "x-ratelimit-remaining-requests"):
        if req_key in norm:
            try:
                res["remaining_requests"] = int(norm[req_key])
            except ValueError:
                pass

    for req_lim in ("anthropic-ratelimit-requests-limit", "x-ratelimit-limit-requests"):
        if req_lim in norm:
            try:
                res["limit_requests"] = int(norm[req_lim])
            except ValueError:
                pass

    for tok_key in ("anthropic-ratelimit-tokens-remaining", "x-ratelimit-remaining-tokens"):
        if tok_key in norm:
            try:
                res["remaining_tokens"] = int(norm[tok_key])
            except ValueError:
                pass

    for tok_lim in ("anthropic-ratelimit-tokens-limit", "x-ratelimit-limit-tokens"):
        if tok_lim in norm:
            try:
                res["limit_tokens"] = int(norm[tok_lim])
            except ValueError:
                pass

    # Retry-After header
    if "retry-after" in norm:
        try:
            res["reset_seconds"] = int(norm["retry-after"])
        except ValueError:
            pass

    return res


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
    def __init__(self, persistence_file: Optional[str] = None) -> None:
        self._until: Dict[str, float] = {}
        self._why: Dict[str, str] = {}
        self._metrics: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._persistence_file = persistence_file
        if self._persistence_file:
            self._load_state()

    def _load_state(self) -> None:
        """Carrega estado persistido em disco para nao perder cooldown em restarts."""
        if not self._persistence_file:
            return
        try:
            p = Path(self._persistence_file)
            if p.is_file():
                data = json.loads(p.read_text(encoding="utf-8"))
                now = time.time()
                for engine, info in data.items():
                    until = float(info.get("until", 0.0))
                    if until > now:
                        self._until[engine] = until
                        self._why[engine] = info.get("reason", "")
        except Exception:
            pass

    def _save_state(self) -> None:
        """Persiste estado de quotas ativas em disco de forma atomica."""
        if not self._persistence_file:
            return
        try:
            p = Path(self._persistence_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            now = time.time()
            data = {
                e: {"until": u, "reason": self._why.get(e, "")}
                for e, u in self._until.items() if u > now
            }
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass

    def record_metrics(self, engine: str, headers: Dict[str, Any]) -> None:
        """Registra metricas oficiais extraidas de headers de resposta da API."""
        parsed = parse_ratelimit_headers(headers)
        with self._lock:
            if engine not in self._metrics:
                self._metrics[engine] = {}
            for k, v in parsed.items():
                if v is not None:
                    self._metrics[engine][k] = v
            self._metrics[engine]["updated_at"] = time.time()

    def get_health_status(self, engine: str) -> str:
        """Determina o estado preditivo de quota de um motor: 'healthy', 'degraded' ou 'survival'."""
        with self._lock:
            until = self._until.get(engine)
            if until is not None and until > time.time():
                return "survival"

            m = self._metrics.get(engine, {})
            rem_req = m.get("remaining_requests")
            lim_req = m.get("limit_requests")
            rem_tok = m.get("remaining_tokens")
            lim_tok = m.get("limit_tokens")

            ratios = []
            if rem_req is not None and lim_req and lim_req > 0:
                ratios.append(rem_req / lim_req)
            if rem_tok is not None and lim_tok and lim_tok > 0:
                ratios.append(rem_tok / lim_tok)

            if ratios:
                min_ratio = min(ratios)
                if min_ratio < 0.30:
                    return "survival"
                elif min_ratio < 0.60:
                    return "degraded"
                return "healthy"

            return "healthy"

    def mark(self, engine: str, seconds: int, reason: str = "") -> None:
        with self._lock:
            self._until[engine] = time.time() + max(MIN_COOLDOWN, min(MAX_COOLDOWN, int(seconds)))
            self._why[engine] = (reason or "")[:200]
            self._save_state()

    def clear(self, engine: str) -> None:
        with self._lock:
            self._until.pop(engine, None)
            self._why.pop(engine, None)
            self._save_state()

    def blocked(self, engine: str) -> bool:
        with self._lock:
            until = self._until.get(engine)
            if until is None:
                return False
            if until <= time.time():
                self._until.pop(engine, None)
                self._why.pop(engine, None)
                self._save_state()
                return False
            return True

    def snapshot(self) -> Dict[str, Dict[str, object]]:
        """engine -> {until (epoch), minutes (quanto falta), reason, health}, só dos motores ainda esgotados."""
        now = time.time()
        with self._lock:
            return {
                e: {
                    "until": u,
                    "minutes": max(1, int((u - now) // 60)),
                    "reason": self._why.get(e, ""),
                    "health": "survival"
                }
                for e, u in self._until.items() if u > now
            }
