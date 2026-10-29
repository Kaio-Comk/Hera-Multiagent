"""Plano em formato de conversa: armazenamento, imagens (validadas pelo conteúdo), rotas e envio de imagens aos motores."""
import asyncio
import os
import struct
import zlib

import pytest
from httpx import ASGITransport, AsyncClient

import app.server as server
from adapters import images as images_mod
from app.chat import uploads
from app.chat.conversations import ChatError, ConversationStore
from helpers import AUTH_HEADERS, TEST_BASE_URL
from test_orchestration import PLAN_JSON, FakeAdapter, api  # noqa: F401  (fixture `api`)


def png_bytes() -> bytes:
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"\x00\xff\x00\x00"
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def client():
    return AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL, headers=AUTH_HEADERS)


@pytest.fixture
def store(tmp_path):
    return ConversationStore(tmp_path / "c.sqlite3")


# --------------------------------------------------------------------------- armazenamento
def test_projetos_e_conversas(store):
    p = store.create_project("  Meu\nsite  ", "")
    assert p["name"] == "Meu site" and p["conversations"] == 0
    c = store.create_conversation("", p["id"])
    assert c["title"] == "Nova conversa" and c["project_id"] == p["id"]
    assert store.list_conversations(p["id"])[0]["id"] == c["id"]
    assert store.list_conversations(None) == []
    store.update_conversation(c["id"], project_id=None)
    assert store.list_conversations(None)[0]["id"] == c["id"]
    store.update_conversation(c["id"], project_id=p["id"])
    assert store.delete_project(p["id"]) is True
    assert store.get_conversation(c["id"])["project_id"] is None        # a conversa sobrevive sem projeto


def test_validacoes_do_armazenamento(store):
    with pytest.raises(ChatError):
        store.create_project("   ")
    with pytest.raises(ChatError) as e:
        store.create_conversation("x", "p_inexistente")
    assert e.value.status == 404
    c = store.create_conversation("a")
    with pytest.raises(ChatError):
        store.add_message(c["id"], "system", "text", "x")
    with pytest.raises(ChatError):
        store.add_message("c_000000000000", "user", "text", "x")


def test_mensagens_em_ordem_com_dados_e_anexos(store):
    c = store.create_conversation("a")
    a = store.add_attachment(c["id"], "foto.png", "image/png", 10, "/x/y.png")
    m1 = store.add_message(c["id"], "user", "text", "oi", attachment_ids=[a["id"]])
    m2 = store.add_message(c["id"], "assistant", "plan", "", {"steps": [{"title": "t"}]})
    msgs = store.list_messages(c["id"])
    assert [m["id"] for m in msgs] == [m1["id"], m2["id"]]
    assert msgs[0]["attachments"][0]["url"] == f"/api/attachments/{a['id']}"
    assert "path" not in msgs[0]["attachments"][0]                      # o caminho do disco nunca sai
    store.update_message(m2["id"], data={"status": "running"}, merge=True)
    assert store.get_message(m2["id"])["data"] == {"steps": [{"title": "t"}], "status": "running"}
    with pytest.raises(ChatError):                                       # anexo já enviado não pode ser reusado
        store.pending_attachments(c["id"], [a["id"]])


def test_apagar_conversa_devolve_arquivos_e_limpa_orfaos(store):
    c = store.create_conversation("a")
    a = store.add_attachment(c["id"], "f.png", "image/png", 1, "/x/1.png")
    assert store.purge_orphans(older_than_seconds=-1) == ["/x/1.png"]
    assert store.get_attachment(a["id"]) is None
    store.add_attachment(c["id"], "g.png", "image/png", 1, "/x/2.png")
    assert store.delete_conversation(c["id"]) == ["/x/2.png"]
    assert store.delete_conversation(c["id"]) is None


def test_sql_injection_e_parametrizado(store):
    evil = "x'); DROP TABLE conversations;--"
    p = store.create_project(evil)
    c = store.create_conversation(evil, p["id"])
    assert store.get_conversation(c["id"])["title"].startswith("x')")
    assert store.get_conversation("' OR 1=1 --") is None


# --------------------------------------------------------------------------- imagens
def test_tipo_da_imagem_vem_do_conteudo(tmp_path):
    assert uploads.sniff_image(png_bytes())["mime"] == "image/png"
    assert uploads.sniff_image(b"\xff\xd8\xff\xe0abc")["mime"] == "image/jpeg"
    assert uploads.sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 ")["mime"] == "image/webp"
    assert uploads.sniff_image(b"<svg onload=alert(1)>") is None
    assert uploads.sniff_image(b"MZ\x90\x00") is None
    with pytest.raises(uploads.UploadError) as e:
        uploads.save_image(tmp_path, "c_" + "0" * 12, b"<svg/>")
    assert e.value.status == 415
    with pytest.raises(uploads.UploadError):
        uploads.save_image(tmp_path, "../../etc", png_bytes())          # id de conversa vira caminho: só o formato exato passa
    saved = uploads.save_image(tmp_path, "c_" + "0" * 12, png_bytes())
    assert uploads.is_inside_uploads(tmp_path, saved["path"]) and saved["path"].endswith(".png")
    assert not uploads.is_inside_uploads(tmp_path, str(tmp_path / "config.yaml"))
    assert uploads.remove_files(tmp_path, [saved["path"], str(tmp_path / "config.yaml")]) == 1


