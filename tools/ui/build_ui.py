"""Gera os arquivos estaticos da UI (CSS do Tailwind, Lucide, marked, DOMPurify e fontes) em web/static/.

A UI nao usa mais CDN: tudo sai do proprio servidor. Os arquivos gerados ficam no repositorio, entao so e preciso rodar
este script quando mudar uma classe do Tailwind em web/index.html ou para atualizar uma biblioteca:

    python tools/ui/build_ui.py          (precisa de Node.js/npm; baixa as versoes fixas abaixo numa pasta temporaria)
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
STATIC = ROOT / "web" / "static"
PACKAGES = ["tailwindcss@3.4.17", "lucide@0.468.0", "marked@12.0.2", "dompurify@3.1.6",
            "@fontsource/inter@5.1.0", "@fontsource/jetbrains-mono@5.1.0"]
FONTS = {"inter": ("400", "500", "600", "700", "800"), "jetbrains-mono": ("400", "600")}


def run(cmd: list[str], cwd: Path) -> None:
    print("$", " ".join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)  # nosec B603 - comandos fixos deste script, sem shell


def main() -> None:
    work = Path(tempfile.mkdtemp(prefix="hera-ui-build-"))
    try:
        npm = shutil.which("npm")
        if not npm:
            sys.exit("Node.js/npm nao encontrado: instale em https://nodejs.org")
        run([npm, "init", "-y"], work)
        run([npm, "i", "--no-audit", "--no-fund", *PACKAGES], work)
        (STATIC / "vendor").mkdir(parents=True, exist_ok=True)
        (STATIC / "fonts").mkdir(parents=True, exist_ok=True)
        nm = work / "node_modules"
        tailwind = nm / ".bin" / ("tailwindcss.cmd" if sys.platform == "win32" else "tailwindcss")
        run([str(tailwind), "-c", str(ROOT / "tools/ui/tailwind.config.js"),
             "-i", str(ROOT / "tools/ui/input.css"), "-o", str(STATIC / "tailwind.css"), "--minify"], ROOT)
        shutil.copy(nm / "lucide/dist/umd/lucide.min.js", STATIC / "vendor/lucide.min.js")
        shutil.copy(nm / "marked/marked.min.js", STATIC / "vendor/marked.min.js")
        shutil.copy(nm / "dompurify/dist/purify.min.js", STATIC / "vendor/purify.min.js")
        for family, weights in FONTS.items():
            for weight in weights:
                name = f"{family}-latin-{weight}-normal.woff2"
                shutil.copy(nm / "@fontsource" / family / "files" / name, STATIC / "fonts" / name)
        print("Pronto: web/static/ atualizado.")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
