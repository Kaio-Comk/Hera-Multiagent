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

import os
import shutil
import subprocess
import sys
from pathlib import Path

REA_DIR = Path(__file__).resolve().parent / "rea"
REA_ENTRY = REA_DIR / "node_modules" / "rea-agents" / "scripts" / "rea.mjs"
VENDOR_DIR = REA_DIR / "vendor"
DEFAULT_JAVA_HOME = Path(r"B:\Program Files\android-build\jdk-17.0.20.1+1")
DEFAULT_ADB_DIR = Path(r"B:\Program Files\android-build\sdk\platform-tools")
DEFAULT_JADX_JAR = VENDOR_DIR / "jadx-headless-mcp-0.7.1-all.jar"
DEFAULT_APKTOOL_EXE = VENDOR_DIR / "apktool.exe"


def build_env() -> dict[str, str]:
    env = os.environ.copy()
    if not env.get("JAVA_HOME") and DEFAULT_JAVA_HOME.is_dir():
        env["JAVA_HOME"] = str(DEFAULT_JAVA_HOME)
    if not env.get("REA_JADX_MCP_JAR") and DEFAULT_JADX_JAR.is_file():
        env["REA_JADX_MCP_JAR"] = str(DEFAULT_JADX_JAR)
    if not env.get("REA_APKTOOL_COMMAND") and DEFAULT_APKTOOL_EXE.is_file():
        env["REA_APKTOOL_COMMAND"] = str(DEFAULT_APKTOOL_EXE)

    extra_paths = [
        str(DEFAULT_JAVA_HOME / "bin"),
        str(DEFAULT_ADB_DIR),
        str(VENDOR_DIR),
    ]
    current_path = env.get("PATH", "")
    for p in reversed(extra_paths):
        if Path(p).is_dir() and p.lower() not in current_path.lower():
            current_path = f"{p};{current_path}"
    env["PATH"] = current_path
    return env


def build_command() -> list[str]:
    node = shutil.which("node")
    if not node:
        raise SystemExit("node não encontrado no PATH (o REA exige Node.js 22.19+, 24.11+ ou 26+).")
    if not REA_ENTRY.is_file():
        raise SystemExit(f"REA não instalado em {REA_DIR}. Rode: npm install --prefix tools/rea --ignore-scripts")
    return [node, str(REA_ENTRY), "mcp"]


def main() -> int:
    try:
        return subprocess.call(build_command(), env=build_env())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
