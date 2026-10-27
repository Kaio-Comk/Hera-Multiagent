"""Marca e interface do Hera ("Pavão & Noite"): ativos, tokens, contraste, navegação e cache dos estáticos."""
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
BRAND = WEB / "static" / "brand"
INDEX = (WEB / "index.html").read_text(encoding="utf-8")
CSS = (WEB / "static" / "hera-ui.css").read_text(encoding="utf-8")
DESIGN = (ROOT / "DESIGN.md").read_text(encoding="utf-8")
TAILWIND_CFG = (ROOT / "tools" / "ui" / "tailwind.config.js").read_text(encoding="utf-8")


def _lum(hex_color: str) -> float:
    r, g, b = (int(hex_color.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4))
    lin = lambda c: c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4          # noqa: E731
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def token(name: str) -> str:
    m = re.search(rf"--{name}:\s*(#[0-9a-fA-F]{{6}})", CSS)
    assert m, name
    return m.group(1).lower()


# ── ativos da marca ──

@pytest.mark.parametrize("name", ["hera-mark.svg", "hera-logo.svg", "hera-logo-light.svg", "hera-mark-mono.svg", "favicon.svg"])
def test_logos_sao_svg_validos_e_acessiveis(name):
    root = ET.fromstring((BRAND / name).read_text(encoding="utf-8"))
    assert root.tag.endswith("svg") and root.attrib.get("viewBox")
    assert root.attrib.get("role") == "img" and root.attrib.get("aria-label") == "Hera"
    assert any(c.tag.endswith("title") for c in root)
    assert "<script" not in (BRAND / name).read_text(encoding="utf-8").lower()            # SVG sem script


def test_logo_tem_o_olho_dourado_e_o_h():
    mark = (BRAND / "hera-mark.svg").read_text(encoding="utf-8")
    assert "#e0a93f" in mark and "stroke-linecap=\"round\"" in mark                       # ouro do olho; traços do H
    assert (BRAND / "favicon.svg").read_text(encoding="utf-8") == mark


def test_pagina_usa_favicon_e_marca_sem_coroa_nem_laranja():
    assert '/static/brand/favicon.png' in INDEX and '/static/brand/hera-mark-192.png' in INDEX
    assert "👑" not in INDEX and "#e56a4a" not in INDEX.lower() and "#e56a4a" not in CSS.lower()
    assert "👑" not in (ROOT / "main.py").read_text(encoding="utf-8")
    assert "<title>Hera" in INDEX


# ── tokens e DESIGN.md ──

def test_hex_da_tabela_do_design_md_existe_no_css_ou_no_tailwind():
    section = DESIGN.split("## 4. Paleta e papéis")[1].split("## 5.")[0]
    hexes = {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}", section)}
    known = {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}", CSS + TAILWIND_CFG)}
    assert hexes and hexes <= known, sorted(hexes - known)


def test_paleta_do_tailwind_segue_os_tokens():
    for name, hex_ in (("hera-bg", "#0a0f15"), ("hera-surface", "#0f1620"), ("hera-brand-500", "#19b8a4"),
                       ("hera-brand-ink", "#04201c"), ("hera-gold-500", "#e0a93f")):
        assert token(name) == hex_
    assert "500: '#19b8a4'" in TAILWIND_CFG and "ink: '#04201c'" in TAILWIND_CFG and "950: '#070b10'" in TAILWIND_CFG


def test_contraste_atende_wcag_aa():
    bg, surface = token("hera-bg"), token("hera-surface")
    assert contrast(token("hera-brand-ink"), token("hera-brand-500")) >= 4.5            # texto do botão primário
    assert contrast(token("hera-text"), surface) >= 7
    assert contrast(token("hera-text-2"), surface) >= 4.5
    assert contrast(token("hera-text-3"), surface) >= 4.5                               # dicas
    assert contrast(token("hera-text-3"), bg) >= 4.5
    assert contrast(token("hera-brand-300"), surface) >= 4.5                           # links
    assert contrast("#ffffff", token("hera-brand-500")) < 4.5                           # por isso o texto é tinta, não branco


def test_botao_primario_nao_usa_branco_sobre_teal():
    for run in re.findall(r"[^\"'`<>\n]*\bbg-brand-(?:500|600)\b[^\"'`<>\n]*", INDEX):
        assert "text-white" not in run, run.strip()


# ── casca e navegação ──

