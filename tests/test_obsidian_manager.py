"""Obsidian dentro do Hera: caminhos seguros, escrita atomica com deteccao de conflito, lixeira, renomear com
links, busca, tags, backlinks, nota diaria, imagens e a API."""
import json
import os
import sys

import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from app.vault_manager import MAX_NOTE_BYTES, VaultConflict, VaultError, VaultManager
from models.agent import HealthStatus, RunHandle
from models.task import AgentResult, TaskStatus
from helpers import AUTH_HEADERS, TEST_BASE_URL


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "Cofre"
    (root / ".obsidian").mkdir(parents=True)
    (root / ".obsidian" / "app.json").write_text("{}", encoding="utf-8")
    (root / "Projetos").mkdir()
    (root / "Projetos" / "Alfa.md").write_text("# Alfa\nVeja [[Beta]] e [[Projetos/Gama|a gama]].\n#projeto #ideia\n", encoding="utf-8")
    (root / "Beta.md").write_text("---\ntags: [clientes, vip]\n---\n# Beta\nVolta para [[Alfa]]. Texto sobre banana.\n", encoding="utf-8")
    (root / "Projetos" / "Gama.md").write_text("Sem links. #ideia\n```\n[[Alfa]] #naoetag\n```\n", encoding="utf-8")
    (root / "Pasta Vazia").mkdir()
    return VaultManager(root)


# --------------------------------------------------------------------------- caminhos
@pytest.mark.parametrize("bad", [
    "../fora.md", "a/../../fora.md", "/etc/passwd.md", "C:/Windows/x.md", "a:b.md", "..\\fora.md",
    ".obsidian/plugins/x.md", ".git/config.md", ".trash/x.md", "CON.md", "nul", "aux.md", "pasta/com2.md",
    "termina com ponto.md.", "x" * 200 + ".md", "a\x00b.md", "", "   ", "a/" * 20 + "x.md",
    "caractere<invalido>.md", 'aspas".md',
])
def test_caminhos_perigosos_sao_recusados(vault, bad):
    with pytest.raises(VaultError):
        vault.safe(bad)


def test_so_md_e_editavel(vault):
    with pytest.raises(VaultError):
        vault.write("script.js", "alert(1)")
    with pytest.raises(VaultError):
        vault.read("Projetos")


