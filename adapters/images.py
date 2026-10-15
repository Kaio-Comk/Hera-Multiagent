"""Imagens anexadas a uma tarefa: o que cada motor consegue fazer com elas.

  * Claude Code e Antigravity (CLIs com ferramenta de leitura): recebem o CAMINHO no prompt e a pasta liberada por `--add-dir`;
  * Ollama: recebe a imagem em base64 no corpo, mas só se o modelo enxergar (senão a tarefa avisa que ignorou);
  * Roteador embutido: ainda não envia imagens (o aviso entra no texto da tarefa).
`AgentTask.images` só é preenchido pelo servidor, a partir de anexos já validados (`app/uploads.py`).
"""
from __future__ import annotations

import base64
import os
from typing import Iterable, List

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
MAX_BYTES = 10 * 1024 * 1024
# nomes de modelos locais que costumam aceitar imagem (a checagem de verdade é `/api/show`, quando o Ollama informa)
VISION_HINTS = ("llava", "vision", "-vl", "vl:", "minicpm-v", "moondream", "bakllava", "pixtral", "gemma3", "qwen2.5vl", "qwen3-vl",
                "granite3.2-vision", "llama3.2-vision")


def usable_images(paths: Iterable[str]) -> List[str]:
    """Só arquivos que existem, são imagem pela extensão e cabem no limite."""
    out: List[str] = []
    for p in paths or []:
        try:
            if os.path.isfile(p) and p.lower().endswith(IMAGE_EXTS) and os.path.getsize(p) <= MAX_BYTES:
                out.append(os.path.abspath(p))
        except OSError:
            continue
    return out


def image_dirs(paths: Iterable[str]) -> List[str]:
    return sorted({os.path.dirname(p) for p in usable_images(paths)})


def prompt_note(paths: Iterable[str]) -> str:
    """Texto que manda um agente com ferramenta de leitura abrir as imagens."""
    imgs = usable_images(paths)
    if not imgs:
        return ""
    return ("\n\nImagens anexadas pelo usuário (abra cada arquivo com a ferramenta de leitura e use o que vê nelas):\n"
            + "\n".join(f"- {p}" for p in imgs))


def not_supported_note(engine: str, paths: Iterable[str]) -> str:
    n = len(usable_images(paths))
    return f"\n\n(O usuário anexou {n} imagem(ns), mas o motor '{engine}' não recebe imagens; responda só com o texto.)" if n else ""


def encode_base64(paths: Iterable[str]) -> List[str]:
    out: List[str] = []
    for p in usable_images(paths):
        try:
            with open(p, "rb") as fh:
                out.append(base64.b64encode(fh.read()).decode("ascii"))
        except OSError:
            continue
    return out


def looks_like_vision_model(name: str) -> bool:
    low = (name or "").lower()
    return any(h in low for h in VISION_HINTS)