def test_cada_item_do_menu_tem_secao_e_metadados():
    tabs = re.findall(r'id="tab-btn-([a-z]+)"', INDEX)
    assert tabs[0] == "home" and len(tabs) == len(set(tabs)) >= 9
    assert "studio" not in tabs and "tab-studio" not in INDEX and "Task Studio" not in INDEX     # removido a pedido do Kaio
    meta = INDEX[INDEX.index("const TAB_META"):INDEX.index("function toggleSidebar")]
    for tab in tabs:
        assert f'id="tab-{tab}"' in INDEX, tab
        assert re.search(rf"\b{tab}:\s*{{ title:", meta), tab
    assert INDEX.count('class="page-title"') == 1 and INDEX.count("<h1") == 1             # um título só, na barra superior


def test_acessibilidade_basica_da_casca():
    assert 'class="skip-link"' in INDEX and 'id="main"' in INDEX
    assert 'aria-label="Navegação principal"' in INDEX and 'aria-controls="sidebar"' in INDEX
    assert "aria-current" in INDEX and "prefers-reduced-motion" in CSS and ":focus-visible" in CSS
    assert 'lang="pt-BR"' in INDEX


def test_css_da_marca_vem_antes_do_tailwind():
    """Os utilitários (hidden, md:inline-flex...) precisam vencer os empates com `.chip`/`.btn`."""
    assert INDEX.index("/static/hera-ui.css") < INDEX.index("/static/tailwind.css")
    assert INDEX.count("/static/hera-ui.css") == 1 and "<style>" not in INDEX


def test_sem_hex_solto_novo_no_html():
    body = INDEX[INDEX.index("</head>"):]
    allowed = {"#f59e0b", "#10b981", "#ef4444", "#60a5fa", "#34d399", "#c084fc", "#f87171", "#19b8a4", "#2d3f56",
               "#cfdae6", "#71869d", "#8396ab", "#ffffff"}
    found = {h.lower() for h in re.findall(r"#[0-9a-fA-F]{6}\b", body)}
    assert found <= allowed, sorted(found - allowed)


# ── cache dos estáticos ──

def test_servidor_versiona_os_estaticos_pelo_mtime(tmp_path, monkeypatch):
    import app.server as server
    (tmp_path / "static").mkdir()
    css = tmp_path / "static" / "a.css"
    css.write_text("x")
    monkeypatch.setattr(server, "WEB_DIR", tmp_path)
    html = '<link href="/static/a.css"><script src="/static/a.css"></script><img src="/static/brand/x.svg"><link href="/static/ausente.css">'
    out = server._version_static_assets(html)
    assert f'href="/static/a.css?v={int(css.stat().st_mtime)}"' in out
    assert "/static/brand/x.svg\"" in out and "ausente.css\"" in out                  # só css/js existentes ganham versão


@pytest.mark.asyncio
async def test_pagina_servida_leva_versao_e_continua_sem_cache():
    from httpx import ASGITransport, AsyncClient
    import app.server as server
    from helpers import AUTH_HEADERS, TEST_BASE_URL
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS) as c:
        r = await c.get("/")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert re.search(r'/static/tailwind\.css\?v=\d+', r.text) and re.search(r'/static/hera-ui\.css\?v=\d+', r.text)
    assert "window.HERA_TOKEN" in r.text


@pytest.mark.asyncio
async def test_endpoint_de_saude_checa_os_motores_em_paralelo(monkeypatch):
    import asyncio
    import time
    import app.server as server
    from httpx import ASGITransport, AsyncClient
    from helpers import AUTH_HEADERS, TEST_BASE_URL
    from models.agent import HealthStatus

    class Slow:
        async def health_check(self):
            await asyncio.sleep(0.4)
            return HealthStatus(healthy=True, version="1", available_models=[])

    monkeypatch.setattr(server, "adapters", {"a": Slow(), "b": Slow(), "c": Slow()})
    t = time.perf_counter()
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS) as c:
        r = await c.get("/api/health")
    assert r.status_code == 200 and set(r.json()["engines"]) == {"a", "b", "c"}
    assert time.perf_counter() - t < 1.0                                               # em série seriam ~1,2 s


