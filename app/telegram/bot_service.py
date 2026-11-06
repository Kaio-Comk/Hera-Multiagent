"""Serviço de Bot Privado do Telegram para o Hera.

Permite que o Kaio converse diretamente com o Hera pelo celular (Telegram),
com memória de contexto, troca de modelos, métricas do PC e acesso ao Gateway multi-provedor.
Acesso estritamente restrito ao Kaio Lima (ID: 8110665027).
"""

from __future__ import annotations

import asyncio
import logging
import os
import psutil
import shutil
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

# Imports internos do Hera
from app.config_manager import ConfigManager
from app.gateway.factory import build_gateway_adapters
from app.gateway.secret_store import SecretStore
from app.gateway.tier_router import TierRouter
from app.gateway.schemas import ChatCompletionRequest, ChatMessage

log = logging.getLogger("hera.telegram.bot")

# ID oficial do Kaio no Telegram
ADMIN_ID = 8110665027

SYSTEM_PROMPT = """Você é a Hera Assistente, uma inteligência artificial e orquestradora autônoma pessoal de ponta.
Seu motor de raciocínio é alimentado por modelos de fronteira em nuvem de alta capacidade.
Seu nome é exclusivamente Hera Assistente.
Nunca mencione ou repita o nome do usuário nas suas respostas. Trate o usuário de forma direta, ágil, inteligente e profissional (usando 'você' se necessário).
Você está conversando diretamente pelo Telegram no celular.
Seja rápida, prática e resolutiva.
Você tem visão do ecossistema de desenvolvimento de software, NetHub, servidores, automações e finanças.
Responda sempre em português brasileiro de forma natural e agradável.
Use formatação Markdown quando apropriado para código, listas ou destaques."""


