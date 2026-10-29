from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app.chat.conversations import ChatError, ConversationStore


class Sessoes:
    '''Sessoes de chat por pasta de trabalho.'''

    def __init__(self, store: ConversationStore, pasta: Path) -> None:
        self.store = store
        self.pasta = str(Path(pasta).resolve())

    def _projeto_id(self, criar: bool) -> int | None:
        for p in self.store.list_projects():
            if str(p.get('folder') or '') == self.pasta:
                return p['id']
        if not criar:
            return None
        nome = Path(self.pasta).name or self.pasta
        proj = self.store.create_project(nome, folder=self.pasta)
        return proj['id']

    def nova(self, titulo: str = '') -> str:
        pid = self._projeto_id(criar=True)
        conv = self.store.create_conversation(title=titulo, project_id=pid)
        return conv['id']

    def registrar_turno(self, conversa_id: str, usuario: str, assistente: str) -> None:
        self.store.add_message(conversa_id, 'user', 'text', usuario)
        if assistente:
            self.store.add_message(conversa_id, 'assistant', 'text', assistente)
        conv = self.store.get_conversation(conversa_id)
        if conv and (conv.get('title') or '') == 'Nova conversa':
            linha = usuario.strip().splitlines()[0] if usuario.strip() else ''
            if len(linha) > 60:
                linha = linha[:60] + '...'
            if linha:
                self.store.update_conversation(conversa_id, title=linha)

    def carregar(self, conversa_id: str) -> list[dict[str, Any]]:
        conv = self.store.get_conversation(conversa_id)
        if conv is None:
            raise LookupError(f'conversa nao encontrada: {conversa_id}')
        out: list[dict[str, Any]] = []
        for m in self.store.list_messages(conversa_id):
            if m.get('kind') != 'text':
                continue
            if m.get('role') not in ('user', 'assistant'):
                continue
            texto = m.get('text') or ''
            if not texto:
                continue
            out.append({'role': m['role'], 'content': texto})
        return out

    def _pasta_da(self, conv: dict[str, Any]) -> str:
        pid = conv.get('project_id')
        if pid is None:
            return '-'
        for p in self.store.list_projects():
            if p['id'] == pid:
                return str(p.get('folder') or '-')
        return '-'

    def resolver(self, ref: str | None) -> dict[str, Any] | None:
        convs = self.store.list_conversations()
        if not ref:
            for c in convs:
                if self._pasta_da(c) == self.pasta:
                    return c
            return convs[0] if convs else None
        for c in convs:
            if c['id'] == ref:
                return c
        alvo = ref.lower()
        for c in convs:
            if alvo in (c.get('title') or '').lower():
                return c
        return None

    def listar(self, limite: int = 20) -> list[dict[str, Any]]:
        convs = self.store.list_conversations()[:limite]
        out: list[dict[str, Any]] = []
        for c in convs:
            pasta = self._pasta_da(c)
            msgs = [m for m in self.store.list_messages(c['id']) if m.get('kind') == 'text']
            atual = c.get('updated') or c.get('created') or ''
            try:
                # o store guarda epoch (float); aceita tambem texto ISO por seguranca
                dt = datetime.fromtimestamp(float(atual)) if isinstance(atual, (int, float)) else datetime.fromisoformat(str(atual))
                atualizada = dt.strftime('%Y-%m-%d %H:%M')
            except (ValueError, OverflowError, OSError):
                atualizada = str(atual)[:16]
            out.append({
                'id': c['id'],
                'titulo': c.get('title') or '',
                'atualizada': atualizada,
                'pasta': pasta,
                'mensagens': len(msgs),
                'desta_pasta': pasta == self.pasta,
            })
        return out


__all__ = ['Sessoes', 'ChatError']
