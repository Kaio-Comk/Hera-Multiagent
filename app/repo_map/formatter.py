"""Formatador de Repo-Map compacto respeitando limites estritos de tokens.

Gera o esqueleto do projeto formatado para injeção no prompt do LLM
sem exceder a janela de contexto de modelos locais leves.
"""

from typing import Dict, Any, List

class RepoMapFormatter:
    """Formata o mapa de símbolos em texto compacto sob orçamento rígido de tokens."""

    def __init__(self, max_token_budget: int = 600):
        self.max_token_budget = max_token_budget
        # Estimativa heurística conservadora: 1 token ≈ 4 caracteres
        self.max_char_budget = max_token_budget * 4

    def format_map(self, scanned_workspace: Dict[str, Dict[str, Any]]) -> str:
        """Gera o esqueleto em texto estruturado."""
        if not scanned_workspace:
            return ""

        output_lines: List[str] = ["=== MAPA ESTRUTURAL DO REPOSITÓRIO (REPO-MAP) ==="]
        current_chars = len(output_lines[0])
        omitted_files = 0

        for file_path, symbols in sorted(scanned_workspace.items()):
            classes = symbols.get("classes", [])
            functions = symbols.get("functions", [])

            # Ignorar arquivos vazios sem classes ou funções
            if not classes and not functions:
                continue

            file_block = [f"\n{file_path}:"]
            for cls in classes:
                methods_str = ", ".join([m["name"] for m in cls.get("methods", [])[:5]])
                if len(cls.get("methods", [])) > 5:
                    methods_str += ", ..."
                file_block.append(f"  class {cls['name']}({methods_str})")

            for fn in functions[:6]:
                args_str = ", ".join(fn.get("args", [])[:4])
                file_block.append(f"  def {fn['name']}({args_str})")
            if len(functions) > 6:
                file_block.append("  def ...")

            block_text = "\n".join(file_block)
            if current_chars + len(block_text) > self.max_char_budget:
                omitted_files += 1
                continue

            output_lines.append(block_text)
            current_chars += len(block_text)

        if omitted_files > 0:
            output_lines.append(f"\n[... {omitted_files} arquivos adicionais omitidos para respeitar o limite de contexto ...]")

        output_lines.append("==================================================")
        return "\n".join(output_lines)
