"""Chaves de acesso para FERRAMENTAS EXTERNAS usarem o Roteador (Cursor, Cline, scripts...).

Por que existem: o token mestre do Hera dá acesso a TUDO (config, tarefas, aprovações). Uma ferramenta que só precisa
de modelos recebe uma chave própria que:
  * só funciona em `/v1/chat/completions` e `/v1/models` (nunca nas rotas de gestão);
  * pode ser limitada a certos modelos/combos, a N requisições por minuto e a N tokens por dia;
  * pode ser desligada ou apagada sozinha, sem trocar o token mestre;
  * é mostrada UMA vez na criação; o Hera guarda só o hash (SHA-256 com sal) e um prefixo para você reconhecê-la.
"""
from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional

KEY_PREFIX = "hk-"
MAX_KEYS = 50
CLIENT_PATHS = frozenset({"/v1/chat/completions", "/v1/models"})


@dataclass
class ClientKey:
    id: str
    name: str
    prefix: str
    enabled: bool = True
    models: Optional[List[str]] = None            # padrões fnmatch (ex.: "groq/*", "rapido"); vazio/None = todos
    rpm: Optional[int] = None
    daily_tokens: Optional[int] = None
    created: float = 0.0
    last_used: Optional[float] = None

    def allows(self, model: str) -> bool:
        return not self.models or any(fnmatch.fnmatchcase(model, p) for p in self.models)

    def public(self) -> Dict[str, Any]:
        return {"id": self.id, "name": self.name, "prefix": self.prefix, "enabled": self.enabled,
                "models": self.models or [], "rpm": self.rpm, "daily_tokens": self.daily_tokens,
                "created": self.created, "last_used": self.last_used}


class ClientKeyStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._hits: Dict[str, Deque[float]] = defaultdict(deque)

    # ------------------------------------------------------------------ disco
    def _read(self) -> List[Dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _write(self, rows: List[Dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(rows), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    @staticmethod
    def _hash(key: str, salt: str) -> str:
        return hashlib.sha256((salt + key).encode("utf-8")).hexdigest()

    @staticmethod
    def _to_key(row: Dict[str, Any]) -> ClientKey:
        return ClientKey(id=row["id"], name=row.get("name", ""), prefix=row.get("prefix", ""), enabled=bool(row.get("enabled", True)),
                         models=row.get("models") or None, rpm=row.get("rpm"), daily_tokens=row.get("daily_tokens"),
                         created=row.get("created", 0.0), last_used=row.get("last_used"))

    # ------------------------------------------------------------------ gestão
    def list(self) -> List[ClientKey]:
        with self._lock:
            return [self._to_key(r) for r in self._read()]

    def create(self, name: str, models: Optional[List[str]] = None, rpm: Optional[int] = None,
               daily_tokens: Optional[int] = None) -> Dict[str, Any]:
        """Devolve `{"key": <segredo, só agora>, **dados públicos}`."""
        with self._lock:
            rows = self._read()
            if len(rows) >= MAX_KEYS:
                raise ValueError(f"No máximo {MAX_KEYS} chaves.")
            key = KEY_PREFIX + secrets.token_urlsafe(30)
            salt = secrets.token_hex(8)
            row = {"id": uuid.uuid4().hex[:10], "name": name, "prefix": key[:9], "salt": salt, "hash": self._hash(key, salt),
                   "enabled": True, "models": models or [], "rpm": rpm, "daily_tokens": daily_tokens,
                   "created": time.time(), "last_used": None}
            rows.append(row)
            self._write(rows)
        return {"key": key, **self._to_key(row).public()}

    def update(self, key_id: str, **changes: Any) -> Optional[ClientKey]:
        allowed = {"name", "enabled", "models", "rpm", "daily_tokens"}
        with self._lock:
            rows = self._read()
            for row in rows:
                if row.get("id") == key_id:
                    row.update({k: v for k, v in changes.items() if k in allowed})
                    self._write(rows)
                    return self._to_key(row)
        return None

    def delete(self, key_id: str) -> bool:
        with self._lock:
            rows = self._read()
            kept = [r for r in rows if r.get("id") != key_id]
            if len(kept) == len(rows):
                return False
            self._write(kept)
            return True

    # ------------------------------------------------------------------ uso
    def authenticate(self, supplied: Optional[str]) -> Optional[ClientKey]:
        """Confere a chave em tempo constante (varre todas, sem parar na primeira)."""
        if not supplied or not supplied.startswith(KEY_PREFIX) or len(supplied) > 200:
            return None
        found: Optional[Dict[str, Any]] = None
        with self._lock:
            rows = self._read()
            for row in rows:
                if hmac.compare_digest(self._hash(supplied, row.get("salt", "")), row.get("hash", "")):
                    found = row
            if found is not None and found.get("enabled", True):
                now = time.time()
                if not found.get("last_used") or now - found["last_used"] > 30:      # nao grava o arquivo a cada chamada
                    found["last_used"] = now
                    self._write(rows)
        return self._to_key(found) if found is not None and found.get("enabled", True) else None

    def over_rpm(self, key: ClientKey) -> Optional[int]:
        """Segundos até poder tentar de novo, se estourou o limite por minuto (conta a tentativa se couber)."""
        if not key.rpm:
            return None
        now = time.monotonic()
        dq = self._hits[key.id]
        while dq and dq[0] <= now - 60:
            dq.popleft()
        if len(dq) >= key.rpm:
            return max(1, int(60 - (now - dq[0])))
        dq.append(now)
        return None
