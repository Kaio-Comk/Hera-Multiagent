"""Rotas do Plano em formato de conversa: projetos, conversas, histórico, imagens e execução.

O fluxo de uma mensagem: o usuário escreve (e anexa imagens) -> o planejador responde com um PLANO (cartão editável) ->
o usuário manda executar -> ao terminar entra uma mensagem com o resultado de cada passo.
Tudo o que vem do navegador é revalidado aqui; os caminhos de imagem nunca saem do servidor.
"""

import urllib.parse
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StringConstraints
from typing_extensions import Annotated

from app.chat import uploads
from app.chat.conversations import ChatError, ConversationStore

_CONTEXT_MESSAGES = 12
_CONTEXT_CHARS = 6000
_ID = Annotated[str, StringConstraints(max_length=40)]


class ProjectBody(BaseModel):
    name: Annotated[str, StringConstraints(max_length=200)] = ""
    folder: Annotated[str, StringConstraints(max_length=500)] = ""


class ProjectPatch(BaseModel):
    name: Optional[Annotated[str, StringConstraints(max_length=200)]] = None
    folder: Optional[Annotated[str, StringConstraints(max_length=500)]] = None


class ConversationBody(BaseModel):
    title: Annotated[str, StringConstraints(max_length=300)] = ""
    project_id: Optional[_ID] = None


class ConversationPatch(BaseModel):
    title: Optional[Annotated[str, StringConstraints(max_length=300)]] = None
    project_id: Optional[_ID] = None
    clear_project: bool = False             # `project_id: null` sozinho significa "não mexer"


class MessageBody(BaseModel):
    text: Annotated[str, StringConstraints(max_length=20_000)] = ""
    attachment_ids: List[_ID] = Field(default_factory=list, max_length=uploads.MAX_IMAGES_PER_MESSAGE)
    executors: Optional[List[Annotated[str, StringConstraints(max_length=40)]]] = Field(default=None, max_length=8)


def _http(exc: Exception) -> HTTPException:
    status = getattr(exc, "status", 400)
    code = {404: "not_found", 409: "limit", 413: "too_large", 415: "unsupported"}.get(status, "bad_request")
    return HTTPException(status_code=status, detail={"error": code, "message": str(exc)})


def _context(messages: List[Dict[str, Any]]) -> str:
    """Resumo curto da conversa para o planejador continuar o assunto (texto, planos propostos e resultados)."""
    lines: List[str] = []
    for m in messages[-_CONTEXT_MESSAGES:]:
        data = m.get("data") or {}
        if m["kind"] == "text" and m["role"] == "user":
            n = len(m.get("attachments") or [])
            lines.append(f"Usuário: {m['text'][:800]}" + (f" [+{n} imagem(ns)]" if n else ""))
        elif m["kind"] == "plan":
            titles = "; ".join(str(s.get("title", ""))[:80] for s in (data.get("steps") or [])[:8])
            lines.append(f"Plano proposto ({data.get('status', 'proposto')}): {titles}")
        elif m["kind"] == "run":
            for r in (data.get("report") or [])[:8]:
                lines.append(f"Resultado do passo '{str(r.get('title', ''))[:60]}' [{r.get('status', '')}]: "
                             f"{str(r.get('summary') or r.get('output_text') or '')[:400]}")
        elif m["kind"] == "error":
            lines.append(f"Erro: {m['text'][:200]}")
    text = "\n".join(lines)
    return text[-_CONTEXT_CHARS:]