# Toda interpolação `${...}` dos blocos novos da UI (Conexões, Início e catálogo de provedores) foi lida e é segura: ou passa por
# escapeHtml/escAttr/encodeURIComponent, ou é número/texto fixo do próprio código, ou é um fragmento de HTML montado só com
# partes escapadas. Uma interpolação NOVA quebra este teste de propósito: revise contra XSS e só então acrescente aqui.
REVISADAS = {
    # CLIs oficiais no painel: `cmd` é constante do próprio código; `ce.version` passa por escapeHtml (e.line), como `ag.version`.
    'ce.version',
    'cmd',
    # Roteador: várias contas, cabeçalhos extras e cota (revisado: texto do servidor só passa por escapeHtml; i = índice numérico;
    # k/v montam TEXTO puro que depois entra escapado em escapeHtml(hdrText)).
    'escapeHtml(h)',
    'escapeHtml(hdrText)',
    'i',
    'i + 1',
    'k',
    'v',
    'quota ? `<div class="text-[11px] text-sky-300">${escapeHtml(quota)}</div>` : ""',
    'stored ? `<div class="pv-meta" title="Contas guardadas no cofre (só os 4 últimos caracteres)">${stored}</div>` : ""',
    '(ol.available_models || []).length',
    '(p.models || []).length',
    'Math.round(p.expires_in / 60)',
    'PV_TIER[e.tier][0]',
    'PV_TIER[e.tier][1]',
    'a.mode ? ` data-mode="${escAttr(a.mode)}"` : ""',
    'ag.version',
    'all.length',
    'ca.account || "conta Claude"',
    'ca.subscription || ca.method || ""',
    'cls',
    'days.map(d => `<span title="${escAttr(d.day)}: ${d.requests} chamada(s)" style="height:${Math.max(6, Math.round(d.requests / max * 100))}%"></span>`).join("")',
    'e.actions.map(pvBtn).join("")',
    'e.detail ? `<div class="pv-detail">${e.detail}</div>` : ""',
    'e.extra || ""',
    'e.line ? `<div class="pv-line">${escapeHtml(e.line)}</div>` : ""',
    'e.ready ? " is-ready" : ""',
    'e.status[0]',
    'encodeURIComponent(id)',
    'encodeURIComponent(name)',
    'encodeURIComponent(r.provider.name)',
    'engines.length',
    'escAttr(a.act)',
    'escAttr(a.id)',
    'escAttr(e.id)',
    'escAttr(n)',
    'escAttr(p.name)',
    'escAttr(p.signup)',
    'escAttr(t)',
    'escapeHtml(OAUTH_GRANTS[p.grant] || p.grant)',
    'escapeHtml(String(Math.round(p.expires_in / 60)))',
    'escapeHtml(String(data.latency_ms))',
    'escapeHtml(String(label).trim().charAt(0).toUpperCase() || "?")',
    'escapeHtml(a.label)',
    'escapeHtml(base + path)',
    'escapeHtml(data.provider)',
    'escapeHtml(days[0].day.slice(5))',
    'escapeHtml(days[days.length - 1].day.slice(5))',
    'escapeHtml(e.default_model || "padrão do motor")',
    'escapeHtml(e.label)',
    'escapeHtml(e.message)',
    'escapeHtml(e.note)',
    'escapeHtml(e.status[1])',
    'escapeHtml(hmWhen(t.created_at))',
    'escapeHtml(k.id)',
    'escapeHtml(k.state)',
    'escapeHtml(keys || "—")',
    'escapeHtml(label)',
    'escapeHtml(name)',
    'escapeHtml(p.name)',
    'escapeHtml(r.snippet || "")',
    'escapeHtml(r.user_code)',
    'escapeHtml(t.assigned_agent || "—")',
    'escapeHtml(t.assigned_model || "modelo padrão")',
    'escapeHtml(t.title || "(sem título)")',
    'escapeHtml(use)',
    'field',
    'h',
    'id',
    'k.cooldown_remaining_s ? " " + k.cooldown_remaining_s + "s" : ""',
    'keys ? `<div class="pv-meta">${keys}</div>` : ""',
    'kind',
    'label',
    'link',
    'link ? `Abra <a class="text-brand-300 underline" target="_blank" rel="noopener noreferrer" href="${escAttr(link)}">${escapeHtml(link)}</a>` : "Abra o endereço de verificação do provedor"',
    'm',
    'n',
    'name',
    'num("daily_requests", "Req. por dia", "sem limite")',
    'num("daily_tokens", "Tokens por dia", "sem limite")',
    'num("priority", "Prioridade (menor = antes)", "100")',
    'num("rpm", "Req. por minuto", "sem limite")',
    'p.connected ? "Reconectar" : "Conectar"',
    'p.connected ? `<button data-oauth="disconnect" data-name="${escAttr(p.name)}" class="px-3 py-1.5 bg-zinc-800 hover:bg-zinc-700 text-white rounded-lg text-xs font-semibold">Desconectar</button>` : ""',
    'p.default_model || ""',
    'p.has_refresh ? " (renova sozinho)" : ""',
    'p.health.resume_in_s',
    'p.keys_env',
    'p.keys_stored',
    'p.keys_stored ? `<button type="button" onclick="clearRouterKeys(\'${n}\')" class="px-2 py-1 text-rose-300 hover:text-rose-200 text-xs">Apagar chaves</button>` : ""',
    'p.name',
    'p[field] && !(field === "priority" && p[field] === 100) ? p[field] : ""',
    'paused ? `<div class="text-[11px] text-amber-300">Pausado após falhas seguidas. Último erro: ${escapeHtml(p.health.last_error || "-")} <button type="button" onclick="resetBreaker(\'${n}\')" class="underline ml-1">tentar de novo agora</button></div>` : ""',
    'ph',
    'pr.default_model',
    'pr.signup ? `<div class="text-[11px] text-zinc-400">Crie a chave em <a class="text-brand-300 underline" target="_blank" rel="noopener noreferrer" href="${escAttr(pr.signup)}">${escapeHtml(pr.signup)}</a>. Ela fica no cofre do Hera, protegida pelo Windows.</div>` : ""',
    'pvAvatar(e.label, e.id)',
    'pvColor(seed)',
    'ready',
    'ready.length',
    'rows',
    'safe ? `<a class="text-brand-300 underline" target="_blank" rel="noopener noreferrer" href="${escAttr(safe)}">${title}</a>` : `<span class="text-zinc-300">${title}</span>`',
    'state',
    'state[0]',
    'state[1]',
    'tier',
    'totals.success_rate',
    'where',
}


