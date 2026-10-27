"""Redutor de contexto deterministico para o Hera (Anthropic CMA - Plan Big, Execute Small).

Executa varredura de AST, extracao de simbolos, resumo de git diff e parse de falhas
de testes sem consumir tokens de LLM. Garante que 95% do volume bruto de I/O seja
reduzido deterministicamente antes de alimentar o modelo de fronteira.
"""
from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


class ContextReducer:
    """Ferramentas deterministicas locais de reducao de contexto."""

    @staticmethod
    def scan_python_ast(filepath: str | Path) -> Dict[str, Any]:
        """Extrai classes, metodos, funcoes e imports de um arquivo Python via AST nativo.
        Custo: 0 tokens, execucao em milissegundos.
        """
        p = Path(filepath)
        if not p.is_file():
            return {"error": f"Arquivo nao encontrado: {p}"}

        try:
            content = p.read_text(encoding="utf-8", errors="replace")
            tree = ast.parse(content, filename=str(p))
        except Exception as exc:
            return {"error": f"Erro de sintaxe ao parsear AST: {exc}"}

        classes = []
        functions = []
        imports = []

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
                doc = ast.get_docstring(node) or ""
                classes.append({
                    "name": node.name,
                    "line": node.lineno,
                    "methods": methods,
                    "doc": doc.split("\n")[0][:80] if doc else ""
                })
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(node) or ""
                functions.append({
                    "name": node.name,
                    "line": node.lineno,
                    "is_async": isinstance(node, ast.AsyncFunctionDef),
                    "doc": doc.split("\n")[0][:80] if doc else ""
                })
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    imports.append(f"{mod}.{alias.name}")

        return {
            "file": p.name,
            "path": str(p),
            "total_lines": len(content.splitlines()),
            "classes": classes,
            "functions": functions,
            "imports": sorted(list(set(imports)))[:30],
        }

    @staticmethod
    def summarize_test_output(output: str, max_chars: int = 1500) -> str:
        """Extrai falhas, excecoes e linhas exatas de logs do pytest/unittest sem ruido."""
        if not output:
            return "(sem saida de testes)"

        lines = output.splitlines()
        failures = []
        short_summary = []
        in_failure_block = False
        current_failure = []

        for line in lines:
            line_stripped = line.strip()
            # Detectar resumo pytest
            if "=== FAILURES ===" in line or "=== ERRORS ===" in line:
                in_failure_block = True
                continue
            if in_failure_block and line.startswith("___"):
                if current_failure:
                    failures.append("\n".join(current_failure[:8]))
                    current_failure = []
                current_failure.append(line_stripped)
            elif in_failure_block and line.startswith("E   "):
                current_failure.append(line_stripped)
            elif line_stripped.startswith("FAILED ") or line_stripped.startswith("ERROR "):
                short_summary.append(line_stripped)

        if current_failure:
            failures.append("\n".join(current_failure[:8]))

        res = []
        if short_summary:
            res.append("Resumo de Falhas:")
            res.extend(short_summary[:10])
        if failures:
            res.append("\nDetalhes dos Erros:")
            res.extend(failures[:5])

        result_text = "\n".join(res) if res else "\n".join(lines[-25:])
        return result_text[:max_chars]

    @staticmethod
    def summarize_git_diff(diff_text: str, max_lines: int = 60) -> str:
        """Resume um git diff mostrando arquivos afetados e linhas alteradas essenciais."""
        if not diff_text or not diff_text.strip():
            return "(nenhuma alteracao detectada)"

        lines = diff_text.splitlines()
        summary_lines = []
        changed_files = []
        insertions = 0
        deletions = 0

        for line in lines:
            if line.startswith("diff --git"):
                parts = line.split(" ")
                if len(parts) >= 4:
                    changed_files.append(parts[2].replace("a/", ""))
            elif line.startswith("+") and not line.startswith("+++"):
                insertions += 1
            elif line.startswith("-") and not line.startswith("---"):
                deletions += 1

        summary_lines.append(f"Arquivos alterados ({len(changed_files)}): {', '.join(changed_files[:10])}")
        summary_lines.append(f"Modificacoes: +{insertions} / -{deletions} linhas")

        # Incluir amostra das linhas modificadas
        snippet = []
        for line in lines:
            if line.startswith("@@") or (line.startswith("+") and not line.startswith("+++")) or (line.startswith("-") and not line.startswith("---")):
                snippet.append(line)
                if len(snippet) >= max_lines:
                    snippet.append("[...diff truncado para economia de tokens]")
                    break

        if snippet:
            summary_lines.append("\nDiff Amostral:")
            summary_lines.extend(snippet)

        return "\n".join(summary_lines)

    @staticmethod
    def reduce_step_output_for_synthesis(step_result: Dict[str, Any], max_chars: int = 1200) -> str:
        """Garante que a saida de um passo nao consuma a cota do modelo de fronteira.
        Mantem apenas status, diffs, evidencias e conclusao.
        """
        sid = step_result.get("id", "")
        title = step_result.get("title", "")
        status = step_result.get("status", "")
        engine = step_result.get("engine", "")
        summary = step_result.get("summary", "")
        output_text = step_result.get("output_text", "")
        artifacts = step_result.get("artifacts", [])

        # O RESULTADO do passo vem primeiro. Ate 27/10/2026 a ordem era `summary` antes de `output_text`, mas o
        # `summary` dos motores e uma linha de status ("concluiu a tarefa com sucesso em 15s"): o resumo final
        # recebia so isso e perdia o conteudo; e o `error` dos passos que falharam nem entrava.
        error = step_result.get("error", "")
        body = (output_text or summary or error or "(sem saida)").strip()
        if len(body) > max_chars:
            body = body[:max_chars] + "\n[...saida cortada]"

        art_text = f" | Artefatos: {', '.join(artifacts)}" if artifacts else ""
        return f"Passo {sid} ({title}) [{status}] via {engine}{art_text}:\n{body}"