def test_link_simbolico_para_fora_e_recusado(vault, tmp_path):
    outside = tmp_path / "fora"
    outside.mkdir()
    (outside / "segredo.md").write_text("segredo", encoding="utf-8")
    try:
        os.symlink(outside, vault.root / "atalho", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("sem permissao para criar link simbolico")
    with pytest.raises(VaultError) as exc:
        vault.read("atalho/segredo.md")
    assert exc.value.status == 403


def test_pastas_ocultas_nao_aparecem(vault):
    tree = json.dumps(vault.tree())
    assert ".obsidian" not in tree and "app.json" not in tree
    assert all(".obsidian" not in r["path"] for r in vault.search("json"))


# --------------------------------------------------------------------------- ler/escrever
def test_criar_ler_editar_e_hash(vault):
    created = vault.create("Nova/Ideia", "# Ideia\ncorpo")
    assert created["path"] == "Nova/Ideia.md"
    note = vault.read("Nova/Ideia.md")
    assert note["content"] == "# Ideia\ncorpo" and note["hash"] == created["hash"]
    with pytest.raises(VaultError) as exc:
        vault.create("Nova/Ideia.md", "outra")
    assert exc.value.status == 409
    saved = vault.write("Nova/Ideia.md", "# Ideia\ncorpo editado", base_hash=note["hash"])
    assert vault.read("Nova/Ideia.md")["content"].endswith("editado") and saved["hash"] != note["hash"]
    assert not list(vault.root.rglob("*.hera-tmp"))                      # escrita atomica nao deixa lixo


def test_conflito_quando_a_nota_mudou_no_disco(vault):
    note = vault.read("Beta.md")
    (vault.root / "Beta.md").write_text("editada no Obsidian", encoding="utf-8")        # alguem editou por fora
    with pytest.raises(VaultConflict) as exc:
        vault.write("Beta.md", "minha versao", base_hash=note["hash"])
    assert exc.value.status == 409 and exc.value.current_hash
    assert (vault.root / "Beta.md").read_text(encoding="utf-8") == "editada no Obsidian"   # nada foi sobrescrito
    vault.write("Beta.md", "minha versao", base_hash=note["hash"], force=True)             # decisao explicita
    assert vault.read("Beta.md")["content"] == "minha versao"


def test_preserva_quebra_de_linha_crlf(vault):
    (vault.root / "Windows.md").write_bytes(b"linha1\r\nlinha2\r\n")
    note = vault.read("Windows.md")
    assert note["crlf"] is True and "\r" not in note["content"]
    vault.write("Windows.md", "linha1\nlinha2\nlinha3\n", base_hash=note["hash"])
    assert (vault.root / "Windows.md").read_bytes() == b"linha1\r\nlinha2\r\nlinha3\r\n"


def test_limites_e_nao_utf8(vault):
    with pytest.raises(VaultError) as exc:
        vault.create("Grande.md", "x" * (MAX_NOTE_BYTES + 1))
    assert exc.value.status == 413
    (vault.root / "Binaria.md").write_bytes(b"\xff\xfe\x00\x01")
    with pytest.raises(VaultError) as exc:
        vault.read("Binaria.md")
    assert exc.value.status == 415


def test_excluir_move_para_a_lixeira(vault):
    out = vault.trash("Beta.md")
    assert out["to"].startswith(".trash/Beta-") and (vault.root / out["to"]).exists()
    assert not (vault.root / "Beta.md").exists()
    assert all(r["path"] != "Beta.md" for r in vault.search("banana"))
    with pytest.raises(VaultError):
        vault.read(out["to"])                                            # a lixeira e invisivel pela API


# --------------------------------------------------------------------------- renomear com links
def test_renomear_atualiza_links_e_respeita_blocos_de_codigo(vault):
    (vault.root / "Projetos" / "Alfa.md").write_text(
        "Veja [[Beta]], [[Beta|apelido]], [[Beta#Titulo]], ![[Beta]] e [[Gama]].\n```\n[[Beta]]\n```\n", encoding="utf-8")
    preview = vault.rename("Beta.md", "Renomeada.md", dry_run=True)
    assert preview["dry_run"] and {n["path"] for n in preview["notes_to_update"]} == {"Projetos/Alfa.md"}
    assert (vault.root / "Beta.md").exists()                              # dry-run nao mexe em nada
    result = vault.rename("Beta.md", "Renomeada.md")
    assert not (vault.root / "Beta.md").exists() and (vault.root / "Renomeada.md").exists()
    text = (vault.root / "Projetos" / "Alfa.md").read_text(encoding="utf-8")
    assert "[[Renomeada]]" in text and "[[Renomeada|apelido]]" in text and "[[Renomeada#Titulo]]" in text
    assert "![[Renomeada]]" in text and "[[Gama]]" in text
    assert "```\n[[Beta]]\n```" in text                                    # codigo intacto
    assert result["notes_updated"][0]["links"] == 4
    assert vault.read("Renomeada.md")["backlinks"] == ["Projetos/Alfa.md"]


def test_renomear_nao_sobrescreve_e_pode_mover_de_pasta(vault):
    with pytest.raises(VaultError) as exc:
        vault.rename("Beta.md", "Projetos/Alfa.md")
    assert exc.value.status == 409
    vault.rename("Beta.md", "Arquivo/Beta.md")
    assert (vault.root / "Arquivo" / "Beta.md").exists()
    assert "[[Beta]]" in (vault.root / "Projetos" / "Alfa.md").read_text(encoding="utf-8")   # mesmo nome: link segue valendo


# --------------------------------------------------------------------------- busca, tags, backlinks, grafo
def test_backlinks_links_e_tags(vault):
    alfa = vault.read("Projetos/Alfa.md")
    assert {o["target"]: o["path"] for o in alfa["outgoing"]} == {"Beta": "Beta.md", "Projetos/Gama": "Projetos/Gama.md"}
    assert alfa["backlinks"] == ["Beta.md"]
    assert set(alfa["tags"]) == {"projeto", "ideia"}
    assert set(vault.read("Beta.md")["tags"]) == {"clientes", "vip"}                  # tags do frontmatter
    assert vault.tags("ideia") == ["Projetos/Alfa.md", "Projetos/Gama.md"]
    assert "naoetag" not in {t["tag"] for t in vault.tags()}                           # tag dentro de codigo nao conta
    assert {n["title"] for n in vault.graph()["nodes"]} >= {"Alfa", "Beta"}


def test_busca_pontua_titulo_e_mostra_trecho(vault):
    results = vault.search("banana")
    assert [r["path"] for r in results] == ["Beta.md"] and "banana" in results[0]["snippet"]
    assert vault.search("alfa beta")[0]["path"] in {"Projetos/Alfa.md", "Beta.md"}
    assert vault.search("   ") == [] and vault.search("palavraqueNaoExiste") == []


def test_nota_diaria_usa_a_config_do_obsidian(vault):
    (vault.root / ".obsidian" / "daily-notes.json").write_text(json.dumps({"folder": "Diario", "format": "YYYY-MM-DD"}), encoding="utf-8")
    first = vault.daily()
    assert first["created"] and first["path"].startswith("Diario/") and (vault.root / first["path"]).exists()
    assert vault.daily()["created"] is False
    (vault.root / ".obsidian" / "daily-notes.json").write_text(json.dumps({"folder": "../fora", "format": "YYYY"}), encoding="utf-8")
    with pytest.raises(VaultError):
        vault.daily()                                                      # config maliciosa nao escapa do cofre


def test_imagens_so_formatos_seguros(vault):
    (vault.root / "Anexos").mkdir()
    (vault.root / "Anexos" / "foto.png").write_bytes(b"\x89PNG\r\n")
    (vault.root / "Anexos" / "vetor.svg").write_text("<svg onload=alert(1)/>", encoding="utf-8")
    data, kind = vault.read_image("Anexos/foto.png")
    assert kind == "image/png" and data.startswith(b"\x89PNG")
    assert vault.find_attachment("foto.png") == "Anexos/foto.png"
    assert vault.find_attachment("vetor.svg") is None
    with pytest.raises(VaultError):
        vault.read_image("Anexos/vetor.svg")


# --------------------------------------------------------------------------- API
@pytest.fixture
def api(vault, monkeypatch):
    monkeypatch.setattr(server, "VAULT", vault)
    return vault


def client():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS)


