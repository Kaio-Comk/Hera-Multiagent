import os
import sys
import uuid
import time
import asyncio
import argparse
from typing import Optional, List, Dict

from models.task import AgentTask, AgentResult, TaskStatus
from app.state_store import StateStore
from app.coordinator import Coordinator
from app.router import TaskRouter
from app.obsidian import ObsidianVaultWriter
from app.dag_scheduler import TaskDAG, DAGScheduler
from app.worktree_manager import WorktreeManager
from app.validator import TaskValidator
from app.auto_fix import AutoFixEngine
from adapters.ollama import OllamaAdapter
from adapters.nine_router import NineRouterAdapter
from adapters.claude_code import ClaudeCodeAdapter
from adapters.antigravity import AntigravityAdapter

class MultiagentOrchestrator:
    """Orquestrador Multiagente Unificado."""

    def __init__(self, db_path: str = "data/orchestrator.db", vault_dir: Optional[str] = None):
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.store = StateStore(db_path=db_path)

        # Configurar adaptadores
        self.adapters = {
            "ollama": OllamaAdapter(),
            "9router": NineRouterAdapter(),
            "claude_code": ClaudeCodeAdapter(),
            "antigravity": AntigravityAdapter(),
        }

        self.router = TaskRouter(adapters=self.adapters)

        v_dir = vault_dir or r"C:\Users\Kaio Lima\Documents\Obsidian Vault\Orquestrador Multiagente"
        self.vault_writer = ObsidianVaultWriter(vault_project_dir=v_dir)

        # Callbacks de feedback visual
        self.coordinator = Coordinator(store=self.store, on_progress=self._print_feedback)

        # Validador determinístico e motor de auto-correção
        self.validator = TaskValidator()
        self.autofix = AutoFixEngine(
            router=self.router,
            coordinator=self.coordinator,
            validator=self.validator,
            on_feedback=self._print_feedback
        )

    def _print_feedback(self, msg: str):
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] {msg}", flush=True)

    async def check_all_health(self):
        """Verifica a saúde e prontidão de todos os 4 motores/adaptadores."""
        print("\n" + "=" * 60)
        print("🔍 DIAGNÓSTICO DE MOTORES E ADAPTADORES DO ORQUESTRADOR")
        print("=" * 60)

        for name, adapter in self.adapters.items():
            try:
                health = await adapter.health_check()
                status_icon = "🟢 ONLINE" if health.healthy else "🔴 OFFLINE"
                version_str = f"(v{health.version})" if health.version else ""
                models_count = f"{len(health.available_models)} modelos" if health.available_models else "nenhum modelo listado"

                print(f"{status_icon} [{name.upper():<12}] {version_str} - {models_count}")
                if health.available_models and len(health.available_models) <= 4:
                    print(f"   Modelos: {', '.join(health.available_models)}")
                elif health.available_models:
                    print(f"   Exemplos: {', '.join(health.available_models[:3])}...")
                if health.error_message:
                    print(f"   Aviso: {health.error_message}")
            except Exception as e:
                print(f"🔴 ERRO   [{name.upper():<12}] Falha no health check: {e}")

        print("=" * 60 + "\n")

    async def execute_task(
        self,
        title: str,
        instruction: str,
        task_type: str = "coding",
        assigned_agent: Optional[str] = None,
        assigned_model: Optional[str] = None,
        acceptance_criteria: Optional[List[str]] = None,
        validation_commands: Optional[List[str]] = None,
        max_retries: int = 2,
        workspace: Optional[str] = None,
        save_to_obsidian: bool = True
    ) -> AgentResult:
        """Ciclo completo e determinístico de execução de tarefa com auto-correção."""
        task_id = f"task-{uuid.uuid4().hex[:8]}"
        task = AgentTask(
            id=task_id,
            title=title,
            instruction=instruction,
            task_type=task_type,
            assigned_agent=assigned_agent,
            assigned_model=assigned_model,
            acceptance_criteria=acceptance_criteria or [],
            validation_commands=validation_commands or [],
            max_retries=max_retries,
            workspace=workspace or ""
        )

        result = await self.autofix.execute_task_with_retry(task, workspace=workspace)

        # Registro no Obsidian Vault
        if save_to_obsidian and result.status == TaskStatus.SUCCEEDED:
            note_path = self.vault_writer.record_execution(
                task=task,
                result=result,
                validation_items=task.acceptance_criteria
            )
            self._print_feedback(f"📝 [OBSIDIAN] Nota persistida no Vault: {os.path.basename(note_path)}")

        return result

    async def execute_dag(self, dag: TaskDAG, save_to_obsidian: bool = True) -> Dict[str, AgentResult]:
        """Executa um Grafo Acíclico Direcionado (DAG) completo com controle de paralelismo."""
        scheduler = DAGScheduler(
            router=self.router,
            coordinator=self.coordinator,
            validator=self.validator,
            max_concurrency=2,
            on_feedback=self._print_feedback
        )

        results = await scheduler.execute_dag(dag)

        if save_to_obsidian:
            for task_id, res in results.items():
                if res.status == TaskStatus.SUCCEEDED:
                    task = dag.tasks[task_id]
                    note_path = self.vault_writer.record_execution(
                        task=task,
                        result=res,
                        validation_items=task.acceptance_criteria
                    )
                    self._print_feedback(f"📝 [OBSIDIAN] Nota salva: {os.path.basename(note_path)}")

        return results

