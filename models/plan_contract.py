"""Contratos Pydantic estritos para especificacao e validacao de planos e DAGs do Hera.

Implementa a especificacao do padrao Plan Big, Execute Small (Anthropic CMA) e
garante validacao deterministica do Grafo Aciclico Dirigido antes de qualquer execucao.
"""
from typing import List, Optional, Literal, Dict, Any, Set
from pydantic import BaseModel, Field, model_validator


StepType = Literal[
    "coding",
    "research",
    "review",
    "command",
    "validation",
    "arbitration",
    "summary",
    "docs",
    "test",
]


class PlanStepModel(BaseModel):
    """Contrato individual de um passo no plano multiagente."""
    id: str = Field(..., min_length=1, max_length=64, description="Identificador unico do passo (ex: 's1', 'step_busca')")
    title: str = Field(..., min_length=2, max_length=120, description="Titulo conciso da tarefa")
    instruction: str = Field(..., min_length=5, description="Instrucao autocontida para o worker")
    type: StepType = Field(default="coding", description="Papel/categoria semantica do passo")
    engine: str = Field(..., min_length=1, description="Motor responsavel (ollama, hera_router, antigravity, claude_code)")
    model: Optional[str] = Field(default=None, description="Modelo especifico do motor")
    depends_on: List[str] = Field(default_factory=list, description="IDs dos passos que devem concluir com sucesso antes")
    timeout_seconds: int = Field(default=900, ge=10, le=7200, description="Tempo limite em segundos")
    acceptance_criteria: List[str] = Field(default_factory=list, description="Criterios verificaveis de conclusao")
    validation_commands: List[str] = Field(default_factory=list, description="Comandos objetivos de teste (pytest, etc.)")
    files_targeted: List[str] = Field(default_factory=list, description="Arquivos delimitados para alteracao")
    reason: Optional[str] = Field(default=None, description="Motivacao da atribuicao de motor/modelo")


class PlanContractModel(BaseModel):
    """Contrato formal do plano decomposto em DAG aciclico."""
    schema_version: int = Field(default=1, description="Versao do schema de plano")
    strategy: Optional[str] = Field(default="balanced", description="Playbook ou estrategia de quota associada")
    steps: List[PlanStepModel] = Field(..., min_length=1, description="Lista de passos do plano")

    @model_validator(mode="after")
    def validate_dag(self) -> "PlanContractModel":
        step_ids: Set[str] = set()
        id_list = []

        # 1. Unicidade de IDs
        for s in self.steps:
            clean_id = s.id.strip()
            if clean_id in step_ids:
                raise ValueError(f"ID de passo duplicado encontrado no plano: '{clean_id}'")
            step_ids.add(clean_id)
            id_list.append(clean_id)

        # 2. Referencias validas e auto-dependencia
        for s in self.steps:
            for dep in s.depends_on:
                dep_clean = dep.strip()
                if dep_clean == s.id:
                    raise ValueError(f"Passo '{s.id}' nao pode depender de si mesmo.")
                if dep_clean not in step_ids:
                    raise ValueError(
                        f"Passo '{s.id}' depende do id '{dep_clean}', que nao existe no plano. "
                        f"Passos disponiveis: {sorted(list(step_ids))}"
                    )

        # 3. Deteccao rigorosa de ciclos topologicos (Kahn)
        in_degree: Dict[str, int] = {sid: 0 for sid in step_ids}
        adj: Dict[str, List[str]] = {sid: [] for sid in step_ids}

        for s in self.steps:
            for dep in s.depends_on:
                adj[dep.strip()].append(s.id)
                in_degree[s.id] += 1

        queue = [sid for sid, deg in in_degree.items() if deg == 0]
        visited_count = 0

        while queue:
            node = queue.pop(0)
            visited_count += 1
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        if visited_count != len(step_ids):
            # Ciclo detectado! Achar quais passos estao no ciclo
            cycle_nodes = [sid for sid, deg in in_degree.items() if deg > 0]
            raise ValueError(
                f"Ciclo fechado detectado no DAG de passos. Nao e possivel ordenar a execucao: {cycle_nodes}"
            )

        # 4. Validacao de seguranca em comandos de validacao
        for s in self.steps:
            for cmd in s.validation_commands:
                cmd_str = str(cmd or "").strip()
                # Bloquear comandos destrutivos ou shell escapes
                if any(bad in cmd_str for bad in [";", "&&", "||", "|", ">", "<", "`", "$("]):
                    raise ValueError(
                        f"Comando de validacao com caracteres perigosos/shell pipe no passo '{s.id}': '{cmd_str}'"
                    )

        return self