def test_imagem_grande_demais(tmp_path):
    with pytest.raises(uploads.UploadError) as e:
        uploads.save_image(tmp_path, "c_" + "0" * 12, png_bytes() + b"0" * uploads.MAX_IMAGE_BYTES)
    assert e.value.status == 413


# --------------------------------------------------------------------------- API
@pytest.mark.asyncio
async def test_api_projeto_conversa_e_imagem(api):  # noqa: F811
    async with client() as c:
        proj = (await c.post("/api/chat/projects", json={"name": "Loja"})).json()
        conv = (await c.post("/api/chat/conversations", json={"project_id": proj["id"]})).json()
        cid = conv["id"]
        # imagem falsa (SVG) recusada; PNG verdadeiro aceito mesmo com nome mentiroso
        bad = await c.post(f"/api/chat/conversations/{cid}/attachments", content=b"<svg onload=alert(1)>",
                           headers={"X-Filename": "a.png", "Content-Type": "image/png"})
        assert bad.status_code == 415
        ok = await c.post(f"/api/chat/conversations/{cid}/attachments", content=png_bytes(), headers={"X-Filename": "tela%20final.exe"})
        att = ok.json()
        assert ok.status_code == 200 and att["mime"] == "image/png" and "path" not in att
        served = await c.get(att["url"])
        assert served.status_code == 200 and served.headers["content-type"] == "image/png" and served.content == png_bytes()
        assert "nosniff" in served.headers["x-content-type-options"]
        # envia a mensagem com a imagem: o planejador recebe o caminho e responde um plano
        planner = api[0]
        r = await c.post(f"/api/chat/conversations/{cid}/messages", json={"text": "crie uma API de login", "attachment_ids": [att["id"]]})
        data = r.json()
        assert r.status_code == 200 and data["assistant"]["kind"] == "plan" and len(data["assistant"]["data"]["steps"]) == 2
        assert data["user"]["attachments"][0]["id"] == att["id"]
        assert planner.received[0].images and planner.received[0].images[0].endswith(".png")
        assert "IMAGENS ANEXADAS" in planner.received[0].instruction
        # a mesma imagem não vale duas vezes; o título virou o texto
        again = await c.post(f"/api/chat/conversations/{cid}/messages", json={"text": "outra coisa", "attachment_ids": [att["id"]]})
        assert again.status_code == 404
        full = (await c.get(f"/api/chat/conversations/{cid}")).json()
        assert full["conversation"]["title"] == "crie uma API de login" and full["project"]["name"] == "Loja"
        assert [m["kind"] for m in full["messages"]] == ["text", "plan"]
        # segunda mensagem recebe a conversa anterior como contexto
        await c.post(f"/api/chat/conversations/{cid}/messages", json={"text": "agora adicione testes"})
        assert "CONTEXTO DA CONVERSA" in planner.received[-1].instruction and "crie uma API de login" in planner.received[-1].instruction
        # apagar a conversa apaga o arquivo da imagem
        path = server.app.state.chat_store.get_attachment(att["id"])["path"]
        assert os.path.isfile(path)
        assert (await c.delete(f"/api/chat/conversations/{cid}")).status_code == 200
        assert not os.path.exists(path)
        assert (await c.get(att["url"])).status_code == 404


@pytest.mark.asyncio
async def test_api_executar_plano_da_conversa_guarda_o_resultado(api):  # noqa: F811
    _, worker = api
    async with client() as c:
        cid = (await c.post("/api/chat/conversations", json={})).json()["id"]
        plan_msg = (await c.post(f"/api/chat/conversations/{cid}/messages", json={"text": "faça o site"})).json()["assistant"]
        steps = plan_msg["data"]["steps"]
        r = await c.post(f"/api/chat/conversations/{cid}/messages/{plan_msg['id']}/execute", json={"steps": steps})
        assert r.status_code == 200 and r.json()["run_id"].startswith("run-")
        for _ in range(60):
            msgs = (await c.get(f"/api/chat/conversations/{cid}")).json()["messages"]
            if msgs[-1]["kind"] == "run":
                break
            await asyncio.sleep(0.1)
        assert msgs[-1]["kind"] == "run" and msgs[-1]["data"]["report"][0]["output_text"] == "resultado do worker"
        assert [m for m in msgs if m["kind"] == "plan"][0]["data"]["status"] == "done"
        assert len(worker.received) == 2
        # plano de outra conversa / mensagem inexistente
        other = (await c.post("/api/chat/conversations", json={})).json()["id"]
        assert (await c.post(f"/api/chat/conversations/{other}/messages/{plan_msg['id']}/execute", json={"steps": steps})).status_code == 404
        assert (await c.post(f"/api/chat/conversations/{cid}/messages/m_000000000000/execute", json={"steps": steps})).status_code == 404