async def main():
    parser = argparse.ArgumentParser(description="Multiagent Orchestrator CLI")
    subparsers = parser.add_subparsers(dest="command")

    # Comando: health
    subparsers.add_parser("health", help="Verificar status de todos os adaptadores")

    # Comando: run
    run_parser = subparsers.add_parser("run", help="Executar uma tarefa no orquestrador")
    run_parser.add_argument("instruction", type=str, help="Instrução da tarefa")
    run_parser.add_argument("--title", type=str, default="", help="Título da tarefa")
    run_parser.add_argument("--type", type=str, default="summary", choices=["coding", "research", "review", "summary", "offline"])
    run_parser.add_argument("--agent", type=str, default=None, choices=["ollama", "9router", "claude_code", "antigravity"])
    run_parser.add_argument("--model", type=str, default=None, help="Modelo específico")
    run_parser.add_argument("--criteria", type=str, nargs="*", default=[], help="Critérios de aceite")
    run_parser.add_argument("--val-cmd", action="append", default=[], help="Comando de validação (repetível)")
    run_parser.add_argument("--max-retries", type=int, default=2, help="Limite de tentativas de auto-correção")
    run_parser.add_argument("--no-obsidian", action="store_true", help="Não registrar no Obsidian")

    # Comando: dag-demo
    subparsers.add_parser("dag-demo", help="Demonstração prática de paralelismo e dependências com DAG")

    # Comando: list
    subparsers.add_parser("list", help="Listar histórico recente de tarefas no SQLite")

    args = parser.parse_args()

    orchestrator = MultiagentOrchestrator()

    if args.command == "health" or not args.command:
        await orchestrator.check_all_health()

    elif args.command == "dag-demo":
        print("\n" + "=" * 60)
        print("🚀 DEMONSTRAÇÃO DE EXECUÇÃO PARALELA COM DAG DE DEPENDÊNCIAS")
        print("=" * 60)

        dag = TaskDAG()
        id1 = f"dag-p1-{uuid.uuid4().hex[:4]}"
        id2 = f"dag-p2-{uuid.uuid4().hex[:4]}"
        id3 = f"dag-p3-{uuid.uuid4().hex[:4]}"

        # Tarefa 1 e 2 rodam em paralelo
        t1 = AgentTask(
            id=id1,
            title="Função de Hash SHA-256",
            instruction="Escreva uma função simples em Python que retorne o hash SHA-256 de uma string.",
            task_type="coding",
            assigned_agent="ollama",
            assigned_model="qwen2.5-coder:1.5b",
            acceptance_criteria=["Função python válida"]
        )
        t2 = AgentTask(
            id=id2,
            title="Conceito de DAG",
            instruction="Defina o que é um Directed Acyclic Graph (DAG) em computação em exatamente 2 frases.",
            task_type="summary",
            assigned_agent="ollama",
            assigned_model="qwen2.5-coder:1.5b",
            acceptance_criteria=["Definição concisa em 2 frases"]
        )
        # Tarefa 3 aguarda t1 e t2
        t3 = AgentTask(
            id=id3,
            title="Consolidação do Plano",
            instruction="Escreva um breve resumo de aprovação do plano técnico após a execução dos módulos paralelos.",
            task_type="summary",
            dependencies=[id1, id2],
            assigned_agent="ollama",
            assigned_model="qwen2.5-coder:1.5b",
            acceptance_criteria=["Plano técnico consolidado"]
        )

        dag.add_task(t1)
        dag.add_task(t2)
        dag.add_task(t3)

        results = await orchestrator.execute_dag(dag)
        print("\n" + "=" * 60)
        print("RESULTADOS DO DAG:")
        for t_id, r in results.items():
            print(f"- [{r.status.value}] {t_id}: {r.summary}")
        print("=" * 60 + "\n")

    elif args.command == "run":
        title = args.title or args.instruction[:50]
        result = await orchestrator.execute_task(
            title=title,
            instruction=args.instruction,
            task_type=args.type,
            assigned_agent=args.agent,
            assigned_model=args.model,
            acceptance_criteria=args.criteria,
            validation_commands=args.val_cmd,
            max_retries=args.max_retries,
            save_to_obsidian=not args.no_obsidian
        )
        print("\n" + "-" * 50)
        print("RESULTADO FINAL:")
        print(result.output_text if result.output_text else result.summary)
        print("-" * 50)

    elif args.command == "list":
        tasks = orchestrator.store.list_tasks(limit=10)
        print(f"\nTotal de tarefas registradas: {len(tasks)}")
        for t in tasks:
            print(f"- [{t.status.value:<9}] {t.id} | {t.assigned_agent or 'none'} | {t.title}")

if __name__ == "__main__":
    asyncio.run(main())
