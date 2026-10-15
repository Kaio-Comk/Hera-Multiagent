"""Imagens enviadas nas conversas: o tipo vem do CONTEÚDO (bytes iniciais), nunca do nome nem do Content-Type do navegador.

Aceita só PNG, JPEG, GIF e WebP (SVG fica de fora de propósito: pode carregar script). Os arquivos ficam em
`<dados>/uploads/<conversa>/<id aleatório>.<ext>`; o nome original é só rótulo de exibição e nunca vira caminho.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import Dict, Iterable, Optional

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES_PER_MESSAGE = 6
CONVERSATION_ID = re.compile(r"^c_[0-9a-f]{12}$")
SERVE_HEADERS = {"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'",
                 "Cache-Control": "private, max-age=3600", "Content-Disposition": "inline"}


class UploadError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def sniff_image(data: bytes) -> Optional[Dict[str, str]]:
    """Tipo real da imagem pelos primeiros bytes; None se não for um dos formatos aceitos."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return {"mime": "image/png", "ext": ".png"}
    if data[:3] == b"\xff\xd8\xff":
        return {"mime": "image/jpeg", "ext": ".jpg"}
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return {"mime": "image/gif", "ext": ".gif"}
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return {"mime": "image/webp", "ext": ".webp"}
    return None


def uploads_root(data_dir: Path) -> Path:
    return Path(data_dir) / "uploads"


def save_image(data_dir: Path, conversation_id: str, data: bytes) -> Dict[str, object]:
    """Valida e grava a imagem. Devolve {mime, size, path}. Levanta UploadError com mensagem pronta."""
    if not CONVERSATION_ID.match(conversation_id or ""):
        raise UploadError("Conversa inválida.")
    if not data:
        raise UploadError("O arquivo está vazio.")
    if len(data) > MAX_IMAGE_BYTES:
        raise UploadError(f"Imagem grande demais (máximo {MAX_IMAGE_BYTES // (1024 * 1024)} MB).", 413)
    kind = sniff_image(data)
    if kind is None:
        raise UploadError("Só aceito imagens PNG, JPEG, GIF ou WebP (o conteúdo do arquivo não é uma delas).", 415)
    folder = uploads_root(data_dir) / conversation_id
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{uuid.uuid4().hex}{kind['ext']}"
    path.write_bytes(data)
    return {"mime": kind["mime"], "size": len(data), "path": str(path)}


def is_inside_uploads(data_dir: Path, path: str) -> bool:
    try:
        Path(path).resolve().relative_to(uploads_root(data_dir).resolve())
        return True
    except (ValueError, OSError):
        return False


def remove_files(data_dir: Path, paths: Iterable[str]) -> int:
    """Apaga arquivos de anexo (só dentro da pasta de uploads) e a pasta da conversa se ficar vazia."""
    removed = 0
    for p in paths:
        if not is_inside_uploads(data_dir, p):
            continue
        try:
            fp = Path(p)
            fp.unlink(missing_ok=True)
            removed += 1
            if not any(fp.parent.iterdir()):
                fp.parent.rmdir()
        except OSError:
            continue
    return removed


def read_as_base64(data_dir: Path, path: str) -> Optional[str]:
    """Conteúdo em base64 (para motores que recebem a imagem no corpo); None se o arquivo não estiver em uploads."""
    import base64
    if not is_inside_uploads(data_dir, path):
        return None
    try:
        return base64.b64encode(Path(path).read_bytes()).decode("ascii")
    except OSError:
        return None