@pytest.mark.asyncio
async def test_api_fluxo_completo(api):
    async with client() as c:
        tree = (await c.get("/api/obsidian/tree")).json()
        assert {ch["name"] for ch in tree["children"]} >= {"Projetos", "Beta", "Pasta Vazia"}
        note = (await c.get("/api/obsidian/note", params={"path": "Beta.md"})).json()
        ok = await c.put("/api/obsidian/note", json={"path": "Beta.md", "content": "novo texto", "base_hash": note["hash"]})
        assert ok.status_code == 200
        stale = await c.put("/api/obsidian/note", json={"path": "Beta.md", "content": "velho", "base_hash": note["hash"]})
        assert stale.status_code == 409 and stale.json()["detail"]["error"] == "conflict" and stale.json()["detail"]["current_hash"]
        assert (await c.post("/api/obsidian/folder", json={"path": "Nova Pasta"})).status_code == 200
        assert (await c.post("/api/obsidian/note", json={"path": "Nova Pasta/X", "content": "oi"})).status_code == 200
        dry = (await c.post("/api/obsidian/rename", json={"src": "Projetos/Alfa.md", "dst": "Alfa2.md", "dry_run": True})).json()
        assert dry["dry_run"] is True
        assert (await c.delete("/api/obsidian/note", params={"path": "Nova Pasta/X.md"})).status_code == 200
        assert (await c.get("/api/obsidian/search", params={"q": "texto"})).json()["results"][0]["path"] == "Beta.md"
        assert (await c.post("/api/obsidian/daily")).json()["created"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["../fora.md", ".obsidian/app.json", "..\\fora.md", "C:/x.md"])
