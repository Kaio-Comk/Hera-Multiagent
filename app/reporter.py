"""Gerador deterministico de Relatorios HTML Autocontidos e Offline para o Hera.

Produz relatorios estaticos em HTML com CSS e graficos vetoriais SVG 100% inline
(zero dependencias externas ou CDNs). Garante auditoria financeira e tecnica
mesmo sem conexao a internet.
"""
from __future__ import annotations

import html
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional


class RunReporter:
    """Compilador de relatorios offline autocontidos."""

    @staticmethod
    def generate_html(run_data: Dict[str, Any]) -> str:
        run_id = html.escape(str(run_data.get("id", "run-desconhecido")))
        status = str(run_data.get("status", "UNKNOWN")).upper()
        created_at_epoch = run_data.get("created_at", time.time())
        created_at_str = datetime.fromtimestamp(created_at_epoch).strftime("%d/%m/%Y %H:%M:%S")

        steps = run_data.get("steps", [])
        total_tokens = sum(int(s.get("tokens", 0) or 0) for s in steps)
        total_duration = sum(float(s.get("duration", 0) or 0) for s in steps)

        # Taxa de cambio fixa ou mockada para auditoria
        usd_rate = 5.75
        # Custo aproximado baseado em tier (USD 0.002 por 1k tokens medio)
        cost_usd = (total_tokens / 1000.0) * 0.002
        cost_brl = cost_usd * usd_rate

        status_color = "#10b981" if status == "SUCCEEDED" else "#ef4444" if status == "FAILED" else "#f59e0b"

        # Calcular distribuicao de tokens por motor
        engine_tokens: Dict[str, int] = {}
        for s in steps:
            eng = s.get("engine", "desconhecido")
            tok = int(s.get("tokens", 0) or 0)
            engine_tokens[eng] = engine_tokens.get(eng, 0) + tok

        # Gerar SVG simples de barras
        svg_bars = []
        max_tok = max(engine_tokens.values()) if engine_tokens and max(engine_tokens.values()) > 0 else 1
        y_offset = 25
        for eng, tok in engine_tokens.items():
            pct = int((tok / max_tok) * 260)
            svg_bars.append(f"""
              <text x="10" y="{y_offset + 14}" fill="#94a3b8" font-size="12" font-family="sans-serif">{html.escape(eng)} ({tok:,} tok)</text>
              <rect x="200" y="{y_offset}" width="{pct}" height="18" rx="4" fill="#19b8a4" />
            """)
            y_offset += 32

        svg_content = f"""
        <svg width="500" height="{max(80, y_offset + 10)}" viewBox="0 0 500 {max(80, y_offset + 10)}" xmlns="http://www.w3.org/2000/svg">
          {''.join(svg_bars)}
        </svg>
        """

        # Construir lista de passos
        step_items = []
        for s in steps:
            sid = html.escape(str(s.get("id", "")))
            stitle = html.escape(str(s.get("title", s.get("name", sid))))
            sstatus = str(s.get("status", "UNKNOWN")).upper()
            sengine = html.escape(str(s.get("engine", "")))
            smodel = html.escape(str(s.get("model", "")))
            stoks = int(s.get("tokens", 0) or 0)
            sdur = float(s.get("duration", 0) or 0)
            summary = html.escape(str(s.get("summary", "") or s.get("output", "") or ""))

            scolor = "#10b981" if sstatus == "SUCCEEDED" else "#ef4444" if sstatus == "FAILED" else "#64748b"

            step_items.append(f"""
            <div class="step-card">
              <div class="step-header">
                <div>
                  <span class="step-id">{sid}</span>
                  <span class="step-title">{stitle}</span>
                </div>
                <div>
                  <span class="badge" style="background: {scolor}20; color: {scolor}; border: 1px solid {scolor}60;">{sstatus}</span>
                </div>
              </div>
              <div class="step-meta">
                <span><strong>Motor:</strong> {sengine} {f'({smodel})' if smodel else ''}</span>
                <span><strong>Tokens:</strong> {stoks:,}</span>
                <span><strong>Tempo:</strong> {sdur:.1f}s</span>
              </div>
              {f'''
              <details class="step-details">
                <summary>Ver saida / sumario</summary>
                <pre class="step-output">{summary}</pre>
              </details>
              ''' if summary else ''}
            </div>
            """)

        html_out = f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
  <meta charset="UTF-8">
  <title>Relatório de Execução — {run_id}</title>
  <style>
    :root {{
      --bg: #090d16;
      --card: #111827;
      --border: #1f2937;
      --text: #f3f4f6;
      --text-muted: #94a3b8;
      --primary: #19b8a4;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      padding: 32px 20px;
      line-height: 1.5;
    }}
    .container {{
      max-width: 900px;
      margin: 0 auto;
    }}
    .header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 24px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 32px;
    }}
    .header h1 {{
      font-size: 24px;
      font-weight: 700;
      color: #fff;
    }}
    .header .subtitle {{
      color: var(--text-muted);
      font-size: 14px;
      margin-top: 4px;
    }}
    .badge {{
      display: inline-block;
      padding: 4px 10px;
      border-radius: 9999px;
      font-size: 12px;
      font-weight: 600;
      text-transform: uppercase;
    }}
    .grid-kpis {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 16px;
      margin-bottom: 32px;
    }}
    .kpi-card {{
      background: var(--card);
      border: 1px solid var(--border);
      padding: 20px;
      border-radius: 12px;
    }}
    .kpi-title {{
      font-size: 12px;
      color: var(--text-muted);
      text-transform: uppercase;
      font-weight: 600;
      margin-bottom: 8px;
    }}
    .kpi-value {{
      font-size: 24px;
      font-weight: 700;
      color: #fff;
    }}
    .section-card {{
      background: var(--card);
      border: 1px solid var(--border);
      padding: 24px;
      border-radius: 12px;
      margin-bottom: 32px;
    }}
    .section-card h2 {{
      font-size: 18px;
      font-weight: 600;
      margin-bottom: 16px;
      color: #fff;
    }}
    .step-card {{
      background: #1e293b50;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 12px;
    }}
    .step-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
    }}
    .step-id {{
      background: #334155;
      color: #cbd5e1;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 12px;
      font-family: monospace;
      margin-right: 8px;
    }}
    .step-title {{
      font-weight: 600;
      font-size: 15px;
    }}
    .step-meta {{
      font-size: 13px;
      color: var(--text-muted);
      display: flex;
      gap: 16px;
    }}
    .step-details {{
      margin-top: 12px;
      border-top: 1px solid var(--border);
      padding-top: 8px;
    }}
    .step-details summary {{
      cursor: pointer;
      font-size: 13px;
      color: var(--primary);
      user-select: none;
    }}
    .step-output {{
      background: #0f172a;
      border: 1px solid #1e293b;
      padding: 12px;
      border-radius: 6px;
      margin-top: 8px;
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      font-size: 12px;
      color: #e2e8f0;
      white-space: pre-wrap;
      word-break: break-all;
      max-height: 350px;
      overflow-y: auto;
    }}
    .footer {{
      text-align: center;
      color: var(--text-muted);
      font-size: 13px;
      margin-top: 48px;
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div>
        <h1>Hera Multiagent — Relatório de Execução</h1>
        <div class="subtitle">Run ID: <code>{run_id}</code> • Iniciado em {created_at_str}</div>
      </div>
      <div>
        <span class="badge" style="background: {status_color}25; color: {status_color}; border: 1px solid {status_color}; font-size: 14px; padding: 6px 14px;">
          {status}
        </span>
      </div>
    </div>

    <div class="grid-kpis">
      <div class="kpi-card">
        <div class="kpi-title">Passos Concluídos</div>
        <div class="kpi-value">{len(steps)}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-title">Duração Acumulada</div>
        <div class="kpi-value">{total_duration:.1f}s</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-title">Volume de Tokens</div>
        <div class="kpi-value">{total_tokens:,}</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-title">Custo Estimado</div>
        <div class="kpi-value">US$ {cost_usd:.4f} <span style="font-size: 14px; color: var(--text-muted); font-weight: normal;">(R$ {cost_brl:.2f})</span></div>
      </div>
    </div>

    <div class="section-card">
      <h2>Distribuição de Tokens por Motor</h2>
      {svg_content}
    </div>

    <div class="section-card">
      <h2>Passos do Grafo (DAG)</h2>
      {''.join(step_items) if step_items else '<p style="color: var(--text-muted);">Nenhum passo executado.</p>'}
    </div>

    <div class="footer">
      Relatório estático offline gerado pelo motor Hera Multiagent • 100% Autocontido sem CDNs.
    </div>
  </div>
</body>
</html>
"""
        return html_out

    @staticmethod
    def save_report(run_data: Dict[str, Any], output_path: Optional[str] = None) -> str:
        """Compila e salva o relatório em disco."""
        run_id = str(run_data.get("id", f"run-{int(time.time())}"))
        if not output_path:
            from app.paths import diretorio_relatorios
            reports_dir = diretorio_relatorios()
            reports_dir.mkdir(parents=True, exist_ok=True)
            output_path = str(reports_dir / f"{run_id}.html")

        html_content = RunReporter.generate_html(run_data)
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html_content, encoding="utf-8")
        return str(out.resolve())
