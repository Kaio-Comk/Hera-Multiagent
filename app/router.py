from typing import Dict, Tuple, Optional
from models.task import AgentTask
from adapters.base import AgentAdapter

class TaskRouter:
    """Roteador inteligente de tarefas multiagente.
    
    Aplica heurísticas determinísticas e fórmula de pontuação para escolher
    o melhor motor de execução (Ollama, 9Router, Claude Code, Antigravity).
    """

    def __init__(self, adapters: Dict[str, AgentAdapter], default_offline: bool = False):
        self.adapters = adapters
        self.default_offline = default_offline

    async def route(self, task: AgentTask) -> Tuple[AgentAdapter, str, str]:
        """Seleciona o adaptador ideal, modelo sugerido e justificativa para auditoria."""

        # 1. Se o usuário explicitou o agente na tarefa
        if task.assigned_agent and task.assigned_agent in self.adapters:
            adapter = self.adapters[task.assigned_agent]
            model = task.assigned_model or getattr(adapter, "default_model", "default")
            reason = f"Atribuição manual direta solicitada para '{task.assigned_agent}'"
            return adapter, model, reason

        # 2. Tarefa com restrição de privacidade ou modo offline
        if self.default_offline or task.task_type == "offline":
            adapter = self.adapters.get("ollama")
            if adapter:
                return adapter, "qwen2.5-coder:1.5b", "Política de privacidade/offline: executado 100% na GPU local via Ollama"

        # 3. Tarefas de codificação e refatoração
        if task.task_type == "coding":
            # Preferir Antigravity ou Claude Code para edição de código e ferramentas de sistema
            if "antigravity" in self.adapters:
                return self.adapters["antigravity"], "gemini-2.5-pro", "Codificação com suporte a workspace e terminal via Antigravity CLI"
            if "claude_code" in self.adapters:
                return self.adapters["claude_code"], "claude-3-7-sonnet", "Codificação avançada via Claude Code CLI"
            # Fallback local
            if "ollama" in self.adapters:
                return self.adapters["ollama"], "qwen2.5-coder:1.5b", "Codificação local em GPU via Ollama (qwen2.5-coder)"

        # 4. Tarefas de pesquisa, triagem, segunda opinião ou resumo
        if task.task_type in ("research", "review", "summary"):
            # Preferir 9Router se disponível (modelos em nuvem rápidos/gratuitos)
            if "9router" in self.adapters:
                return self.adapters["9router"], "alicode-intl/qwen3.5-plus", "Triagem/pesquisa via 9Router (alta velocidade e modelos atualizados)"
            if "ollama" in self.adapters:
                return self.adapters["ollama"], "qwen2.5-coder:1.5b", "Resumo local via Ollama"

        # 5. Default Fallback determinístico
        if "ollama" in self.adapters:
            return self.adapters["ollama"], "qwen2.5-coder:1.5b", "Roteamento padrão para motor local (GPU GTX 1650 SUPER)"
        first_key = next(iter(self.adapters.keys()))
        return self.adapters[first_key], "default", f"Roteamento de fallback para o primeiro adaptador disponível ({first_key})"
