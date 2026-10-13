"""A UI nao depende de CDN: tudo sai de /static (publico, so leitura), com CSP restritiva."""
import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from helpers import AUTH_HEADERS, TEST_BASE_URL

WEB = Path(server.WEB_DIR)


def anon():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL)


def test_index_nao_usa_nenhum_recurso_externo():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert re.findall(r'(?:src|href)="(?:https?:)?//[^"]+"', html) == []              # nenhum script/css de fora
    assert "cdn.tailwindcss.com" not in html and "tailwind.config" not in html and "@import url(" not in html
    for ref in re.findall(r'(?:src|href)="(/static/[^"]+)"', html):
        assert (WEB / ref.lstrip("/")).is_file(), ref                                 # tudo o que a pagina pede existe


def test_arquivos_gerados_existem_e_tem_conteudo():
    css = (WEB / "static" / "tailwind.css").read_text(encoding="utf-8")
    assert len(css) > 10_000 and ".bg-brand-500" in css and "2xl\:grid-cols-2" in css
    fonts = (WEB / "static" / "fonts.css").read_text(encoding="utf-8")
    for ref in re.findall(r"url\('(/static/fonts/[^']+)'\)", fonts):
        assert (WEB / ref.lstrip("/")).is_file(), ref
    for lib in ("lucide.min.js", "marked.min.js", "purify.min.js"):
        assert (WEB / "static" / "vendor" / lib).stat().st_size > 5_000


@pytest.mark.asyncio
async def test_static_e_publico_mas_so_leitura_e_sem_escapar():
    async with anon() as c:
        css = await c.get("/static/tailwind.css")
        assert css.status_code == 200 and "text/css" in css.headers["content-type"]
        assert (await c.get("/static/fonts/inter-latin-400-normal.woff2")).status_code == 200
        assert (await c.post("/static/tailwind.css")).status_code == 401              # so GET/HEAD sao publicos
        for evil in ("/static/%2e%2e/config.yaml", "/static/..%2fconfig.yaml", "/static/../../config.yaml", "/static/%2e%2e/app/server.py"):
            assert (await c.get(evil)).status_code in (400, 401, 404), evil
        assert (await c.get("/static/vendor/nao-existe.js")).status_code == 404
        assert (await c.get("/api/config")).status_code == 401                        # o resto continua fechado
        assert (await c.get("/favicon.ico")).status_code == 204


@pytest.mark.asyncio
async def test_pagina_vai_com_csp_restritiva():
    async with anon() as c:
        r = await c.get("/")
    csp = r.headers["content-security-policy"]
    assert "default-src 'self'" in csp and "object-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "http://" not in csp.replace("ws://", "") and "https:" not in csp and "*.com" not in csp
