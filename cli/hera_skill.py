"""Hera Universal Skill & MCP Server (Anthropic CMA / alethe-orchestrate-skill).

Permite invocar o Hera via CLI direta (/hera setup, /hera orchestrate, /hera report)
ou como um servidor de ferramentas MCP (Model Context Protocol) compativel com
Claude Code, Antigravity, Cursor e Codex.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from typing import Any, Dict, Optional


HERA_BASE_URL = os.environ.get("HERA_URL", "http://127.0.0.1:8000")


def _get_token() -> str:
    """Obtem o token do Hera a partir do ambiente ou do arquivo local data/.hera_token."""
    token = os.environ.get("HERA_API_KEY", "")
    if token:
        return token
    token_file = Path(__file__).resolve().parent.parent / "data" / ".hera_token"
    if token_file.is_file():
        return token_file.read_text(encoding="utf-8").strip()
    return ""


def _http_request(endpoint: str, method: str = "GET", data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{HERA_BASE_URL.rstrip('/')}/{endpoint.lstrip('/')}"
    headers = {"Content-Type": "application/json"}
    token = _get_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        try:
            err_json = json.loads(err_body)
            raise RuntimeError(f"HTTP {e.code}: {err_json.get('message', err_body)}")
        except Exception:
            raise RuntimeError(f"HTTP {e.code}: {err_body}")
    except Exception as e:
        raise RuntimeError(f"Falha de conexao com o Hera ({HERA_BASE_URL}): {e}")


def cmd_setup() -> Dict[str, Any]:
    """Valida a saude do servidor Hera, motores locais e conectividade."""
    results = {"hera_online": False, "ollama_online": False, "engines": []}
    try:
        res = _http_request("/healthz")
        results["hera_online"] = True
        results["version"] = res.get("version", "1.0.0")
    except Exception as e:
        results["hera_error"] = str(e)

    # Checar Ollama local
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags")
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            results["ollama_online"] = True
            results["ollama_models"] = [m.get("name") for m in data.get("models", [])][:8]
    except Exception:
        results["ollama_online"] = False

    # Checar motores configurados no Hera
    if results["hera_online"]:
        try:
            orch = _http_request("/api/orchestration")
            results["engines"] = orch.get("active_engines", [])
            results["offline_mode"] = orch.get("offline", False)
        except Exception:
            pass

    return results


def cmd_orchestrate(prompt: str, strategy: str = "balanced", max_steps: int = 6) -> Dict[str, Any]:
    """Dispara uma tarefa composta no Hera sob a estrategia especificada."""
    payload = {
        "goal": prompt,
        "strategy": strategy,
        "max_steps": max_steps
    }
    # Tenta usar o endpoint do plano
    res = _http_request("/api/orchestration/plan", method="POST", data=payload)
    return res


def cmd_report(run_id: str) -> Dict[str, Any]:
    """Gera e localiza o relatorio HTML offline de uma execucao."""
    events_res = _http_request(f"/api/console/runs/{run_id}")
    events = events_res.get("events", [])
    if not events:
        raise RuntimeError(f"Nenhum evento encontrado para o run {run_id}")

    from app.reporter import RunReporter
    steps_dict = {}
    run_status = "SUCCEEDED"
    for e in events:
        s_id = e.get("step")
        if s_id:
            if s_id not in steps_dict:
                steps_dict[s_id] = {
                    "id": s_id,
                    "title": e.get("title", s_id),
                    "engine": e.get("engine", ""),
                    "model": e.get("model", ""),
                    "tokens": 0,
                    "duration": 0.0,
                    "status": "RUNNING"
                }
            if e.get("kind") == "step_end":
                steps_dict[s_id]["status"] = e.get("status", "SUCCEEDED")
                steps_dict[s_id]["tokens"] = e.get("tokens") or 0
                steps_dict[s_id]["duration"] = e.get("duration") or 0.0
                if e.get("status") == "FAILED":
                    run_status = "FAILED"
            if e.get("kind") == "text":
                steps_dict[s_id]["output"] = (steps_dict[s_id].get("output", "") + e.get("text", ""))[-2000:]

    run_data = {
        "id": run_id,
        "status": run_status,
        "created_at": events[0].get("ts") if events else time.time(),
        "steps": list(steps_dict.values())
    }
    report_path = RunReporter.save_report(run_data)
    total_tokens = sum(s.get("tokens", 0) for s in run_data["steps"])

    return {
        "run_id": run_id,
        "status": run_status,
        "steps_count": len(run_data["steps"]),
        "total_tokens": total_tokens,
        "report_file": report_path,
        "file_url": f"file:///{report_path.replace(os.sep, '/')}"
    }


def run_mcp_server() -> None:
    """Implementa o servidor MCP basico via stdio (JSON-RPC 2.0)."""
    tools = [
        {
            "name": "hera_setup",
            "description": "Verifica a integridade e conectividade do Hera e dos motores de IA locais (Ollama) e remotos.",
            "inputSchema": {
                "type": "object",
                "properties": {},
            }
        },
        {
            "name": "hera_orchestrate",
            "description": "Orquestra uma tarefa multiagente de engenharia de software usando o Hera sob o padrao Plan Big, Execute Small.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Objetivo em linguagem natural a ser executado"},
                    "strategy": {"type": "string", "enum": ["savings", "balanced", "aggressive_quality", "cascade"], "default": "balanced"},
                    "max_steps": {"type": "integer", "default": 6}
                },
                "required": ["prompt"]
            }
        },
        {
            "name": "hera_report",
            "description": "Gera e exporta um relatorio HTML estatico offline com evidencias, auditoria e custos de um run.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "ID da execucao (ex: run-f08678e9)"}
                },
                "required": ["run_id"]
            }
        }
    ]

    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            req = json.loads(line)
            req_id = req.get("id")
            method = req.get("method")

            if method == "tools/list":
                resp = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}
            elif method == "tools/call":
                params = req.get("params", {})
                name = params.get("name")
                args = params.get("arguments", {})

                if name == "hera_setup":
                    res = cmd_setup()
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(res, indent=2)}]}}
                elif name == "hera_orchestrate":
                    res = cmd_orchestrate(args.get("prompt", ""), args.get("strategy", "balanced"), args.get("max_steps", 6))
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(res, indent=2)}]}}
                elif name == "hera_report":
                    res = cmd_report(args.get("run_id", ""))
                    resp = {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(res, indent=2)}]}}
                else:
                    resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Ferramenta desconhecida: {name}"}}
            elif method == "initialize":
                resp = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "hera-orchestrate-mcp", "version": "1.0.0"},
                        "capabilities": {"tools": {}}
                    }
                }
            else:
                resp = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"Metodo nao suportado: {method}"}}

            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
        except Exception as e:
            err_resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32603, "message": str(e)}}
            sys.stdout.write(json.dumps(err_resp) + "\n")
            sys.stdout.flush()


def main() -> None:
    parser = argparse.ArgumentParser(description="Hera Universal Skill & MCP Tool")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("setup", help="Verificar ambiente e conexao com Hera e Ollama")

    orch_p = sub.add_parser("orchestrate", help="Disparar plano multiagente")
    orch_p.add_argument("prompt", type=str, help="Objetivo a ser executado")
    orch_p.add_argument("--strategy", type=str, default="balanced", choices=["savings", "balanced", "aggressive_quality", "cascade"])
    orch_p.add_argument("--max-steps", type=int, default=6)

    rep_p = sub.add_parser("report", help="Gerar relatorio HTML de uma execucao")
    rep_p.add_argument("run_id", type=str, help="ID da execucao")

    sub.add_parser("mcp", help="Iniciar servidor MCP StdIO para agentes hospedeiros")

    args = parser.parse_args()

    if args.command == "setup":
        r = cmd_setup()
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif args.command == "orchestrate":
        r = cmd_orchestrate(args.prompt, args.strategy, args.max_steps)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif args.command == "report":
        r = cmd_report(args.run_id)
        print(f"📊 Relatório compilado com sucesso!")
        print(f"   Status: {r['status']} | Passos: {r['steps_count']} | Tokens: {r['total_tokens']:,}")
        print(f"   Arquivo: {r['report_file']}")
        print(f"   URL: {r['file_url']}")
    elif args.command == "mcp":
        run_mcp_server()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