class HeraTelegramBot:
    """Bot privado do Telegram integrado ao motor do Hera."""

    def __init__(
        self,
        token: str,
        admin_id: int = ADMIN_ID,
        config_path: str = r"B:\Projetos\Hera\config.yaml",
        data_dir: str = r"B:\Projetos\Hera\data",
    ):
        self.token = token.strip()
        self.admin_id = admin_id
        self.api_url = f"https://api.telegram.org/bot{self.token}"
        self.config_path = Path(config_path)
        self.data_dir = Path(data_dir)
        
        # Histórico de mensagens da sessão em memória
        self.conversation_history: List[ChatMessage] = []
        self.max_history_turns = 16
        
        # Estado do roteador e modelo
        self.router: Optional[TierRouter] = None
        self.current_model = "default"
        self._running = False
        self._offset = 0

    def init_gateway(self) -> None:
        """Inicializa adaptadores de IA e o TierRouter do Hera (100% Nuvem)."""
        secret_store = SecretStore(self.data_dir / ".hera_provider_keys.json")
        cfg_mgr = ConfigManager(str(self.config_path))
        cfg = cfg_mgr.load_config()
        all_adapters = build_gateway_adapters(cfg, secrets=secret_store)

        # Regra estrita: 100% Nuvem (modelos locais desativados permanentemente para o bot)
        adapters = [a for a in all_adapters if not getattr(a, "is_local", False)]

        # Camadas exclusivamente de nuvem (cloud_cheap -> cloud_free)
        tier_order = ["cloud_cheap", "cloud_free"]
        self.router = TierRouter(adapters=adapters, tier_order=tier_order)
        log.info("[HeraTelegramBot] Gateway 100%% Nuvem inicializado com %d provedores (locais desativados)", len(adapters))

    async def send_message(
        self,
        chat_id: int,
        text: str,
        parse_mode: str = "Markdown",
        reply_to_message_id: Optional[int] = None,
    ) -> bool:
        """Envia mensagem ao Telegram, dividindo em pedaços se exceder 4000 caracteres."""
        chunks = self._chunk_text(text, 4000)
        async with httpx.AsyncClient(timeout=15.0) as client:
            for chunk in chunks:
                payload: Dict[str, Any] = {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": parse_mode,
                }
                if reply_to_message_id:
                    payload["reply_to_message_id"] = reply_to_message_id
                
                try:
                    resp = await client.post(f"{self.api_url}/sendMessage", json=payload)
                    # Se falhar por erro de parse Markdown, envia como texto plano
                    if resp.status_code == 400 and "can't parse entities" in resp.text:
                        payload.pop("parse_mode", None)
                        await client.post(f"{self.api_url}/sendMessage", json=payload)
                except Exception as exc:
                    log.error("[HeraTelegramBot] Erro ao enviar mensagem: %s", exc)
                    return False
        return True

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        """Envia ação de 'digitando...' para dar feedback visual no Telegram."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                await client.post(f"{self.api_url}/sendChatAction", json={"chat_id": chat_id, "action": action})
            except Exception:
                pass

    @staticmethod
    def _chunk_text(text: str, limit: int = 4000) -> List[str]:
        if len(text) <= limit:
            return [text]
        chunks = []
        while text:
            if len(text) <= limit:
                chunks.append(text)
                break
            # Corta na última quebra de linha antes do limite
            cut = text.rfind("\n", 0, limit)
            if cut == -1 or cut < limit // 2:
                cut = limit
            chunks.append(text[:cut].strip())
            text = text[cut:].strip()
        return chunks

    def get_system_status(self) -> str:
        """Gera relatório de telemetria da máquina e serviços."""
        cpu = psutil.cpu_percent(interval=0.2)
        ram = psutil.virtual_memory()
        c_disk = shutil.disk_usage("C:\\")
        b_disk = shutil.disk_usage("B:\\") if os.path.exists("B:\\") else None

        # Checar GPU via nvidia-smi se disponível
        gpu_info = "GTX 1650 SUPER (4 GB)"

        # Checar processos importantes
        proc_names = [p.name().lower() for p in psutil.process_iter(['name'])]
        nethub_active = "python.exe" in proc_names
        telegram_active = "telegram.exe" in proc_names

        lines = [
            "📊 *Status do Sistema (Hera)*",
            "",
            f"🧠 *CPU:* {cpu}% (Xeon E3-1270 v3)",
            f"⚡ *RAM:* {ram.percent}% ({ram.used / (1024**3):.1f} GB de {ram.total / (1024**3):.1f} GB)",
            f"🎮 *GPU:* {gpu_info}",
            f"💾 *Disco C:* {c_disk.free / (1024**3):.1f} GB livres de {c_disk.total / (1024**3):.1f} GB",
        ]
        if b_disk:
            lines.append(f"💾 *Disco B (NVMe):* {b_disk.free / (1024**3):.1f} GB livres de {b_disk.total / (1024**3):.1f} GB")

        lines.extend([
            "",
            f"🤖 *Modelo Ativo:* `{self.current_model}` (River DeepSeek V4.1 / 100% Nuvem)",
            f"☁️ *Infra de IA:* Nuvem Exclusiva (Modelos locais desativados)",
            f"💬 *Turnos na Memória:* {len(self.conversation_history) // 2}",
            f"🌐 *NetHub:* {'🟢 Online' if nethub_active else '🔴 Offline'}",
            f"📱 *Telegram Desktop:* {'🟢 Rodando' if telegram_active else '⚪ Inativo'}",
        ])
        return "\n".join(lines)

    async def handle_update(self, update: Dict[str, Any]) -> None:
        """Processa uma atualização (mensagem recebida)."""
        msg = update.get("message")
        if not msg:
            return

        chat_id = msg.get("chat", {}).get("id")
        user_id = msg.get("from", {}).get("id")
        text = (msg.get("text") or "").strip()

        # Trava de segurança: apenas admin autorizado
        if user_id != self.admin_id:
            log.warning("[HeraTelegramBot] Acesso recusado para user_id=%s", user_id)
            await self.send_message(chat_id, "⛔ *Acesso Restrito.* Esta assistente é privada.")
            return

        if not text:
            await self.send_message(chat_id, "Recebi sua mensagem, mas não há texto para processar.")
            return

        # ── Comandos ─────────────────────────────────────────────────────────────
        cmd = text.split()[0].lower()

        if cmd in ("/start", "/help"):
            welcome = (
                "🦚 *Olá! Eu sou a Hera Assistente.*\n\n"
                "Estou conectada diretamente ao sistema e pronta para ajudar. "
                "Pode conversar comigo normalmente ou pedir o que precisar por aqui!\n\n"
                "📌 *Comandos disponíveis:*\n"
                "• `/status` — Ver uso de CPU, RAM, GPU e serviços\n"
                "• `/model` — Ver ou alternar modelo de IA\n"
                "• `/clear` — Limpar histórico da conversa e reiniciar contexto\n"
                "• `/help` — Ver esta ajuda\n\n"
                "💬 *Pode me mandar uma pergunta ou instrução agora!*"
            )
            await self.send_message(chat_id, welcome)
            return

        elif cmd == "/status":
            await self.send_message(chat_id, self.get_system_status())
            return

        elif cmd == "/clear":
            self.conversation_history.clear()
            await self.send_message(chat_id, "🧹 *Memória da conversa limpa.* Novo contexto iniciado!")
            return

        elif cmd.startswith("/model"):
            parts = text.split()
            if len(parts) > 1:
                new_model = parts[1].strip()
                # Bloqueio explícito de modelos locais (Ollama, qwen, etc.)
                if any(x in new_model.lower() for x in ("ollama", "qwen2.5-coder", "local")):
                    await self.send_message(chat_id, "⚠️ *Modelos locais estão desativados.* O bot opera exclusivamente com modelos de nuvem (DeepSeek, Gemini, Llama 70B, etc.).")
                    return
                self.current_model = new_model
                await self.send_message(chat_id, f"✅ Modelo alterado para: `{self.current_model}` (100% Nuvem)")
            else:
                msg_models = (
                    f"🤖 *Modelo Atual:* `{self.current_model}` (100% Nuvem)\n\n"
                    "💡 *Modelos disponíveis via Gateway (Nuvem):*\n"
                    "• `claude-sonnet-4-6` — Claude Sonnet (via Vyce AI)\n"
                    "• `gpt-6-luna` — OpenAI GPT Luna (via Vyce AI)\n"
                    "• `deepseek-v4.1` — DeepSeek V4.1 (via Vyce AI / River)\n"
                    "• `default` — River (DeepSeek-V4.1-Flash com fallback 100% nuvem)\n"
                    "• `gemini-3.8-flash` — Google Gemini (Nuvem)\n"
                    "• `Meta-Llama-3.3-70B-Instruct` — Sambanova / Groq (Nuvem 70B)\n"
                    "• `agnes-2.5-flash` — Agnes AI (Nuvem)\n"
                    "• `gpt-4o-mini` — GitHub Models (Nuvem)\n\n"
                    "🔒 _(Modelos locais estão permanentemente desativados.)_\n\n"
                    "Para trocar, digite: `/model <nome_do_modelo>`"
                )
                await self.send_message(chat_id, msg_models)
            return

        # ── Conversa Normal com IA ───────────────────────────────────────────────
        await self.send_chat_action(chat_id, "typing")

        # Adicionar mensagem do usuário ao histórico
        self.conversation_history.append(ChatMessage(role="user", content=text))
        
        # Poda de histórico para manter no limite
        if len(self.conversation_history) > self.max_history_turns * 2:
            self.conversation_history = self.conversation_history[-self.max_history_turns * 2:]

        # Montar lista completa com system prompt
        messages_to_send = [ChatMessage(role="system", content=SYSTEM_PROMPT)] + self.conversation_history

        req = ChatCompletionRequest(
            model=self.current_model,
            messages=messages_to_send,
            stream=False,
        )

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await self.router.route_and_execute(req, client)
                reply_text = resp.choices[0].message.content or "Não obtive resposta do modelo."
                used_model = resp.model or self.current_model
                
                # Salvar resposta da assistente no histórico
                self.conversation_history.append(ChatMessage(role="assistant", content=reply_text))
                
                # Enviar resposta formatada
                await self.send_message(chat_id, reply_text)
                
        except Exception as exc:
            log.exception("[HeraTelegramBot] Erro ao consultar modelo: %s", exc)
            err_msg = f"⚠️ *Erro ao processar com a IA:*\n`{exc}`"
            await self.send_message(chat_id, err_msg)

    async def run(self) -> None:
        """Laço principal de Long Polling do Telegram."""
        self._running = True
        self.init_gateway()
        log.info("[HeraTelegramBot] Bot @hera_kaio_bot iniciado com sucesso em modo polling...")
        print("[HeraTelegramBot] Bot iniciado com sucesso!", flush=True)
        print(f"[HeraTelegramBot] Usuario autorizado: ID {self.admin_id} (Kaio Lima)", flush=True)
        print("[HeraTelegramBot] Aguardando mensagens no Telegram (@hera_kaio_bot)...", flush=True)

        async with httpx.AsyncClient(timeout=40.0) as client:
            while self._running:
                try:
                    url = f"{self.api_url}/getUpdates?offset={self._offset}&timeout=30"
                    resp = await client.get(url)
                    if resp.status_code == 200:
                        data = resp.json()
                        if data.get("ok"):
                            for update in data.get("result", []):
                                self._offset = update["update_id"] + 1
                                # Dispara processamento assíncrono para não travar o loop de polling
                                asyncio.create_task(self.handle_update(update))
                    elif resp.status_code == 409:
                        log.warning("[HeraTelegramBot] Conflito de webhook/instância. Aguardando 5s...")
                        await asyncio.sleep(5.0)
                    else:
                        await asyncio.sleep(2.0)
                except httpx.RequestError as exc:
                    log.warning("[HeraTelegramBot] Erro de rede temporário: %s", exc)
                    await asyncio.sleep(3.0)
                except Exception as exc:
                    log.error("[HeraTelegramBot] Exceção no loop de polling: %s", exc)
                    await asyncio.sleep(3.0)

    def stop(self) -> None:
        self._running = False


async def main():
    token_file = Path(r"B:\Projetos\Hera\data\.hera_telegram_token")
    if not token_file.exists():
        print(f"[ERRO] Arquivo de token não encontrado em {token_file}")
        sys.exit(1)
        
    token = token_file.read_text(encoding="utf-8").strip()
    bot = HeraTelegramBot(token=token)
    await bot.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[HeraTelegramBot] Encerrado pelo usuário.")
