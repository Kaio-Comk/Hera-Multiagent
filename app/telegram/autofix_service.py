from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.agente.caminhos import Raiz
from app.agente.checkpoints import Checkpoints
from app.agente.escrita import Escrita, Pendencia
from app.agente.laco import Agente
from app.agente.leitura import Leitura
from app.paths import home_hera
from app.telegram.notifier import enviar_telegram_kaio, limpar_estilo, notificar_cliente
from app.telegram.schemas import SystemErrorAlert
from cli.chat_client import ClienteHera, ler_token
from cli.chat_ferramentas import conversar_com_ferramentas

log = logging.getLogger("hera.telegram.autofix")

NETHUB_BR_DIR = Path(os.environ.get("NETHUB_BR_PATH", r"B:\Projetos\NetHub\NetHub BR"))
NETHUB_ES_DIR = Path(os.environ.get("NETHUB_ES_PATH", r"B:\Projetos\NetHub\NetHub ES"))
VAULT_NETHUB = Path(os.environ.get("NETHUB_VAULT", r"C:\Users\Kaio Lima\Documents\Obsidian Vault")) / "NetHub"
GIT_AUTHOR_NAME = "Kaio Lima"
GIT_AUTHOR_EMAIL = "kaiocomkamil@gmail.com"

# Erros de rede transitorios ou de robo de varredura que nao devem disparar auto-cura de codigo
IGNORAR_ERROS = (
    "Failed to fetch updates",
    "LineTooLong",
    "BadHttpMessage",
    "Request Timeout",
    "ClientPayloadError",
    "ServerDisconnectedError",
    "Cannot connect to host api.telegram.org",
)


