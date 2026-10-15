"""Atualiza o catálogo de provedores do Hera a partir do registro do 9Router (https://github.com/decolua/9router, MIT).

Só DADOS PÚBLICOS entram (endereço, nome, link de cadastro, nomes de modelo). Nada de código, de client id/secret de OAuth
nem de provedores que exigem imitar outro cliente. Uso:

    # 1) despeje o registro (Node, sem rede nem escrita):
    node --permission --allow-fs-read=<pasta do script e do 9router> tools/dump_9router_registry.mjs <pasta 9router> > registry.json
    # 2) regenere o catálogo:
    python tools/sync_9router_catalog.py registry.json

Escreve `app/gateway/presets_extra.py` (provedores novos + atualização de modelos dos já existentes).
Depois rode "Buscar modelos" e "Testar" no painel: o catálogo NÃO é testado ao vivo por este script.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.gateway.presets import BASE_PRESETS, EXTRA_PRESETS  # noqa: E402

OUT = ROOT / "app" / "gateway" / "presets_extra.py"
MAX_MODELS = 12


def openai_url(x: dict) -> str:
    """Endereço OpenAI-compatível (`.../chat/completions`) do provedor: o principal ou um transporte alternativo SEM gancho especial."""
    top = x.get("baseUrl") or ""
    if x.get("format") == "openai" and top.endswith("/chat/completions") and not x.get("hasExecutor") and not x.get("hooks"):
        return top
    for t in x.get("transports", []):
        if t.get("format") == "openai" and t.get("baseUrl", "").endswith("/chat/completions") and not t.get("hooks"):
            return t["baseUrl"]
    return ""


def importable(x: dict) -> bool:
    """Só provedores usáveis com CHAVE DE API pelo adaptador OpenAI do Hera (sem OAuth de terceiros, sem executor próprio)."""
    url = openai_url(x)
    api_key_mode = x.get("category") in ("apikey", "freeTier") or "apikey" in x.get("authModes", [])
    # provedores de login (OAuth) só entram pelo modo de chave; nenhum entra se exigir cabeçalhos que imitam outro cliente (User-Agent/X-IDE-*).
    spoof = any(h.lower() in ("user-agent", "x-ide-type", "x-ide-name", "x-product") for h in x.get("headers", []))
    return (bool(url) and not spoof and api_key_mode and not x.get("noAuth") and not x.get("deprecated")
            and "{" not in url and "<" not in url and any(m["kind"] == "llm" for m in x.get("models", [])))


def llm_models(x: dict) -> list[str]:
    seen: list[str] = []
    for m in x["models"]:
        if m["kind"] == "llm" and m["id"] not in seen and not re.search(r"[\x00-\x1f\x7f]", m["id"]):
            seen.append(m["id"])
    return seen[:MAX_MODELS]


def key_env_for(pid: str, old: dict) -> str:
    return old.get("key_env") or re.sub(r"[^A-Z0-9]+", "_", pid.upper()).strip("_") + "_API_KEY"


def main(path: str) -> None:
    data = json.load(open(path, encoding="utf-8-sig"))
    base = {p["id"]: p for p in BASE_PRESETS}
    old_extra = {p["id"]: p for p in EXTRA_PRESETS}
    extras, overlay = [], {}
    used_urls = {p["base_url"].rstrip("/") for p in BASE_PRESETS}          # o catálogo exige um endereço por provedor
    for x in data:
        if not importable(x):
            continue
        models = llm_models(x)
        if x["id"] in base:
            b = base[x["id"]]
            overlay[x["id"]] = {"default_model": b["default_model"] if b["default_model"] in models else models[0], "models": models}
            continue
        url = openai_url(x)[: -len("/chat/completions")].rstrip("/")
        if url in used_urls:                                               # mesmo endereço de outro provedor: não duplica
            continue
        used_urls.add(url)
        old = old_extra.get(x["id"], {})
        free = x.get("category") == "freeTier" or x.get("hasFree")
        extras.append({
            "id": x["id"], "label": x.get("name") or x["id"], "tier": "cloud_free" if free else "cloud_cheap",
            "base_url": url, "key_env": key_env_for(x["id"], old),
            "default_model": models[0], "models": models, "signup": x.get("apiKeyUrl") or x.get("website") or "",
            "note": "Camada gratuita; confira os limites no site do provedor." if free else "Cobrança por uso, com a sua chave.",
        })
    extras.sort(key=lambda p: p["id"])
    header = OUT.read_text(encoding="utf-8").split("EXTRA_PRESETS:")[0].rstrip() + "\n\n"
    body = "EXTRA_PRESETS: List[Dict[str, Any]] = [\n" + "".join("    " + json.dumps(p, ensure_ascii=False) + ",\n" for p in extras) + "]\n\n"
    body += ("# Modelos atualizados dos provedores que já existem em `presets.py` (id -> default_model/models), do registro do 9Router.\n"
             "MODEL_OVERLAY: Dict[str, Dict[str, Any]] = {\n" + "".join(f"    {k!r}: {json.dumps(v, ensure_ascii=False)},\n" for k, v in sorted(overlay.items())) + "}\n")
    OUT.write_text(header + body, encoding="utf-8")
    print(f"{len(extras)} provedores extras, {len(overlay)} presets com modelos atualizados -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1])