def install(app: FastAPI, *, data_dir: Path, plan_for: Callable[..., Awaitable[Dict[str, Any]]],
            start_run: Callable[..., Awaitable[Dict[str, Any]]], broadcast: Callable[[Dict[str, Any]], Awaitable[Any]],
            allowed_roots: Callable[[], List[Path]], ensure_inside: Callable[..., Path], step_model: type,
            store: Optional[ConversationStore] = None) -> ConversationStore:
    store = store or ConversationStore(Path(data_dir) / "conversations.sqlite3")
    router = APIRouter()

    class ExecuteBody(BaseModel):
        steps: List[step_model] = Field(min_length=1, max_length=20)         # type: ignore[valid-type]  mesmos limites do /execute
        executors: Optional[List[Annotated[str, StringConstraints(max_length=40)]]] = Field(default=None, max_length=8)
        project_dir: Optional[Annotated[str, StringConstraints(max_length=500)]] = None

    # anexos enviados e nunca usados em mensagem (o usuário desistiu) são limpos na subida
    uploads.remove_files(Path(data_dir), store.purge_orphans())

    # ── projetos ───────────────────────────────────────────────────────────────
    def _project_folder(folder: str) -> str:
        """Pasta do projeto: precisa existir e estar nas raízes liberadas (mesma regra do `project_dir` de execução)."""
        folder = (folder or "").strip()
        if not folder:
            return ""
        try:
            resolved = ensure_inside(folder, allowed_roots())
        except Exception as exc:            # noqa: BLE001 - PathNotAllowedError e afins
            raise HTTPException(status_code=400, detail={
                "error": "project_dir_not_allowed",
                "message": f"{exc} Adicione a pasta em `general.allowed_repo_roots` no config.yaml (com o servidor parado)."})
        if not resolved.is_dir():
            raise HTTPException(status_code=400, detail={"error": "project_dir_missing", "message": f"A pasta não existe: {resolved}"})
        return str(resolved)

    @router.get("/api/chat/projects")
    async def list_projects():
        return {"projects": store.list_projects()}

    @router.post("/api/chat/projects")
    async def create_project(body: ProjectBody):
        try:
            return store.create_project(body.name, _project_folder(body.folder))
        except ChatError as exc:
            raise _http(exc)

    @router.patch("/api/chat/projects/{project_id}")
    async def patch_project(project_id: _ID, body: ProjectPatch):
        kwargs: Dict[str, Any] = {}
        if body.name is not None:
            kwargs["name"] = body.name
        if body.folder is not None:
            kwargs["folder"] = _project_folder(body.folder)
        try:
            row = store.update_project(project_id, **kwargs)
        except ChatError as exc:
            raise _http(exc)
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Projeto não encontrado."})
        return row

    @router.delete("/api/chat/projects/{project_id}")
    async def delete_project(project_id: _ID):
        if not store.delete_project(project_id):
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Projeto não encontrado."})
        return {"status": "ok"}

    # ── conversas ──────────────────────────────────────────────────────────────
    @router.get("/api/chat/conversations")
    async def list_conversations():
        return {"conversations": store.list_conversations()}

    @router.post("/api/chat/conversations")
    async def create_conversation(body: ConversationBody):
        try:
            return store.create_conversation(body.title, body.project_id)
        except ChatError as exc:
            raise _http(exc)

    def _conv_or_404(conversation_id: str) -> Dict[str, Any]:
        row = store.get_conversation(conversation_id)
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Conversa não encontrada."})
        return row

    @router.get("/api/chat/conversations/{conversation_id}")
    async def get_conversation(conversation_id: _ID):
        conv = _conv_or_404(conversation_id)
        project = store.get_project(conv["project_id"]) if conv["project_id"] else None
        return {"conversation": conv, "project": project, "messages": store.list_messages(conversation_id)}

    @router.patch("/api/chat/conversations/{conversation_id}")
    async def patch_conversation(conversation_id: _ID, body: ConversationPatch):
        _conv_or_404(conversation_id)
        kwargs: Dict[str, Any] = {}
        if body.title is not None:
            kwargs["title"] = body.title
        if body.clear_project:
            kwargs["project_id"] = None
        elif body.project_id is not None:
            kwargs["project_id"] = body.project_id
        try:
            return store.update_conversation(conversation_id, **kwargs)
        except ChatError as exc:
            raise _http(exc)

    @router.delete("/api/chat/conversations/{conversation_id}")
    async def delete_conversation(conversation_id: _ID):
        paths = store.delete_conversation(conversation_id)
        if paths is None:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Conversa não encontrada."})
        uploads.remove_files(Path(data_dir), paths)
        return {"status": "ok"}

    # ── imagens ────────────────────────────────────────────────────────────────
    @router.post("/api/chat/conversations/{conversation_id}/attachments")
    async def upload_attachment(conversation_id: _ID, request: Request):
        """Corpo = os bytes da imagem (sem multipart). O nome original vem em `X-Filename` (codificado em URL) e só serve de rótulo."""
        _conv_or_404(conversation_id)
        pending = store._count("attachments", "conversation_id = ? AND message_id IS NULL", (conversation_id,))
        if pending >= uploads.MAX_IMAGES_PER_MESSAGE:
            raise HTTPException(status_code=409, detail={"error": "limit", "message": f"No máximo {uploads.MAX_IMAGES_PER_MESSAGE} imagens por mensagem."})
        data = await request.body()
        name = urllib.parse.unquote(request.headers.get("x-filename", ""))[:120]
        try:
            saved = uploads.save_image(Path(data_dir), conversation_id, data)
            row = store.add_attachment(conversation_id, name, str(saved["mime"]), int(saved["size"]), str(saved["path"]))
        except (uploads.UploadError, ChatError) as exc:
            if "saved" in locals():
                uploads.remove_files(Path(data_dir), [str(saved["path"])])
            raise _http(exc)
        return store.public_attachment(row)

    @router.get("/api/attachments/{attachment_id}")
    async def get_attachment(attachment_id: _ID):
        row = store.get_attachment(attachment_id)
        if row is None or not uploads.is_inside_uploads(Path(data_dir), row["path"]) or not Path(row["path"]).is_file():
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Imagem não encontrada."})
        return FileResponse(row["path"], media_type=row["mime"], headers=uploads.SERVE_HEADERS)

    @router.delete("/api/chat/attachments/{attachment_id}")
    async def delete_attachment(attachment_id: _ID):
        """Remove uma imagem que ainda não foi enviada (o usuário tirou da caixa de texto)."""
        row = store.get_attachment(attachment_id)
        if row is None or row["message_id"] is not None:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Imagem não encontrada."})
        uploads.remove_files(Path(data_dir), [store.delete_attachment(attachment_id) or ""])
        return {"status": "ok"}

    # ── mensagens ──────────────────────────────────────────────────────────────
    @router.post("/api/chat/conversations/{conversation_id}/messages")
    async def send_message(conversation_id: _ID, body: MessageBody):
        """Guarda a mensagem do usuário, pede o plano ao planejador e guarda a resposta (plano ou erro)."""
        conv = _conv_or_404(conversation_id)
        text = body.text.strip()
        if len(text) < 3 and not body.attachment_ids:
            raise HTTPException(status_code=400, detail={"error": "empty", "message": "Escreva o que você quer fazer (ou anexe uma imagem)."})
        try:
            pend = store.pending_attachments(conversation_id, body.attachment_ids)
            history = store.list_messages(conversation_id)
            user_msg = store.add_message(conversation_id, "user", "text", text, attachment_ids=[a["id"] for a in pend])
        except ChatError as exc:
            raise _http(exc)
        if conv["title"] == "Nova conversa" and text:
            store.update_conversation(conversation_id, title=" ".join(text.split())[:60])
        images = [a["path"] for a in pend]
        goal = text if len(text) >= 3 else "Analise a(s) imagem(ns) anexada(s) e descreva o que vê."
        project = store.get_project(conv["project_id"]) if conv.get("project_id") else None
        extra = {"project_name": str(project["name"])} if project and project.get("name") else {}
        try:
            plan = await plan_for(goal, body.executors, _context(history), images, **extra)
        except HTTPException as exc:
            detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail)}
            err = store.add_message(conversation_id, "assistant", "error", str(detail.get("message", "Falha ao planejar.")))
            return {"user": user_msg, "assistant": err}
        data = {"goal": goal, "steps": plan["steps"], "warnings": plan["warnings"], "planner": plan["planner"],
                "executors": plan["executors"], "project_dirs": plan.get("project_dirs", []), "status": "proposed", "user_message_id": user_msg["id"]}
        reply = store.add_message(conversation_id, "assistant", "plan", "", data)
        return {"user": user_msg, "assistant": reply}

    @router.post("/api/chat/conversations/{conversation_id}/messages/{message_id}/execute")
    async def execute_message(conversation_id: _ID, message_id: _ID, body: ExecuteBody, background_tasks: BackgroundTasks):
        """Executa o plano de uma mensagem (os passos que chegam aqui são os editados pelo usuário) na pasta do projeto."""
        conv = _conv_or_404(conversation_id)
        msg = store.get_message(message_id)
        if msg is None or msg["conversation_id"] != conversation_id or msg["kind"] != "plan":
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Plano não encontrado."})
        if msg["data"].get("status") == "running":
            raise HTTPException(status_code=409, detail={"error": "already_running", "message": "Este plano já está em execução."})
        project_dir = (body.project_dir or "").strip() or None
        if project_dir is None and conv["project_id"]:
            project = store.get_project(conv["project_id"])
            project_dir = (project or {}).get("folder") or None
        suggested = [d for d in (msg["data"].get("project_dirs") or []) if isinstance(d, str)]
        if project_dir is None and suggested:                 # pastas citadas no pedido (ex.: "nethub"): a primeira e a de trabalho
            project_dir = suggested[0]
        extra_dirs = [d for d in suggested if d != project_dir]
        images = [a["path"] for a in store.message_attachments(msg["data"].get("user_message_id", ""))]
        goal = str(msg["data"].get("goal", ""))

        async def finished(report: List[Dict[str, Any]], error: str, final: str = "") -> None:
            ok_n = sum(1 for r in report if r.get("status") == "SUCCEEDED")
            status = "done" if report and ok_n == len(report) else "problems"
            store.update_message(message_id, data={"status": status}, merge=True)
            summary = error or f"{ok_n} de {len(report)} passo(s) deram certo."
            store.add_message(conversation_id, "assistant", "run", summary,
                              {"report": report, "error": error, "final": final, "plan_message_id": message_id, "status": status})
            await broadcast({"type": "chat_updated", "conversation_id": conversation_id})

        result = await start_run(goal, [s.model_dump() for s in body.steps], body.executors, project_dir, background_tasks, images, finished, extra_dirs)
        store.update_message(message_id, data={"status": "running", "run_id": result["run_id"], "steps": result["steps"]}, merge=True)
        return {"status": "enqueued", "run_id": result["run_id"], "warnings": result["warnings"], "message": store.get_message(message_id)}

    app.include_router(router)
    app.state.chat_store = store
    return store