async def test_api_recusa_caminhos_perigosos(api, path):
    async with client() as c:
        assert (await c.get("/api/obsidian/note", params={"path": path})).status_code in (400, 403)
        assert (await c.put("/api/obsidian/note", json={"path": path, "content": "x"})).status_code in (400, 403, 422)
        assert (await c.delete("/api/obsidian/note", params={"path": path})).status_code in (400, 403)
        assert (await c.get("/api/obsidian/file", params={"path": path})).status_code in (400, 403)


@pytest.mark.asyncio
async def test_api_exige_token(api):
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL) as c:
        assert (await c.get("/api/obsidian/tree")).status_code == 401
        assert (await c.put("/api/obsidian/note", json={"path": "Beta.md", "content": "x"})).status_code == 401


@pytest.mark.asyncio
async def test_api_imagem_vai_com_cabecalhos_defensivos(api):
    (api.root / "foto.png").write_bytes(b"\x89PNG\r\n")
    async with client() as c:
        r = await c.get("/api/obsidian/file", params={"name": "foto.png"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["content-security-policy"] == "sandbox"


class FakeEngine:
    def __init__(self, name, text="resposta"):
        self.name, self.text, self.default_model, self.seen = name, text, "m", []

    async def health_check(self):
        return HealthStatus(healthy=True, version="x", available_models=["m"])

    async def submit(self, task):
        self.seen.append(task)
        return RunHandle(task_id=task.id)

    async def collect(self, handle):
        return AgentResult(task_id=handle.task_id, status=TaskStatus.SUCCEEDED, summary="ok", output_text=self.text)


@pytest.mark.asyncio
async def test_api_ia_local_nao_pede_confirmacao_e_nao_grava_nada(api, monkeypatch):
    local = FakeEngine("ollama", "resumo local")
    monkeypatch.setattr(server.router, "adapters", {"ollama": local})
    monkeypatch.setattr(server.router, "disabled", set())
    monkeypatch.setattr(server.router, "default_offline", True)
    before = (api.root / "Beta.md").read_bytes()
    async with client() as c:
        r = await c.post("/api/obsidian/ai", json={"action": "summarize", "path": "Beta.md"})
    assert r.status_code == 200 and r.json() == {"text": "resumo local", "engine": "ollama", "model": r.json()["model"], "local": True}
    assert "banana" in local.seen[0].instruction and (api.root / "Beta.md").read_bytes() == before


@pytest.mark.asyncio
async def test_api_ia_na_nuvem_exige_confirmacao(api, monkeypatch):
    cloud_engine = FakeEngine("hera_router", "texto da nuvem")
    cloud_engine.has_cloud = lambda: True
    monkeypatch.setattr(server.router, "adapters", {"ollama": FakeEngine("ollama"), "hera_router": cloud_engine})
    monkeypatch.setattr(server.router, "disabled", set())
    monkeypatch.setattr(server.router, "default_offline", False)
    async with client() as c:
        first = await c.post("/api/obsidian/ai", json={"action": "summarize", "path": "Beta.md"})
        assert first.status_code == 409 and first.json()["detail"]["error"] == "cloud_confirm" and cloud_engine.seen == []
        second = await c.post("/api/obsidian/ai", json={"action": "summarize", "path": "Beta.md", "confirm_cloud": True})
        assert second.status_code == 200 and second.json()["local"] is False


@pytest.mark.asyncio
async def test_api_ia_com_segredo_na_nota_forca_o_local(api, monkeypatch):
    (api.root / "Segredo.md").write_text("minha chave sk-" + "abcdef0123456789" * 2, encoding="utf-8")
    local = FakeEngine("ollama", "ok")
    cloud_engine = FakeEngine("hera_router")
    cloud_engine.has_cloud = lambda: True
    monkeypatch.setattr(server.router, "adapters", {"ollama": local, "hera_router": cloud_engine})
    monkeypatch.setattr(server.router, "disabled", set())
    monkeypatch.setattr(server.router, "default_offline", False)
    async with client() as c:
        r = await c.post("/api/obsidian/ai", json={"action": "summarize", "path": "Segredo.md"})
    assert r.status_code == 200 and r.json()["engine"] == "ollama" and cloud_engine.seen == []