@pytest.mark.asyncio
async def test_api_executar_usa_a_pasta_do_projeto_e_recusa_fora_da_lista(api, tmp_path):  # noqa: F811
    async with client() as c:
        fora = os.path.join(tmp_path.anchor, "Windows", "System32")          # 'C:\Windows\System32' no Windows, '/Windows/System32' no resto
        r = await c.post("/api/chat/projects", json={"name": "Fora", "folder": fora})
        assert r.status_code == 400 and r.json()["detail"]["error"] == "project_dir_not_allowed"
        assert (await c.post("/api/chat/projects", json={"name": "   "})).status_code == 400


@pytest.mark.asyncio
async def test_api_validacoes(api):  # noqa: F811
    async with client() as c:
        cid = (await c.post("/api/chat/conversations", json={})).json()["id"]
        assert (await c.post(f"/api/chat/conversations/{cid}/messages", json={"text": ""})).status_code == 400
        assert (await c.post(f"/api/chat/conversations/{cid}/messages", json={"text": "x" * 20001})).status_code == 422
        assert (await c.get("/api/chat/conversations/c_nao_existe")).status_code == 404
        assert (await c.get("/api/attachments/a_nao_existe")).status_code == 404
        assert (await c.post("/api/chat/conversations/c_nao_existe/attachments", content=png_bytes())).status_code == 404
        assert (await c.patch(f"/api/chat/conversations/{cid}", json={"project_id": "p_nao_existe"})).status_code == 404


@pytest.mark.asyncio
async def test_api_sem_token_nao_acessa_conversas_nem_imagens(api):  # noqa: F811
    async with AsyncClient(transport=ASGITransport(app=server.app), base_url=TEST_BASE_URL) as anon:
        assert (await anon.get("/api/chat/conversations")).status_code == 401
        assert (await anon.get("/api/attachments/a_000000000000")).status_code == 401


# --------------------------------------------------------------------------- motores
def test_adaptadores_recebem_so_imagens_validas(tmp_path):
    good = tmp_path / "a.png"
    good.write_bytes(png_bytes())
    txt = tmp_path / "b.txt"
    txt.write_text("x")
    paths = [str(good), str(txt), str(tmp_path / "nao-existe.png")]
    assert images_mod.usable_images(paths) == [str(good)]
    assert str(good) in images_mod.prompt_note(paths) and "b.txt" not in images_mod.prompt_note(paths)
    assert images_mod.image_dirs(paths) == [str(tmp_path)]
    assert images_mod.encode_base64(paths)[0].startswith("iVBOR")
    assert images_mod.prompt_note([]) == "" and images_mod.not_supported_note("x", []) == ""
    assert images_mod.looks_like_vision_model("llava:7b") and not images_mod.looks_like_vision_model("llama3.2:3b")


def test_prompt_do_claude_code_e_antigravity_cita_a_imagem(tmp_path):
    from adapters.antigravity import AntigravityAdapter
    from adapters.claude_code import ClaudeCodeAdapter
    from models.task import AgentTask
    img = tmp_path / "a.png"
    img.write_bytes(png_bytes())
    task = AgentTask(id="t", title="x", instruction="olhe a tela", images=[str(img)])
    for cls in (ClaudeCodeAdapter, AntigravityAdapter):
        prompt = cls._build_prompt(cls.__new__(cls), task)
        assert str(img) in prompt


@pytest.mark.asyncio
async def test_botao_parar_cancela_o_plano_em_execucao(api):  # noqa: F811
    _, worker = api

    async def lento(handle):
        await asyncio.sleep(60)

    worker.collect = lento
    async with client() as c:
        cid = (await c.post("/api/chat/conversations", json={})).json()["id"]
        plan = (await c.post(f"/api/chat/conversations/{cid}/messages", json={"text": "faça o site"})).json()["assistant"]
        # o transporte de teste so devolve a resposta depois das tarefas de fundo: dispara em paralelo (no uvicorn a resposta sai antes)
        pending = asyncio.create_task(c.post(f"/api/chat/conversations/{cid}/messages/{plan['id']}/execute",
                                             json={"steps": plan["data"]["steps"]}))
        for _ in range(50):                                   # espera a execucao entrar no ar
            if server.ACTIVE_RUNS:
                break
            await asyncio.sleep(0.1)
        run_id = next(iter(server.ACTIVE_RUNS))
        assert (await c.post("/api/orchestration/runs/run-zzzzzzzz/cancel")).status_code == 400
        assert (await c.post("/api/orchestration/runs/run-00000000/cancel")).status_code == 404
        assert (await c.post(f"/api/orchestration/runs/{run_id}/cancel")).status_code == 200
        for _ in range(50):
            msgs = (await c.get(f"/api/chat/conversations/{cid}")).json()["messages"]
            if msgs[-1]["kind"] == "run":
                break
            await asyncio.sleep(0.1)
        assert msgs[-1]["kind"] == "run" and "Cancelado" in msgs[-1]["text"]
        await asyncio.wait_for(pending, timeout=10)
        assert run_id not in server.ACTIVE_RUNS
