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

    def active_adapters(self) -> Dict[str, AgentAdapter]:
        return {name: ad for name, ad in self.adapters.items() if name not in self.disabled and not self.quota.blocked(name)}

    def fallback_adapter(self, task: AgentTask, failed_engine: str):
        """Outro motor para refazer um passo cujo motor ficou sem limite de uso. None se nao houver um adequado.
        Passo que mexe em arquivos so vai para outro motor com acesso a arquivos; so texto pode ir ao Roteador/Ollama."""
        adapters = {n: a for n, a in self.active_adapters().items() if n != failed_engine}
        order = ["claude_code", "antigravity", "codex", "cursor", "qoder", "codebuddy", "kimi"]
        if task.task_type in ("research", "review", "summary", "docs"):
            order += ["hera_router", "ollama"]
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
            if adapter:
                return adapter, self._model("ollama", "qwen2.5-coder:1.5b"), f"{reason_privacy} Executado 100% na GPU local via Ollama."

            # Se não houver executor local disponível, FALHAR FECHADO (não fazer fallback para nuvem!)
            raise PermissionError(
                f"Falha de segurança (Fail-Closed): Tarefa confidencial/offline não pode ser enviada para a nuvem "
                f"e o executor local (Ollama) não está disponível."
            )

        # 2. Atribuição manual direta (quando segura)
        if task.assigned_agent and task.assigned_agent in adapters:
            adapter = adapters[task.assigned_agent]
            model = task.assigned_model or getattr(adapter, "default_model", "default")
            reason = f"Atribuição manual direta solicitada para '{task.assigned_agent}'"
            return adapter, model, reason

        # 3. Tarefas de codificação e refatoração
        if task.task_type == "coding":
            # Preferir Antigravity ou Claude Code para edição de código e ferramentas de sistema
            if "antigravity" in adapters:
                return adapters["antigravity"], self._model("antigravity", "default"), "Codificação com suporte a workspace e terminal via Antigravity CLI"
            if "claude_code" in adapters:
                return adapters["claude_code"], self._model("claude_code", "sonnet"), "Codificação avançada via Claude Code CLI"
            # Fallback local
            if "ollama" in adapters:
                return adapters["ollama"], self._model("ollama", "qwen2.5-coder:1.5b"), "Codificação local em GPU via Ollama (qwen2.5-coder)"

        # 4. Tarefas de pesquisa, triagem, segunda opinião ou resumo
        if task.task_type in ("research", "review", "summary"):
            # Roteador embutido do Hera, se houver provedor de nuvem ligado
            hera = adapters.get("hera_router")
            if hera is not None and getattr(hera, "has_cloud", lambda: False)():
                return hera, self._model("hera_router", "default"), "Pesquisa/triagem via Roteador embutido do Hera (nuvem com fallback)"
            if "ollama" in adapters:
                return adapters["ollama"], self._model("ollama", "qwen2.5-coder:1.5b"), "Resumo local via Ollama"

        # 5. Default Fallback determinístico
        if "ollama" in adapters:
            return adapters["ollama"], self._model("ollama", "qwen2.5-coder:1.5b"), "Roteamento padrão para motor local (GPU GTX 1650 SUPER)"
        first_key = next(iter(adapters.keys()))
        return adapters[first_key], "default", f"Roteamento de fallback para o primeiro adaptador disponível ({first_key})"
