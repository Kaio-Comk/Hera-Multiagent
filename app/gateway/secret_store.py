"""Cofre local das chaves de API dos provedores do Roteador.

  * No Windows cada lista de chaves é protegida com a DPAPI (CryptProtectData): só o MESMO usuário do Windows,
    neste mesmo PC, consegue abrir o arquivo; copiar `data/` para outro lugar não vaza nada.
  * Fora do Windows (sem DPAPI) o arquivo fica só com permissão 0600 (limite honesto: sem cifra).
  * A API do Hera NUNCA devolve uma chave: só diz QUANTAS existem. Escrever exige o código humano (ver server).
  * O arquivo guarda só o nome do provedor -> blob; nada de log de chave.
"""
from __future__ import annotations

import base64
import ctypes
import json
import os
import sys
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Dict, List

MAX_KEYS_PER_PROVIDER = 10
MIN_KEY_LEN, MAX_KEY_LEN = 8, 512


class SecretStoreError(ValueError):
    pass


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32      # type: ignore[attr-defined]
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)):
        raise SecretStoreError("O Windows recusou abrir/proteger o cofre (outro usuário ou PC?).")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def validate_key(key: str) -> str:
    key = (key or "").strip()
    if not MIN_KEY_LEN <= len(key) <= MAX_KEY_LEN or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in key):
        raise SecretStoreError("Chave inválida (8 a 512 caracteres, sem espaços).")
    return key


class SecretStore:
    def __init__(self, path: Path, use_dpapi: bool | None = None):
        self.path = Path(path)
        self.use_dpapi = (sys.platform == "win32") if use_dpapi is None else use_dpapi
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ disco
    def _read(self) -> Dict[str, str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, data: Dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, self.path)

    def _seal(self, keys: List[str]) -> str:
        raw = json.dumps(keys).encode("utf-8")
        return base64.b64encode(_dpapi(raw, True) if self.use_dpapi else raw).decode("ascii")

    def _open(self, blob: str) -> List[str]:
        raw = base64.b64decode(blob.encode("ascii"))
        raw = _dpapi(raw, False) if self.use_dpapi else raw
        keys = json.loads(raw.decode("utf-8"))
        return [k for k in keys if isinstance(k, str)]

    # ------------------------------------------------------------------ API
    def names(self) -> List[str]:
        with self._lock:
            return sorted(self._read())

    def get(self, name: str) -> List[str]:
        with self._lock:
            blob = self._read().get(name)
        if not blob:
            return []
        try:
            return self._open(blob)
        except (SecretStoreError, ValueError, OSError):
            return []                      # cofre ilegível: trata como "sem chave", nunca derruba o servidor

    def count(self, name: str) -> int:
        return len(self.get(name))

    def set(self, name: str, keys: List[str]) -> int:
        clean = list(dict.fromkeys(validate_key(k) for k in keys))
        if not clean:
            raise SecretStoreError("Informe ao menos uma chave.")
        if len(clean) > MAX_KEYS_PER_PROVIDER:
            raise SecretStoreError(f"No máximo {MAX_KEYS_PER_PROVIDER} chaves por provedor.")
        with self._lock:
            data = self._read()
            data[name] = self._seal(clean)
            self._write(data)
        return len(clean)

    def delete(self, name: str) -> None:
        with self._lock:
            data = self._read()
            if data.pop(name, None) is not None:
                self._write(data)
