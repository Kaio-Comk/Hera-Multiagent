from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import unicodedata
import re

log = logging.getLogger("hera.telegram.facts")

DEFAULT_VAULT = Path(os.environ.get("NETHUB_VAULT") or (Path.home() / "Documents" / "Obsidian Vault")) / "NetHub"
FACTS_REL_PATH = "03 Operação/Atendimento — Base da IA.md"


@dataclass
class SupportFacts:
    fatos: dict[str, list[str]] = field(default_factory=dict)
    escalar: list[str] = field(default_factory=list)
    tom: list[str] = field(default_factory=list)
    bug: list[str] = field(default_factory=list)
    mtime: float = 0.0
    origem: str = ""


_cached_facts: Optional[SupportFacts] = None


def sem_acento(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s.lower()) if unicodedata.category(c) != "Mn")


def _bullets(bloco: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"(?m)^- (.+)$", bloco)]


def parse_markdown_base(texto: str) -> tuple[dict[str, list[str]], list[str], list[str], list[str]]:
    secoes: dict[str, str] = {}
    for m in re.finditer(r"(?ms)^## (.+?)\s*$(.*?)(?=^## |\Z)", texto):
        secoes[sem_acento(m.group(1)).strip()] = m.group(2)

    fatos = {
        "pt": _bullets(secoes.get("fatos pt", "")),
        "es": _bullets(secoes.get("fatos es", "")),
    }
    tom = _bullets(secoes.get("tom", ""))

    escalar: list[str] = []
    for linha in _bullets(secoes.get("escalar", "")):
        escalar.extend([t.strip() for t in linha.split(",") if t.strip()])

    bug: list[str] = []
    for linha in _bullets(secoes.get("bug", "")):
        bug.extend([t.strip() for t in linha.split(",") if t.strip()])

    return fatos, escalar, tom, bug


def get_support_facts(vault_path: Optional[Path] = None, force_reload: bool = False) -> SupportFacts:
    global _cached_facts
    base_dir = vault_path or DEFAULT_VAULT
    note_path = base_dir / FACTS_REL_PATH

    if not note_path.exists():
        log.warning("[facts_loader] Nota de fatos nao encontrada em %s; usando regras de reserva", note_path)
        if _cached_facts is not None:
            return _cached_facts
        return SupportFacts(
            fatos={
                "pt": [
                    "O bot do Brasil e o @NetHub_Filme_Bot",
                    "Plano Completo por R$ 9,90 por mes (Telegram + site + apps)",
                    "Teste gratis de 30 minutos a partir do primeiro video entregue",
                    "Pagamento por PIX gerado direto no bot @NetHub_Filme_Bot com /assinatura",
                    "App Android em https://gw.nethubfilmes.org/app/mobile.apk",
                    "URL do provedor de midia ou servidor para o app ou TV e https://gw.nethubfilmes.org",
                    "Codigo de recuperacao de senha vai para o e-mail cadastrado (verificar spam e promocoes)",
                ],
                "es": [
                    "El bot en espanol es @NetHub_Peliculas_Bot",
                    "Prueba gratis de 30 minutos a partir del primer video",
                    "Pago con tarjeta (Stripe) o Telegram Stars direto en el bot con /suscripcion",
                    "URL del proveedor o servidor para la app o TV es https://gw.nethubfilmes.org",
                ],
            },
            escalar=["paguei e nao ativou", "fiz o pix e nao liberou", "comprovante", "reembolso", "cancelar", "estorno", "bloqueio", "golpe", "procon"],
            tom=["Curto: no maximo 3 frases", "Simpatico e informal", "Sem emojis"],
            bug=["nao carrega", "nao funciona", "tela preta", "deu erro", "travando", "bug", "nao toca"],
            origem="fallback",
        )

    try:
        current_mtime = note_path.stat().st_mtime
        if not force_reload and _cached_facts is not None and _cached_facts.mtime == current_mtime:
            return _cached_facts

        texto = note_path.read_text(encoding="utf-8")
        fatos, escalar, tom, bug = parse_markdown_base(texto)
        _cached_facts = SupportFacts(
            fatos=fatos,
            escalar=escalar,
            tom=tom,
            bug=bug,
            mtime=current_mtime,
            origem=str(note_path),
        )
        log.info("[facts_loader] Fatos carregados com sucesso do Obsidian (%d fatos PT, %d fatos ES)", len(fatos["pt"]), len(fatos["es"]))
        return _cached_facts
    except Exception as exc:
        log.warning("[facts_loader] Erro ao ler nota de fatos: %s", exc)
        if _cached_facts is not None:
            return _cached_facts
        raise
