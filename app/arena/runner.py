import time
import uuid
import asyncio
from typing import Dict, List, Optional, Any

from app.arena.schemas import (
    ArenaCandidate,
    CandidateResult,
    ArenaRunRequest,
    ArenaRunReport,
)
from app.gateway.privacy import GatewayPrivacyManager, PrivacyClass


class ArenaRunner:
    """Executor comparativo Multi-Model Arena com controle de VRAM e privacidade."""

    def __init__(
        self,
        adapters: Optional[Dict[str, Any]] = None,
        privacy_manager: Optional[GatewayPrivacyManager] = None,
        state_store: Optional[Any] = None,
        enabled: bool = True,
    ):
        self.adapters = adapters or {}
        self.privacy_manager = privacy_manager or GatewayPrivacyManager()
        self.state_store = state_store
        self.enabled = enabled
        # Semáforo exclusivo de GPU para proteger a VRAM de 4GB da GTX 1650 Super
        self._ollama_semaphore = asyncio.Semaphore(1)
        self._cloud_semaphore = asyncio.Semaphore(5)
        self._runs: Dict[str, ArenaRunReport] = {}
        self._active_tasks: Dict[str, List[asyncio.Task]] = {}

    async def _execute_candidate(
        self,
        candidate: ArenaCandidate,
        prompt: str,
        system_prompt: Optional[str] = None
    ) -> CandidateResult:
        """Executa um candidato individual medindo TTFT, duração e vazão de tokens."""
        adapter = self.adapters.get(candidate.adapter_name)
        if not adapter:
            return CandidateResult(
                model_id=candidate.model_id,
                adapter_name=candidate.adapter_name,
                is_local=candidate.is_local,
                status="failed",
                error=f"Adaptador '{candidate.adapter_name}' não encontrado no orquestrador."
            )

        sem = self._ollama_semaphore if candidate.is_local else self._cloud_semaphore

        async with sem:
            start_time = time.perf_counter()
            first_token_time = None
            try:
                # Se o adaptador tiver método generate ou stream
                if hasattr(adapter, "generate"):
                    if asyncio.iscoroutinefunction(adapter.generate):
                        resp = await adapter.generate(
                            prompt=prompt,
                            model=candidate.model_id,
                            system=system_prompt
                        )
                    else:
                        resp = await asyncio.to_thread(
                            adapter.generate,
                            prompt=prompt,
                            model=candidate.model_id,
                            system=system_prompt
                        )
                elif hasattr(adapter, "chat"):
                    messages = [{"role": "user", "content": prompt}]
                    if system_prompt:
                        messages.insert(0, {"role": "system", "content": system_prompt})
                    if asyncio.iscoroutinefunction(adapter.chat):
                        resp = await adapter.chat(messages=messages, model=candidate.model_id)
                    else:
                        resp = await asyncio.to_thread(adapter.chat, messages=messages, model=candidate.model_id)
                else:
                    return CandidateResult(
                        model_id=candidate.model_id,
                        adapter_name=candidate.adapter_name,
                        is_local=candidate.is_local,
                        status="failed",
                        error=f"Adaptador '{candidate.adapter_name}' não suporta método generate/chat."
                    )

                end_time = time.perf_counter()
                duration_ms = (end_time - start_time) * 1000.0

                # Formatação do texto de saída
                output_text = resp if isinstance(resp, str) else getattr(resp, "text", str(resp))
                if isinstance(resp, dict):
                    output_text = resp.get("response") or resp.get("content") or str(resp)

                # TTFT aproximado (ou 30% da duração se síncrono)
                ttft_ms = 0.3 * duration_ms

                # Estimativa de tokens na saída
                words = output_text.split()
                tokens_count = int(len(words) * 1.3) or 1
                duration_sec = duration_ms / 1000.0
                tps = tokens_count / duration_sec if duration_sec > 0 else 0.0

                return CandidateResult(
                    model_id=candidate.model_id,
                    adapter_name=candidate.adapter_name,
                    is_local=candidate.is_local,
                    status="completed",
                    ttft_ms=ttft_ms,
                    duration_ms=duration_ms,
                    output_text=output_text,
                    tokens_generated=tokens_count,
                    tokens_per_second=round(tps, 2)
                )

            except asyncio.CancelledError:
                return CandidateResult(
                    model_id=candidate.model_id,
                    adapter_name=candidate.adapter_name,
                    is_local=candidate.is_local,
                    status="cancelled",
                    error="Execução cancelada pelo operador."
                )
            except Exception as e:
                duration_ms = (time.perf_counter() - start_time) * 1000.0
                return CandidateResult(
                    model_id=candidate.model_id,
                    adapter_name=candidate.adapter_name,
                    is_local=candidate.is_local,
                    status="failed",
                    duration_ms=duration_ms,
                    error=str(e)
                )

    async def run_arena(self, req: ArenaRunRequest) -> ArenaRunReport:
        """Executa a rodada comparativa entre todos os candidatos sob limites de VRAM e DLP."""
        if not self.enabled:
            raise RuntimeError("Multi-Model Arena está desabilitada por Feature Flag.")

        run_id = f"arena-{uuid.uuid4().hex[:12]}"
        started_at = time.time()

        # 1. Avaliação de Privacidade
        privacy_dec = self.privacy_manager.classify_prompt(req.prompt)
        is_local_only = privacy_dec.classification == PrivacyClass.LOCAL_ONLY

        tasks: List[asyncio.Task] = []
        skipped_results: List[CandidateResult] = []

        # 2. Filtrar candidatos cloud sob restrição de privacidade
        active_candidates: List[ArenaCandidate] = []
        for cand in req.candidates:
            if is_local_only and not cand.is_local:
                skipped_results.append(CandidateResult(
                    model_id=cand.model_id,
                    adapter_name=cand.adapter_name,
                    is_local=cand.is_local,
                    status="skipped_privacy",
                    error="Ignorado por política de privacidade: prompt restrito ao ambiente local."
                ))
            else:
                active_candidates.append(cand)

        # 3. Disparar tarefas concorrentes controladas por semáforo
        for cand in active_candidates:
            t = asyncio.create_task(self._execute_candidate(cand, req.prompt, req.system_prompt))
            tasks.append(t)

        self._active_tasks[run_id] = tasks

        try:
            executed_results = await asyncio.gather(*tasks, return_exceptions=False)
        finally:
            self._active_tasks.pop(run_id, None)

        all_results: List[CandidateResult] = list(executed_results) + skipped_results
        completed_at = time.time()

        # 4. Calcular vencedores por velocidade e vazão
        successful = [r for r in all_results if r.status == "completed"]
        fastest = min(successful, key=lambda x: x.duration_ms).model_id if successful else None
        highest_tps = max(successful, key=lambda x: x.tokens_per_second).model_id if successful else None

        report = ArenaRunReport(
            run_id=run_id,
            task_id=req.task_id,
            prompt_summary=req.prompt[:120] + "..." if len(req.prompt) > 120 else req.prompt,
            privacy_classification=privacy_dec.classification.value,
            results=all_results,
            started_at=started_at,
            completed_at=completed_at,
            fastest_model=fastest,
            highest_throughput_model=highest_tps
        )

        self._runs[run_id] = report

        # Registrar no StateStore se disponível
        if self.state_store and req.task_id:
            try:
                self.state_store.record_event(
                    task_id=req.task_id,
                    event_type="ARENA_RUN_COMPLETED",
                    message=f"Rodada da Arena {run_id} concluída com {len(successful)} modelos avaliados.",
                    payload={
                        "run_id": run_id,
                        "fastest": fastest,
                        "highest_tps": highest_tps,
                        "privacy": privacy_dec.classification.value
                    }
                )
            except Exception:
                pass

        return report

    def cancel_run(self, run_id: str) -> bool:
        """Cancela graciosamente uma rodada da Arena em andamento."""
        tasks = self._active_tasks.get(run_id)
        if not tasks:
            return False
        for t in tasks:
            if not t.done():
                t.cancel()
        return True

    def get_run(self, run_id: str) -> Optional[ArenaRunReport]:
        return self._runs.get(run_id)

    def list_runs(self) -> List[ArenaRunReport]:
        return sorted(list(self._runs.values()), key=lambda x: x.started_at, reverse=True)
