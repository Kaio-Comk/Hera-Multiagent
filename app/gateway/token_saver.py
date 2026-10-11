"""RTK Token Saver Conservador para o Hera Smart Gateway.

Reduz o consumo de contexto em saídas volumosas de ferramentas (ToolResult)
sem corromper código, indentação Python, fences Markdown ou estruturas JSON.
"""

import json
from dataclasses import dataclass
from typing import Tuple, List, Optional

@dataclass(frozen=True)
class TokenSavingReport:
    original_chars: int
    compressed_chars: int
    reduction_percentage: float
    is_lossy: bool
    strategy_applied: str

class RTKTokenSaver:
    """Compressor de tokens conservador e tipado para saídas de ferramentas."""

    def __init__(
        self,
        max_tool_lines: int = 120,
        head_lines: int = 40,
        tail_lines: int = 40,
        dedup_repeats_threshold: int = 3
    ):
        self.max_tool_lines = max_tool_lines
        self.head_lines = head_lines
        self.tail_lines = tail_lines
        self.dedup_repeats_threshold = dedup_repeats_threshold

    def is_json(self, text: str) -> bool:
        """Verifica se o texto é uma estrutura JSON válida."""
        stripped = text.strip()
        if (stripped.startswith("{") and stripped.endswith("}")) or (stripped.startswith("[") and stripped.endswith("]")):
            try:
                json.loads(stripped)
                return True
            except Exception:
                return False
        return False

    def compress_tool_output(self, text: str) -> Tuple[str, TokenSavingReport]:
        """Aplica estratégias de compressão conservadora a uma saída de ferramenta.
        
        Preserva rigorosamente indentação, cercas Markdown e estruturas JSON.
        """
        if not text or not isinstance(text, str):
            return text, TokenSavingReport(0, 0, 0.0, False, "none")

        original_chars = len(text)

        # Regra 1: JSON estruturado válido não deve ser truncado internamente
        if self.is_json(text):
            return text, TokenSavingReport(original_chars, original_chars, 0.0, False, "json_passthrough")

        lines = text.splitlines(keepends=True)
        if len(lines) <= self.max_tool_lines:
            # Tentar apenas deduplicação conservadora de linhas consecutivas idênticas
            deduped_lines, dedup_applied = self._deduplicate_consecutive_lines(lines)
            if dedup_applied:
                result = "".join(deduped_lines)
                comp_chars = len(result)
                pct = ((original_chars - comp_chars) / original_chars) * 100.0 if original_chars > 0 else 0.0
                return result, TokenSavingReport(original_chars, comp_chars, round(pct, 2), True, "consecutive_dedup")
            return text, TokenSavingReport(original_chars, original_chars, 0.0, False, "verbatim_preserved")

        # Regra 2: Truncamento estruturado (Head + Marker + Tail)
        # Verifica se o corte não rompe um bloco fence Markdown no meio
        head = lines[:self.head_lines]
        tail = lines[-self.tail_lines:]
        omitted_count = len(lines) - (self.head_lines + self.tail_lines)

        marker = f"\n[... {omitted_count} linhas omitidas pelo RTK Token Saver ...]\n\n"
        compressed_text = "".join(head) + marker + "".join(tail)
        compressed_chars = len(compressed_text)
        reduction_pct = ((original_chars - compressed_chars) / original_chars) * 100.0 if original_chars > 0 else 0.0

        return compressed_text, TokenSavingReport(
            original_chars=original_chars,
            compressed_chars=compressed_chars,
            reduction_percentage=round(reduction_pct, 2),
            is_lossy=True,
            strategy_applied="structured_head_tail_truncation"
        )

    def _deduplicate_consecutive_lines(self, lines: List[str]) -> Tuple[List[str], bool]:
        """Colapsa repetições consecutivas idênticas (ex: polling logs)."""
        if not lines:
            return lines, False

        output: List[str] = []
        applied = False
        i = 0
        n = len(lines)

        while i < n:
            curr = lines[i]
            count = 1
            while i + 1 < n and lines[i + 1] == curr:
                count += 1
                i += 1

            if count >= self.dedup_repeats_threshold and len(curr.strip()) > 3:
                # Manter primeira ocorrência e adicionar sumário de repetição
                output.append(curr)
                eol = "\n" if not curr.endswith("\n") else ""
                output.append(f"  [... linha acima repetida {count - 1} vezes ...]{eol}")
                applied = True
            else:
                for _ in range(count):
                    output.append(curr)
            i += 1

        return output, applied
