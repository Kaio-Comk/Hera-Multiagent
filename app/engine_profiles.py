"""Perfil dos motores e dos modelos, para o planejador ESCOLHER bem (motor e modelo de cada passo).

Sem isso o planejador so via os nomes dos motores e escolhia o que conhecia (o proprio Claude Code, para tudo).
Aqui cada motor ganha uma descricao honesta (o que faz bem, se mexe em arquivos, se e local/pago) e cada modelo uma
classificacao `leve | equilibrado | forte`, deduzida do NOME (nao ha como medir a qualidade de verdade; e uma heuristica
que o usuario pode corrigir na tela antes de executar).
"""
from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Optional

LEVE, EQUILIBRADO, FORTE = "leve", "equilibrado", "forte"
STRENGTH_ORDER = {LEVE: 0, EQUILIBRADO: 1, FORTE: 2}

ENGINE_PROFILES: Dict[str, Dict[str, Any]] = {
    "ollama": {
        "label": "Ollama (local)", "files": False, "local": True, "cost": "gratis",
        "about": "Modelos pequenos rodando na GPU do usuario: gratis, privado e rapido para o que e simples. NAO acessa "
                 "arquivos nem terminal. Bom para rascunhos, resumos curtos, reescrita, classificacao e perguntas diretas; "
                 "fraco em raciocinio longo ou codigo complexo.",
    },
    "claude_code": {
        "label": "Claude Code", "files": True, "local": False, "cost": "assinatura",
        "about": "Agente de codigo da Anthropic COM acesso a arquivos e terminal do projeto. Forte para ler e entender "
                 "codigo, refatorar, corrigir bugs, rodar testes e analisar logs. Consome a assinatura do usuario: "
                 "prefira um modelo leve para o que e simples.",
    },
    "antigravity": {
        "label": "Antigravity", "files": True, "local": False, "cost": "assinatura",
        "about": "Agente com acesso ao workspace (arquivos e terminal) que oferece varios modelos (Gemini, Claude, GPT-OSS). "
                 "Bom para varrer muitos arquivos, tarefas longas de edicao e para ter uma segunda opiniao de outra familia.",
    },
    "hera_router": {
        "label": "Roteador Hera (nuvem)", "files": False, "local": False, "cost": "varia",
        "about": "Chat em modelos de nuvem dos provedores configurados (com fallback entre eles). NAO acessa arquivos nem "
                 "terminal: so recebe texto e devolve texto. Bom para pesquisa, comparacoes, textos, planejamento e "
                 "revisoes de algo que ja foi colado na instrucao.",
    },
}


def classify_model(engine: str, model: str) -> str:
    """leve | equilibrado | forte, pelo nome do modelo (heuristica)."""
    name = (model or "").lower()
    if not name or name == "default":
        return EQUILIBRADO
    if engine == "ollama":
        size = re.search(r"(\d+(?:\.\d+)?)\s*b\b", name)
        if size:
            b = float(size.group(1))
            return LEVE if b <= 4 else EQUILIBRADO if b <= 14 else FORTE
        return LEVE
    if re.search(r"haiku|(?<![a-z])(?:mini|nano|lite)(?![a-z])|instant|small|turbo|8b\b|:free", name):
        return LEVE
    if "flash" in name:                                   # Flash: leve; com esforco medio/alto sobe para equilibrado
        return LEVE if name.endswith("-low") else EQUILIBRADO
    if re.search(r"opus|fable|\bpro\b|-pro|large|max|reason|r1\b|405b|70b|ultra|gpt-5|o1|o3", name):
        return FORTE
    if name.endswith("-high"):                            # sufixo de esforco do Antigravity
        return FORTE
    if name.endswith("-low"):
        return LEVE
    return EQUILIBRADO


def describe_models(engine: str, models: Iterable[str]) -> List[Dict[str, str]]:
    return [{"id": m, "strength": classify_model(engine, m)} for m in models]


def build_catalog(engines: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`engines`: nome -> {"models": [...], "default": "modelo padrao"}. Devolve o catalogo que o planejador recebe."""
    catalog = []
    for name, info in engines.items():
        profile = ENGINE_PROFILES.get(name, {"label": name, "files": False, "local": False, "cost": "?", "about": ""})
        models = [m for m in dict.fromkeys(info.get("models") or []) if m and m != "default"][:30]
        catalog.append({"engine": name, "label": profile["label"], "files": profile["files"], "local": profile["local"],
                        "cost": profile["cost"], "about": profile["about"], "default": info.get("default") or "",
                        "models": describe_models(name, models)})
    return catalog


def catalog_text(catalog: List[Dict[str, Any]]) -> str:
    """O catalogo em texto, para colar no pedido do planejador."""
    blocks = []
    for item in catalog:
        models = ", ".join(f"{m['id']} ({m['strength']})" for m in item["models"]) or "(use o modelo padrao)"
        blocks.append(
            f"- MOTOR \"{item['engine']}\" ({item['label']}): {item['about']}\n"
            f"    acesso a arquivos/terminal: {'SIM' if item['files'] else 'NAO'} | roda local: {'SIM' if item['local'] else 'NAO'} | custo: {item['cost']}\n"
            f"    modelos: {models}")
    return "\n".join(blocks)


def strongest_first(models: List[Dict[str, str]]) -> List[Dict[str, str]]:
    return sorted(models, key=lambda m: -STRENGTH_ORDER.get(m["strength"], 1))


def pick_model(catalog: List[Dict[str, Any]], engine: str, strength: str) -> Optional[str]:
    """Um modelo do motor com a forca pedida (ou o mais proximo). None se o motor nao lista modelos."""
    for item in catalog:
        if item["engine"] != engine or not item["models"]:
            continue
        want = STRENGTH_ORDER.get(strength, 1)
        return min(item["models"], key=lambda m: abs(STRENGTH_ORDER.get(m["strength"], 1) - want))["id"]
    return None