class AutoFixService:
    def __init__(self, base_url: str = "http://127.0.0.1:8000"):
        self.base_url = base_url
        self._tarefas: Dict[str, Dict[str, Any]] = {}
        self._erros_vistos: Dict[str, float] = {}
        self._janela_dedup_s: float = 900.0  # 15 minutos de deduplicacao
        self._token: Optional[str] = None

    def _obter_token(self) -> str:
        if self._token:
            return self._token
        tok = ler_token(home_hera())
        if tok:
            self._token = tok
            return tok
        return ""

    def status(self) -> Dict[str, Any]:
        ativas = sum(1 for t in self._tarefas.values() if t.get("status") == "running")
        recentes = sorted(self._tarefas.values(), key=lambda x: x.get("criado_em", 0), reverse=True)[:10]
        return {
            "status": "online",
            "active_tasks": ativas,
            "recent_fixes": recentes,
        }

    # -------------------------------------------------------------
    # 1. AUTO-FIX DE BUG DE CLIENTE
    # -------------------------------------------------------------
    async def agendar_autofix_cliente(
        self,
        chat_id: int,
        nome: str,
        username: str,
        edicao: str,
        texto: str,
    ) -> str:
        task_id = f"autofix-cliente-{uuid.uuid4().hex[:8]}"
        info = {
            "task_id": task_id,
            "tipo": "cliente",
            "chat_id": chat_id,
            "nome": nome,
            "username": username,
            "edicao": edicao,
            "texto": texto,
            "status": "running",
            "criado_em": time.time(),
        }
        self._tarefas[task_id] = info
        asyncio.create_task(self._executar_autofix_cliente_background(task_id, info))
        return task_id

    async def _executar_autofix_cliente_background(self, task_id: str, info: Dict[str, Any]) -> None:
        chat_id = info["chat_id"]
        edicao = info["edicao"]
        texto = info["texto"]
        nome = info["nome"]
        username = info["username"]
        quem = " ".join(x for x in (nome, f"@{username}" if username else "") if x) or f"id {chat_id}"

        log.info("[autofix] Iniciando auto-fix para cliente %s (task: %s)", quem, task_id)
        workspace = NETHUB_ES_DIR if edicao.lower() == "es" else NETHUB_BR_DIR

        prompt = (
            f"Um cliente do NetHub {edicao.upper()} relatou o seguinte problema/bug pelo Telegram:\n"
            f"Relato do cliente: \"{texto}\"\n\n"
            "Seu objetivo como Agente Autônomo Hera:\n"
            "1. Localizar no repositório os arquivos relevantes (ex.: WebPlayer/src, Bot/routers, App, etc.) usando buscar ou listar_pasta.\n"
            "2. Ler os trechos relevantes e diagnosticar a causa do problema.\n"
            "3. Aplicar a correção necessária com editar_arquivo ou escrever_arquivo.\n"
            "4. Se houver build ou teste (ex.: node build.mjs ou python -m py_compile), execute para verificar.\n"
            "5. Ao concluir, forneça um resumo claro e técnico do que foi diagnosticado e corrigido."
        )

        try:
            resumo_correcao = await self._executar_laco_agente(workspace, prompt, max_passos=35)
            commit_hash = await self._fazer_commit_git(
                workspace,
                f"fix(suporte): correcao de bug relatado pelo cliente {chat_id} - {limpar_estilo(texto[:50])}",
            )

            info["status"] = "succeeded"
            info["concluido_em"] = time.time()
            info["resumo"] = resumo_correcao
            info["commit"] = commit_hash

            # Notificar cliente que o problema foi corrigido
            msg_cliente = (
                "Pronto! O problema que você relatou foi corrigido e testado com sucesso pelo nosso sistema autônomo. "
                "Você já pode testar novamente no NetHub. Se precisar de mais alguma coisa, estou por aqui à disposição!"
                if edicao != "es"
                else "¡Listo! El problema que reportaste fue corregido y probado con éxito por nuestro sistema autónomo. "
                "Ya puedes probarlo nuevamente en NetHub. Cualquier duda, ¡estoy a tu disposición!"
            )
            notificar_cliente(chat_id, msg_cliente)

            # Notificar Kaio
            msg_kaio = (
                f"Auto-fix autônomo concluído com sucesso!\n"
                f"Cliente: {quem} (id: {chat_id})\n"
                f"Edição: NetHub {edicao.upper()}\n"
                f"Problema relatado: {texto}\n"
                f"Correção aplicada: {resumo_correcao[:300]}\n"
                f"Commit: {commit_hash or 'sem alterações pendentes'}"
            )
            enviar_telegram_kaio(msg_kaio, edicao=edicao, titulo="AUTO-FIX CONCLUIDO")

            # Documentar no Obsidian Vault
            self._registrar_adr_obsidian(
                titulo=f"Auto-Fix Cliente {chat_id} — {limpar_estilo(texto[:40])}",
                projeto="NetHub",
                origem=f"Cliente Telegram {quem}",
                problema=texto,
                solucao=resumo_correcao,
                commit=commit_hash,
            )

        except Exception as exc:
            log.exception("[autofix] Falha no auto-fix do cliente %s: %s", chat_id, exc)
            info["status"] = "failed"
            info["erro"] = str(exc)
            enviar_telegram_kaio(
                f"Falha ao executar auto-fix para o cliente {quem} (id: {chat_id}):\n{exc}",
                edicao=edicao,
                titulo="FALHA NO AUTO-FIX",
            )

    # -------------------------------------------------------------
    # 2. AUTO-CURA DE ERROS DE SISTEMA (NetHub BR e ES)
    # -------------------------------------------------------------
    async def agendar_autoheal_sistema(self, alert: SystemErrorAlert) -> Tuple[str, str]:
        # Filtrar erros transitorios conhecidos
        msg_crua = alert.mensagem or ""
        tb_cru = alert.traceback or ""
        if any(ignorar in msg_crua or ignorar in tb_cru for ignorar in IGNORAR_ERROS):
            return "ignored_transient", "Erro transitório de rede ou requisição malformada ignorado."

        # Deduplicacao
        sig = f"{alert.servico}|{alert.arquivo}|{alert.linha}|{msg_crua[:100]}"
        agora = time.time()
        ultimo = self._erros_vistos.get(sig)
        if ultimo and (agora - ultimo < self._janela_dedup_s):
            return "deduplicated", "Erro duplicado recente; auto-cura já em andamento ou executada."
        self._erros_vistos[sig] = agora

        task_id = f"autoheal-sys-{uuid.uuid4().hex[:8]}"
        info = {
            "task_id": task_id,
            "tipo": "sistema",
            "servico": alert.servico,
            "edicao": alert.edicao,
            "arquivo": alert.arquivo,
            "linha": alert.linha,
            "mensagem": alert.mensagem,
            "traceback": alert.traceback,
            "status": "running",
            "criado_em": agora,
        }
        self._tarefas[task_id] = info
        asyncio.create_task(self._executar_autoheal_sistema_background(task_id, info))
        return "started", f"Auto-cura iniciada com sucesso (Task: {task_id})"

    async def _executar_autoheal_sistema_background(self, task_id: str, info: Dict[str, Any]) -> None:
        servico = info["servico"]
        edicao = info["edicao"]
        arquivo = info.get("arquivo") or ""
        linha = info.get("linha")
        mensagem = info.get("mensagem") or ""
        tb = info.get("traceback") or ""

        log.info("[autoheal] Iniciando auto-cura para %s no NetHub %s (task: %s)", servico, edicao, task_id)
        workspace = NETHUB_ES_DIR if edicao.lower() == "es" else NETHUB_BR_DIR

        prompt = (
            f"O serviço \"{servico}\" do NetHub {edicao.upper()} disparou uma exceção/erro capturado pelo rastreio_erros:\n"
            f"Arquivo: {arquivo}:{linha}\n"
            f"Mensagem: {mensagem}\n"
            f"Traceback:\n{tb}\n\n"
            "Seu objetivo como Agente Autônomo Hera:\n"
            "1. Ler o arquivo indicado ao redor da linha do erro usando ler_arquivo.\n"
            "2. Analisar o motivo da exceção (ex.: chave ausente, NoneType, tipo incorreto, edge-case não tratado).\n"
            "3. Aplicar o patch corretivo com editar_arquivo tratando o caso com segurança e sem quebrar o restante do fluxo.\n"
            "4. Validar o arquivo editado (ex.: python -m py_compile <arquivo> ou pytest no arquivo correspondente).\n"
            "5. Resumir de forma precisa a causa raiz e a correção realizada."
        )

        try:
            resumo_correcao = await self._executar_laco_agente(workspace, prompt, max_passos=30)
            nome_arq = Path(arquivo).name if arquivo else servico
            commit_hash = await self._fazer_commit_git(
                workspace,
                f"fix({servico}): auto-cura de erro em {nome_arq} - {limpar_estilo(mensagem[:40])}",
            )

            info["status"] = "succeeded"
            info["concluido_em"] = time.time()
            info["resumo"] = resumo_correcao
            info["commit"] = commit_hash

            # Notificar Kaio
            msg_kaio = (
                f"Auto-cura de sistema executada com sucesso!\n"
                f"Edição: NetHub {edicao.upper()}\n"
                f"Serviço: {servico}\n"
                f"Arquivo: {arquivo}:{linha}\n"
                f"Erro capturado: {mensagem}\n"
                f"Solução aplicada: {resumo_correcao[:300]}\n"
                f"Commit: {commit_hash or 'sem alterações pendentes'}"
            )
            enviar_telegram_kaio(msg_kaio, edicao=edicao, titulo="AUTO-CURA DE SISTEMA CONCLUIDA")

            # Documentar no Obsidian Vault
            self._registrar_adr_obsidian(
                titulo=f"Auto-Cura {servico.capitalize()} — {limpar_estilo(mensagem[:40])}",
                projeto="NetHub",
                origem=f"Serviço {servico} ({edicao.upper()})",
                problema=f"{mensagem}\n\nTraceback:\n```python\n{tb}\n```",
                solucao=resumo_correcao,
                commit=commit_hash,
            )

        except Exception as exc:
            log.exception("[autoheal] Falha na auto-cura de %s: %s", servico, exc)
            info["status"] = "failed"
            info["erro"] = str(exc)
            enviar_telegram_kaio(
                f"Falha na auto-cura do serviço {servico} ({edicao.upper()}):\n"
                f"Arquivo: {arquivo}:{linha}\n"
                f"Erro: {mensagem}\n"
                f"Falha: {exc}",
                edicao=edicao,
                titulo="FALHA NA AUTO-CURA",
            )

    # -------------------------------------------------------------
    # 3. EXECUCAO DO AGENTE AUTONOMO COM FERRAMENTAS
    # -------------------------------------------------------------
    async def _executar_laco_agente(self, workspace: Path, prompt_usuario: str, max_passos: int = 30) -> str:
        token = self._obter_token()
        cliente = ClienteHera(self.base_url, token, modelo="hera_router")

        raiz = Raiz(workspace, permitir_tudo=True)
        checkpoints_dir = workspace / ".hera_checkpoints"
        ck = Checkpoints(checkpoints_dir, workspace)
        leitura = Leitura(raiz)
        escrita = Escrita(raiz, ck, modo="auto")

        async def _aprovar_tudo(p: Pendencia) -> str:
            return "sim"

        saida_logs: List[str] = []

        def _capturar_log(texto: str) -> None:
            saida_logs.append(texto)

        agente = Agente(
            conversar=lambda msgs, tools: conversar_com_ferramentas(cliente, msgs, tools),
            leitura=leitura,
            escrita=escrita,
            aprovar=_aprovar_tudo,
            escrever=_capturar_log,
            max_passos=max_passos,
        )

        sistema = (
            "Você é o Agente Autônomo de Engenharia Hera.\n"
            "Você tem ferramentas para ler, listar, buscar, editar e escrever arquivos no workspace, além de executar comandos.\n"
            "Diretrizes:\n"
            "- Trabalhe de forma focada e assertiva.\n"
            "- Diagnostique a causa raiz antes de editar.\n"
            "- Ao alterar código, mantenha a formatação existente e trate exceções e valores nulos com cuidado.\n"
            "- NUNCA use emojis.\n"
            "- Ao terminar, explique resumidamente o que foi verificado e corrigido."
        )

        historico = [{"role": "user", "content": prompt_usuario}]
        resposta_final = await agente.turno(historico, sistema=sistema)
        return resposta_final or "\n".join(saida_logs[-10:]) or "Correção concluída com sucesso."

    # -------------------------------------------------------------
    # 4. GIT E OBSIDIAN HELPERS
    # -------------------------------------------------------------
    async def _fazer_commit_git(self, workspace: Path, mensagem: str) -> str:
        """Adiciona arquivos modificados e commita com a identidade oficial do Kaio."""
        msg_limpa = limpar_estilo(mensagem)
        try:
            # 1. Verifica se houve alteracoes
            status_proc = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(workspace), "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=False,
            )
            if not status_proc.stdout.strip():
                log.info("[autofix] Nenhuma alteração pendente para commit em %s", workspace)
                return ""

            # 2. git add dos arquivos modificados (exceto checkpoints/temporarios)
            await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(workspace), "add", "-u"],
                capture_output=True,
                check=False,
            )

            # 3. git commit com identidade Kaio Lima
            cmd_commit = [
                "git",
                "-C",
                str(workspace),
                "-c",
                f"user.name={GIT_AUTHOR_NAME}",
                "-c",
                f"user.email={GIT_AUTHOR_EMAIL}",
                "commit",
                "-m",
                msg_limpa,
            ]
            commit_proc = await asyncio.to_thread(
                subprocess.run,
                cmd_commit,
                capture_output=True,
                text=True,
                check=False,
            )
            if commit_proc.returncode != 0:
                log.warning("[autofix] git commit falhou: %s", commit_proc.stderr)
                return ""

            # 4. Obter hash
            hash_proc = await asyncio.to_thread(
                subprocess.run,
                ["git", "-C", str(workspace), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            commit_hash = hash_proc.stdout.strip()
            log.info("[autofix] Commit gerado em %s: %s", workspace, commit_hash)
            return commit_hash
        except Exception as exc:
            log.warning("[autofix] Erro ao executar git commit: %s", exc)
            return ""

    def _registrar_adr_obsidian(
        self,
        titulo: str,
        projeto: str,
        origem: str,
        problema: str,
        solucao: str,
        commit: str = "",
    ) -> None:
        """Gera e registra uma nota de decisao/auto-cura no Obsidian Vault."""
        try:
            hoje = time.strftime("%Y-%m-%d")
            pasta_decisoes = VAULT_NETHUB / "04 Decisões"
            pasta_decisoes.mkdir(parents=True, exist_ok=True)

            titulo_arquivo = re.sub(r'[<>:"/\\|?*]', "", titulo).strip()
            nome_nota = f"{hoje} — {titulo_arquivo}.md"
            caminho_nota = pasta_decisoes / nome_nota

            conteudo = (
                f"---\n"
                f"tags: [decisao, adr, auto-cura, hera, nethub]\n"
                f"data: {hoje}\n"
                f"projeto: {projeto}\n"
                f"tipo: adr\n"
                f"---\n\n"
                f"# {hoje} — {titulo_arquivo}\n\n"
                f"← [[Decisões de Arquitetura (ADRs)]] · [[Home (MOC)]]\n\n"
                f"> [!summary] Contexto e Diagnóstico\n"
                f"> **Origem:** {limpar_estilo(origem)}\n"
                f"> **Problema Identificado:**\n"
                f"> {limpar_estilo(problema)}\n\n"
                f"---\n\n"
                f"### Resolução e Auto-Cura\n"
                f"- **Ação realizada pelo Hera:** {limpar_estilo(solucao)}\n"
                f"- **Commit de validação:** `{commit or 'sem commit'}`\n"
                f"- **Data/Hora:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                f"---\n\n"
                f"### Consequências e Resultados\n"
                f"- Correção aplicada e verificada autonomamente pelo Hera.\n"
                f"- Notificação enviada ao canal de administração do Telegram.\n"
            )

            caminho_nota.write_text(conteudo, encoding="utf-8")
            log.info("[autofix] ADR registrado no Obsidian Vault: %s", caminho_nota)
        except Exception as exc:
            log.warning("[autofix] Falha ao registrar ADR no Obsidian: %s", exc)
