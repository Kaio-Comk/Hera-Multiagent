import asyncio
import time
from typing import Dict, Tuple, Optional
from models.task import AgentTask
from adapters.base import AgentAdapter
from app.privacy_guard import PrivacyGuard
from app.quota import QuotaTracker

class TaskRouter:
    """Roteador inteligente de tarefas multiagente com Gate de Privacidade (DLP).
    
    Aplica heurísticas determinísticas, verificação de segredos e regras de isolamento
    para garantir que dados sensíveis nunca sejam enviados a provedores em nuvem.
    """

    def __init__(self, adapters: Dict[str, AgentAdapter], default_offline: bool = False):
        self.adapters = adapters
        self.default_offline = default_offline
        # Ligar/desligar motores e escolher o modelo de cada um pelo painel (sem reiniciar).
        self.disabled: set = set()
        self.default_models: Dict[str, str] = {}
        self.quota = QuotaTracker()          # motores por assinatura que bateram no limite de uso (ficam fora por um tempo)
        # Ultima checagem do Ollama (ele nao sobe sozinho: rotear para ele desligado derruba o passo).
        self._ollama_ok = True
        self._ollama_ts = 0.0

    # Motores que juntam varios provedores e ja desviam entre eles: o limite de UM provedor (ex.: 429 do River)
    # nao pode bloquear o motor inteiro (bug de 28/10/2026).
    MOTORES_MULTIPROVEDOR = frozenset({"hera_router"})

    def registrar_limite(self, engine: str, seconds: int, reason: str = "") -> bool:
        """Marca o motor como sem limite de uso. Motor multiprovedor nao e bloqueado inteiro. True se bloqueou."""
        if engine in self.MOTORES_MULTIPROVEDOR:
            return False
        self.quota.mark(engine, seconds, reason)
        return True

    async def _ollama_vivo(self) -> bool:
        """O Ollama responde? Checagem curta, guardada por 15 s para nao pesar em cada passo."""
        adapter = self.adapters.get("ollama")
        if adapter is None:
            self._ollama_ok = False
            return False
        agora = time.monotonic()
        if agora - self._ollama_ts < 15:
            return self._ollama_ok
        try:
            saude = await asyncio.wait_for(adapter.health_check(), timeout=3)
            ok = bool(getattr(saude, "healthy", False))
        except Exception:
            ok = False
        self._ollama_ts, self._ollama_ok = agora, ok
        return ok

    def active_adapters(self) -> Dict[str, AgentAdapter]:
        return {name: ad for name, ad in self.adapters.items() if name not in self.disabled and not self.quota.blocked(name)}

    def fallback_adapter(self, task: AgentTask, failed_engine: str):
        """Outro motor para refazer um passo cujo motor ficou sem limite de uso. None se nao houver um adequado.
        Passo que mexe em arquivos so vai para outro motor com acesso a arquivos; so texto pode ir ao Roteador/Ollama.
        O claude_code (gasta o plano do Kaio) so entra por ultimo; o Ollama so se estiver respondendo."""
        adapters = {n: a for n, a in self.active_adapters().items() if n != failed_engine}
        order = ["antigravity", "codex", "cursor", "qoder", "codebuddy", "kimi"]
        texto = task.task_type in ("research", "review", "summary", "docs")
        if texto:
            order += ["hera_router"]
            if self._ollama_ok:
                order += ["ollama"]
        order += ["claude_code"]
        for name in order:
            ad = adapters.get(name)
            if ad is None:
                continue
            if name == "hera_router" and not getattr(ad, "has_cloud", lambda: False)():
                continue
            return ad, self._model(name, getattr(ad, "default_model", "default"))
        return None

    def _model(self, engine: str, fallback: str) -> str:
        return self.default_models.get(engine) or fallback

    async def route(self, task: AgentTask) -> Tuple[AgentAdapter, str, str]:
        """Seleciona o adaptador ideal, modelo sugerido e justificativa para auditoria."""
        adapters = self.active_adapters()
        if not adapters:
            raise PermissionError("Nenhum motor esta ligado: ligue ao menos um na aba Motores & IAs.")
        if "ollama" in self.adapters:
            await self._ollama_vivo()      # atualiza o cache: o Ollama desligado nao pode ser o destino padrao

        # 1. GATE DE PRIVACIDADE E POLÍTICA OFFLINE (Prioridade Máxima)
        is_offline_req = self.default_offline or (task.task_type == "offline")
        has_secret, secret_types = PrivacyGuard.scan_content(task.instruction)

        # Se houver segredos detectados ou requisito estrito de offline
        if is_offline_req or has_secret:
            reason_privacy = (
                f"Gate DLP ativado (detectado: {', '.join(secret_types)}): rota forçada para execução 100% local."
                if has_secret else
                "Política de privacidade/offline: rota restrita a execução local."
            )
            # Se o usuário tentou forçar um motor que não é local em uma tarefa sensível, barrar!
            if task.assigned_agent and task.assigned_agent != "ollama":
                reason_privacy += f" Tentativa de envio para '{task.assigned_agent}' bloqueada pelo Gate DLP."

            # Garantir executor local Ollama (Fail-Closed)
            adapter = adapters.get("ollama")
            if adapter and self._ollama_ok:
                return adapter, self._model("ollama", "qwen2.5-coder:1.5b"), f"{reason_privacy} Executado 100% na GPU local via Ollama."

            # Se não houver executor local disponível, FALHAR FECHADO (não fazer fallback para nuvem!)
            raise PermissionError(
                f"Falha de segurança (Fail-Closed): Tarefa confidencial/offline não pode ser enviada para a nuvem "
                f"e o executor local (Ollama) não está disponível (desligado ou sem resposta)."
            )

        # 2. Atribuição manual direta (quando segura)
        if task.assigned_agent and task.assigned_agent in adapters:
            adapter = adapters[task.assigned_agent]
            model = task.assigned_model or getattr(adapter, "default_model", "default")
            reason = f"Atribuição manual direta solicitada para '{task.assigned_agent}'"
            return adapter, model, reason
        if task.assigned_agent and task.assigned_agent in self.adapters:
            # O motor pedido existe, mas esta bloqueado por limite de uso ou desligado: usa outro, em vez de cair
            # no padrao (que era o Ollama, mesmo desligado).
            alt = self.fallback_adapter(task, task.assigned_agent)
            if alt is not None:
                return alt[0], alt[1], (f"Motor '{task.assigned_agent}' indisponivel (limite de uso ou desligado): "
                                        f"usando '{alt[0].name}'")

        # 3. Tarefas de codificação e refatoração
        if task.task_type == "coding":
            # Preferir Antigravity ou Claude Code para edição de código e ferramentas de sistema
            if "antigravity" in adapters:
                return adapters["antigravity"], self._model("antigravity", "default"), "Codificação com suporte a workspace e terminal via Antigravity CLI"
            if "claude_code" in adapters:
                return adapters["claude_code"], self._model("claude_code", "sonnet"), "Codificação avançada via Claude Code CLI"
            # Fallback local
            if "ollama" in adapters and self._ollama_ok:
                return adapters["ollama"], self._model("ollama", "qwen2.5-coder:1.5b"), "Codificação local em GPU via Ollama (qwen2.5-coder)"

        # 4. Tarefas de pesquisa, triagem, segunda opinião ou resumo
        if task.task_type in ("research", "review", "summary"):
            # Roteador embutido do Hera, se houver provedor de nuvem ligado
            hera = adapters.get("hera_router")
            if hera is not None and getattr(hera, "has_cloud", lambda: False)():
                return hera, self._model("hera_router", "default"), "Pesquisa/triagem via Roteador embutido do Hera (nuvem com fallback)"
            if "ollama" in adapters and self._ollama_ok:
                return adapters["ollama"], self._model("ollama", "qwen2.5-coder:1.5b"), "Resumo local via Ollama"

        # 5. Default Fallback determinístico
        if "ollama" in adapters and self._ollama_ok:
            return adapters["ollama"], self._model("ollama", "qwen2.5-coder:1.5b"), "Roteamento padrão para motor local (GPU GTX 1650 SUPER)"
        # Ollama desligado ou ausente: outro motor ligado em vez de um destino que vai falhar.
        alt = self.fallback_adapter(task, "")
        if alt is not None:
            return alt[0], alt[1], f"Motor local desligado: roteamento de fallback para '{alt[0].name}'"
        first_key = next(iter(adapters.keys()))
        return adapters[first_key], "default", f"Roteamento de fallback para o primeiro adaptador disponível ({first_key})"