def test_interpolacoes_de_html_da_ui_foram_revisadas():
    block = INDEX[INDEX.index("// ---------- Conexões"):INDEX.rindex("</body>")]
    found = {m.group(1).strip() for m in re.finditer(r"\$\{((?:[^{}]|\{[^{}]*\})*)\}", block)}
    novas = sorted(found - REVISADAS)
    assert not novas, f"interpolação nova em HTML da UI sem revisão de XSS: {novas}"
    # e nenhuma delas injeta dado de fora sem escape: dados do servidor/IdP só aparecem dentro de escapeHtml/escAttr
    for expr in found:
        for raw in re.findall(r"(?:p|d|e|t|r|data)\.(?:name|title|snippet|message|default_model|assigned_agent|assigned_model|user_code|provider|url|signup)",
                              re.sub(r"(?:escapeHtml|escAttr)\([^)]*\)", "", expr)):
            pytest.fail(f"dado externo fora de escape: {raw} em {expr!r}")


def test_links_e_destinos_do_catalogo_sao_https_e_sem_combos():
    from app.gateway.presets import PROVIDER_PRESETS
    assert PROVIDER_PRESETS and all(p["signup"].startswith("https://") and p["base_url"].startswith("https://") for p in PROVIDER_PRESETS)
    assert [p["id"] for p in PROVIDER_PRESETS if p.get("login")] == ["openrouter"]
    assert "combo" not in INDEX.lower().replace('combos: "apelidos"', "")           # a tela nao fala mais em combos
    assert 'data-sub="apelidos"' in INDEX and 'data-sub="combos"' not in INDEX
    assert INDEX.index('data-sub="providers"') < INDEX.index('data-sub="overview"')  # catalogo e a 1a sub-aba do Roteador


def test_plano_e_conversa_com_projetos_e_imagens():
    for needle in ('id="chat-side-list"', 'id="chat-thread"', 'id="chat-text"', 'id="chat-file"', 'accept="image/png,image/jpeg,image/gif,image/webp"',
                   "chatNewProject()", "chatSend()", "chatAddFiles"):
        assert needle in INDEX, needle
    chat = INDEX[INDEX.index("// ==================== PLANO EM CONVERSA"):INDEX.index("// ==================== ROTEADOR EMBUTIDO")]
    assert "innerHTML" not in chat and "insertAdjacentHTML" not in chat and "document.write" not in chat   # texto do servidor so por textContent
