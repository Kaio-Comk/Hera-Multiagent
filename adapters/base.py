from typing import Protocol, runtime_checkable
from models.task import AgentTask, AgentResult
from models.agent import HealthStatus, AgentCapabilities, RunHandle

@runtime_checkable
class AgentAdapter(Protocol):
    """Protocolo uniforme que todos os adaptadores (Ollama, Claude Code, Antigravity, Roteador Hera) implementam."""

    @property
    def name(self) -> str:
        """Identificador único do adaptador."""
        ...

    async def health_check(self) -> HealthStatus:
        """Verifica conectividade, versão e modelos disponíveis."""
        ...

    async def capabilities(self) -> AgentCapabilities:
        """Retorna o conjunto de capacidades do agente/motor."""
        ...

    async def submit(self, task: AgentTask) -> RunHandle:
        """Inicia o processamento da tarefa."""
        ...

    async def collect(self, handle: RunHandle) -> AgentResult:
        """Aguarda ou coleta o resultado da execução."""
        ...

    async def cancel(self, handle: RunHandle) -> bool:
        """Cancela a execução em andamento de forma limpa."""
        ...
