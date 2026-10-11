import time
import httpx
import shutil
import subprocess
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field

class BenchmarkResult(BaseModel):
    """Resultado detalhado do benchmark de inferência local."""
    model: str
    prompt: str
    tokens_generated: int = 0
    tokens_per_second: float = 0.0
    eval_duration_sec: float = 0.0
    prompt_eval_tokens: int = 0
    prompt_eval_duration_sec: float = 0.0
    total_duration_sec: float = 0.0
    gpu_name: Optional[str] = None
    vram_used_mb: Optional[int] = None
    vram_total_mb: Optional[int] = None
    sample_response: str = ""

class LocalBenchmark:
    """Benchmark determinístico para medição de throughput e latência na GPU local."""

    def __init__(self, ollama_url: str = "http://127.0.0.1:11434"):
        self.ollama_url = ollama_url.rstrip("/")

    def get_gpu_vram_info(self) -> Dict[str, Any]:
        """Obtém informações de VRAM diretamente via nvidia-smi se disponível."""
        if not shutil.which("nvidia-smi"):
            return {}

        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total,memory.used,memory.free", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if res.returncode == 0 and res.stdout.strip():
                parts = [p.strip() for p in res.stdout.strip().split(",")]
                if len(parts) >= 3:
                    return {
                        "gpu_name": parts[0],
                        "vram_total_mb": int(parts[1]),
                        "vram_used_mb": int(parts[2]),
                        "vram_free_mb": int(parts[3]) if len(parts) > 3 else 0
                    }
        except Exception:
            pass
        return {}

    async def run_benchmark(
        self,
        model: str = "qwen2.5-coder:1.5b",
        prompt: str = "Escreva uma implementação concisa de quicksort em Python com type hints.",
        max_tokens: int = 120
    ) -> BenchmarkResult:
        """Executa geração única no Ollama e calcula métricas reais de hardware."""
        gpu_info = self.get_gpu_vram_info()

        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": 0.2
            }
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(f"{self.ollama_url}/api/generate", json=payload)
            resp.raise_for_status()
            data = resp.json()

        eval_count = data.get("eval_count", 0)
        eval_duration_ns = data.get("eval_duration", 0)
        prompt_eval_count = data.get("prompt_eval_count", 0)
        prompt_eval_duration_ns = data.get("prompt_eval_duration", 0)
        total_duration_ns = data.get("total_duration", 0)

        eval_duration_sec = eval_duration_ns / 1e9 if eval_duration_ns else 0.0
        prompt_eval_duration_sec = prompt_eval_duration_ns / 1e9 if prompt_eval_duration_ns else 0.0
        total_duration_sec = total_duration_ns / 1e9 if total_duration_ns else 0.0

        tps = (eval_count / eval_duration_sec) if eval_duration_sec > 0 else 0.0

        # Atualizar leitura de VRAM durante/pós geração
        updated_gpu = self.get_gpu_vram_info() or gpu_info

        return BenchmarkResult(
            model=model,
            prompt=prompt,
            tokens_generated=eval_count,
            tokens_per_second=round(tps, 2),
            eval_duration_sec=round(eval_duration_sec, 3),
            prompt_eval_tokens=prompt_eval_count,
            prompt_eval_duration_sec=round(prompt_eval_duration_sec, 3),
            total_duration_sec=round(total_duration_sec, 3),
            gpu_name=updated_gpu.get("gpu_name"),
            vram_used_mb=updated_gpu.get("vram_used_mb"),
            vram_total_mb=updated_gpu.get("vram_total_mb"),
            sample_response=data.get("response", "").strip()[:200]
        )
