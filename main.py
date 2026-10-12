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
from app.vault_memory import ObsidianMemoryManager
from app.dag_scheduler import TaskDAG, DAGScheduler
from app.worktree_manager import WorktreeManager
from app.validator import TaskValidator
from app.auto_fix import AutoFixEngine
from app.benchmark import LocalBenchmark
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

        v_dir = vault_dir or r"C:\Users\<usuario>\Documents\Obsidian Vault\Orquestrador Multiagente"
        self.vault_writer = ObsidianVaultWriter(vault_project_dir=v_dir)
        self.memory_manager = ObsidianMemoryManager(vault_project_dir=v_dir)

        # Callbacks de feedback visual
        self.coordinator = Coordinator(store=self.store, on_progress=self._print_feedback)

        # Validador determinístico e motor de auto-correção
        self.validator = TaskValidator()
        self.worktree_manager = WorktreeManager(repo_root=os.getcwd())
        self.autofix = AutoFixEngine(
            router=self.router,
            coordinator=self.coordinator,
            validator=self.validator,
            on_feedback=self._print_feedback
        )

        # Recuperação determinística de falhas (Crash Recovery)
        recovered = self.store.recover_interrupted_tasks()
        if recovered:
            self._print_feedback(f"⚠️ [RECOVERY] {len(recovered)} tarefas interrompidas foram recuperadas e marcadas como FAILED.")

    def _print_feedback(self, msg: str):
        timestamp = time.strftime("%H:%M:%S")
        print(f"[{timestamp}] {msg}", flush=True)

    async def run_benchmark(self, model: str = "qwen2.5-coder:1.5b", prompt: Optional[str] = None):
        """Executa medição de throughput e latência de inferência na GPU local."""
        print("\n" + "=" * 60)
        print("⚡ BENCHMARK DE HARDWARE E INFERÊNCIA LOCAL (OLLAMA)")
        print("=" * 60)
        bm = LocalBenchmark()
        p = prompt or "Escreva uma implementação concisa de quicksort em Python com type hints."
        self._print_feedback(f"Iniciando benchmark com modelo '{model}'...")
        res = await bm.run_benchmark(model=model, prompt=p)
        print(f"GPU Detectada:       {res.gpu_name or 'N/A'}")
        if res.vram_used_mb and res.vram_total_mb:
            print(f"VRAM Utilizada:      {res.vram_used_mb} MiB / {res.vram_total_mb} MiB")
        print(f"Tokens Gerados:      {res.tokens_generated}")
        print(f"Tempo de Geração:    {res.eval_duration_sec}s")
        print(f"Velocidade:          {res.tokens_per_second} tokens/s")
        print(f"Tempo Total:         {res.total_duration_sec}s")
        print("=" * 60 + "\n")
        return res

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
        # 1. Injetar contexto relevante do Obsidian Vault se aplicável
        context_block = self.memory_manager.extract_context_from_instruction(instruction)
        final_instruction = instruction
        if context_block:
            final_instruction = f"{context_block}\n{instruction}"
            self._print_feedback("🧠 **[MEMÓRIA]** Contexto relevante injetado a partir do Obsidian Vault.")

        task_id = f"task-{uuid.uuid4().hex[:8]}"
        task = AgentTask(
            id=task_id,
            title=title,
            instruction=final_instruction,
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

    # Comando: vault-search
    vault_parser = subparsers.add_parser("vault-search", help="Pesquisar notas e convenções no Obsidian Vault")
    vault_parser.add_argument("query", type=str, help="Termo ou conceito a ser pesquisado no vault")

    # Comando: list
    subparsers.add_parser("list", help="Listar histórico recente de tarefas no SQLite")
    subparsers.add_parser("approval-code", help="Mostrar o código de aprovação humana atual (HITL / MCP)")

    # Comando: benchmark
    bench_parser = subparsers.add_parser("benchmark", help="Medir throughput e latência de inferência local na GPU")
    bench_parser.add_argument("--model", type=str, default="qwen2.5-coder:1.5b", help="Modelo Ollama a avaliar")
    bench_parser.add_argument("--prompt", type=str, default=None, help="Prompt de teste para o benchmark")

    # Comando: web
    web_parser = subparsers.add_parser("web", help="Iniciar interface Web no navegador")
    web_parser.add_argument("--port", type=int, default=8000, help="Porta do servidor HTTP (padrão: 8000)")
    web_parser.add_argument("--no-browser", action="store_true", help="Não abrir navegador automaticamente")

    # Comando: gui
    gui_parser = subparsers.add_parser("gui", help="Abrir interface Desktop nativa do Windows (1 clique)")
    gui_parser.add_argument("--port", type=int, default=8000, help="Porta do servidor interno (padrão: 8000)")

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

    elif args.command == "vault-search":
        notes = orchestrator.memory_manager.search_context(args.query, max_notes=5)
        print(f"\n🔍 Notas encontradas no Vault para '{args.query}': {len(notes)}")
        for n in notes:
            print(f"- 📄 [[{n['title']}]] (Relevância: {n['score']})")
            print(f"  {n['content'][:180].strip()}...\n")

    elif args.command == "approval-code":
        from app.security.data_dir import data_dir
        from app.security.human_code import HumanApprovalCode, load_or_create_secret
        hc = HumanApprovalCode(load_or_create_secret(data_dir() / ".hera_approval_secret"))
        print(f"🔑 Código de aprovação humana: {hc.current()}  (válido por ~{hc.seconds_left()}s)")

    elif args.command == "list":
        tasks = orchestrator.store.list_tasks(limit=10)
        print(f"\nTotal de tarefas registradas: {len(tasks)}")
        for t in tasks:
            print(f"- [{t.status.value:<9}] {t.id} | {t.assigned_agent or 'none'} | {t.title}")

    elif args.command == "benchmark":
        await orchestrator.run_benchmark(model=args.model, prompt=args.prompt)

    elif args.command == "web":
        import uvicorn
        import webbrowser
        port = args.port
        url = f"http://127.0.0.1:{port}"
        print(f"\n👑 [HERA] Servidor Web iniciando em {url}...")
        print("🔐 [HERA] API protegida por token. A interface web já o recebe sozinha; para ferramentas externas (/v1) use o conteúdo de data/.hera_token como Bearer (ou defina HERA_API_KEY).")
        if not args.no_browser:
            asyncio.get_event_loop().call_later(0.8, lambda: webbrowser.open(url))
        config = uvicorn.Config("app.server:app", host="127.0.0.1", port=port, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()

    elif args.command == "gui":
        import threading
        import uvicorn
        import webview
        port = args.port
        url = f"http://127.0.0.1:{port}"

        def _run_server():
            uvicorn.run("app.server:app", host="127.0.0.1", port=port, log_level="warning")

        thread = threading.Thread(target=_run_server, daemon=True)
        thread.start()
        time.sleep(1.2)

        print(f"👑 [HERA] Abrindo janela Desktop nativa...")
        webview.create_window(
            title="👑 HERA Multiagent — Mission Control",
            url=url,
            width=1340,
            height=860,
            resizable=True
        )
        webview.start()

if __name__ == "__main__":
    asyncio.run(main())
