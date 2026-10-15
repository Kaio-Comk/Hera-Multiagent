"""Ponte MCP stdio para o REA (Reverse Engineer Anything, https://github.com/morluto/rea).

POR QUE EXISTE: o REA é um pacote npm, e a política de MCP do Hera só lança scripts `.py` dentro do projeto
(`app/mcp/launch.py`) e proíbe `npx`/`npm`. Em vez de afrouxar a allowlist, este script é o ÚNICO executável
autorizado: ele roda o REA instalado localmente em `tools/rea/` (versão fixada em package.json, instalada com
`--ignore-scripts`), sem `npx` e sem baixar nada na hora. O stdio é herdado, então o protocolo MCP passa direto.

Registro (feito por um humano, via API/UI do MCP, com `enable_mcp` ligado):
    transport=stdio, command=python, args=["tools/mcp_rea_bridge.py"]
As ferramentas do REA nascem DESLIGADAS e com HITL (ver `McpCatalog.register_discovered_tools`).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REA_DIR = Path(__file__).resolve().parent / "rea"
REA_ENTRY = REA_DIR / "node_modules" / "rea-agents" / "scripts" / "rea.mjs"


def build_command() -> list[str]:
    node = shutil.which("node")
    if not node:
        raise SystemExit("node não encontrado no PATH (o REA exige Node.js 22.19+, 24.11+ ou 26+).")
    if not REA_ENTRY.is_file():
        raise SystemExit(f"REA não instalado em {REA_DIR}. Rode: npm install --prefix tools/rea --ignore-scripts")
    return [node, str(REA_ENTRY), "mcp"]


def main() -> int:
    try:
        return subprocess.call(build_command())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
