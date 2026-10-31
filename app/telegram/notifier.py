from __future__ import annotations

import html
import json
import logging
import os
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from dotenv import dotenv_values

log = logging.getLogger("hera.telegram.notifier")

# Caminhos padrao dos ambientes NetHub
NETHUB_BR_DIR = Path(os.environ.get("NETHUB_BR_PATH", r"B:\Projetos\NetHub\NetHub BR"))
NETHUB_ES_DIR = Path(os.environ.get("NETHUB_ES_PATH", r"B:\Projetos\NetHub\NetHub ES"))

# Regex para limpar emojis e caracteres especiais proibidos
_EMOJI_RE = re.compile(
    "["
    "\U0001F600-\U0001F64F"  # emoticons
    "\U0001F300-\U0001F5FF"  # symbols & pictographs
    "\U0001F680-\U0001F6FF"  # transport & map
    "\U0001F1E0-\U0001F1FF"  # flags (iOS)
    "\U00002702-\U000027B0"
    "\U000024C2-\U0001F251"
    "\U0001F900-\U0001F9FF"  # supplemental symbols
    "\U0001FA70-\U0001FAFF"
    "\U00002600-\U000026FF"
    "]+",
    flags=re.UNICODE,
)
_TRAVESSAO_RE = re.compile(r"\s*[—–]\s*")


def limpar_estilo(texto: str) -> str:
    """Garante ausencia total de emojis e travessoes soltos."""
    if not texto:
        return ""
    sem_emoji = _EMOJI_RE.sub("", texto)
    sem_travessao = _TRAVESSAO_RE.sub(", ", sem_emoji)
    # Evita espacos duplos gerados pela remocao
    return re.sub(r" +", " ", sem_travessao).strip()


def carregar_credenciais(edicao: str = "br") -> tuple[str, str]:
    """Retorna (BOT_TOKEN, ADMIN_ID) para a edicao informada."""
    caminho_env = (NETHUB_ES_DIR if edicao.lower() == "es" else NETHUB_BR_DIR) / "Bot" / ".env"
    env_dados = {}
    if caminho_env.exists():
        try:
            env_dados = dotenv_values(caminho_env)
        except Exception as exc:
            log.warning("[notifier] Erro ao ler %s: %s", caminho_env, exc)

    token = os.environ.get(f"NETHUB_{edicao.upper()}_BOT_TOKEN") or env_dados.get("BOT_TOKEN") or os.environ.get("BOT_TOKEN") or ""
    admin = os.environ.get(f"NETHUB_{edicao.upper()}_ADMIN_ID") or env_dados.get("ADMIN_ID") or os.environ.get("ADMIN_ID") or "8110665027"

    return token, str(admin)


def enviar_telegram_kaio(texto: str, edicao: str = "br", titulo: str = "") -> bool:
    """Envia notificacao ao Kaio (ADMIN_ID) pelo Bot do Telegram."""
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("HERA_ENV") == "test":
        log.debug("[notifier] Ambiente de teste detectado: envio ao Telegram suprimido.")
        return True
    token, admin_id = carregar_credenciais(edicao)
    if not token or not admin_id:
        log.warning("[notifier] Sem BOT_TOKEN ou ADMIN_ID para alertar Kaio.")
        return False

    texto_limpo = limpar_estilo(texto)
    if titulo:
        texto_limpo = f"[{limpar_estilo(titulo).upper()}]\n{texto_limpo}"

    dados = urllib.parse.urlencode({
        "chat_id": admin_id,
        "text": texto_limpo[:4000],
        "disable_web_page_preview": "true",
    }).encode("utf-8")

    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=dados,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as exc:
        log.warning("[notifier] Falha ao enviar mensagem para Kaio no Telegram: %s", exc)
        return False


def notificar_cliente(chat_id: int, texto: str, porta: int = 8088) -> bool:
    """Notifica o cliente diretamente na conversa de suporte via webhook local do ia_suporte.py."""
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("HERA_ENV") == "test":
        log.debug("[notifier] Ambiente de teste detectado: notificacao de cliente suprimida.")
        return True
    texto_limpo = limpar_estilo(texto)
    if not texto_limpo:
        return False

    payload = json.dumps({"chat_id": chat_id, "texto": texto_limpo}).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{porta}/send_customer",
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                log.info("[notifier] Mensagem entregue ao cliente %s via porta %d", chat_id, porta)
                return True
    except Exception as exc:
        log.info("[notifier] Webhook do ia_suporte na porta %d nao respondeu (%s); tentando via Bot API", porta, exc)

    # Fallback: tentar enviar pelo bot Telegram oficial
    token, _ = carregar_credenciais("br")
    if token:
        dados = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": texto_limpo[:4000],
            "disable_web_page_preview": "true",
        }).encode("utf-8")
        try:
            req_bot = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=dados,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req_bot, timeout=10) as resp:
                if resp.status == 200:
                    log.info("[notifier] Mensagem entregue ao cliente %s via Bot API fallback", chat_id)
                    return True
        except Exception as bot_exc:
            log.warning("[notifier] Falha tambem no fallback via Bot API para %s: %s", chat_id, bot_exc)

    return False
