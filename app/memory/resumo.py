"""Resumo de sessão com prévia obrigatória (M1b)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable

from app.memory.schema import TIPOS, validar_nome
from app.memory.store import DuplicateNoteError, MemoryStore
from app.privacy_guard import PrivacyGuard


@dataclass(frozen=True)
class NotaProposta:
    """Proposta de nota ainda não persistida."""

    nome: str
    descricao: str
    tipo: str
    corpo: str


_PROMPT = (
    'Você é um assistente que extrai fatos duráveis de uma conversa.\n'
    'Extraia apenas fatos duráveis: preferências, decisões e referências.\n'
    'Ignore informações efêmeras (saudações, perguntas passageiras, etc.).\n'
    'Responda SOMENTE com um JSON no formato:\n'
    '{"notas":[{"nome","descricao","tipo","corpo"}]}\n'
    'Regras: nome em ^[a-z0-9][a-z0-9-]{0,63}$; tipo em '
    + str(list(TIPOS))
    + '; corpo não vazio.\n\n'
    'Conversa:\n'
)


def _extrair_json(texto: str) -> object | None:
    """Tenta extrair JSON do texto cru, inclusive dentro de bloco ```."""
    if not isinstance(texto, str):
        return None
    candidatos: list[str] = []
    for m in re.finditer(r'```(?:json)?\s*(.*?)```', texto, re.DOTALL | re.IGNORECASE):
        candidatos.append(m.group(1).strip())
    candidatos.append(texto.strip())
    for cand in candidatos:
        try:
            return json.loads(cand)
        except (ValueError, TypeError):
            continue
    return None


def _montar_conversa(mensagens: list[dict], max_chars: int) -> str:
    """Sanitiza e corta pelas mensagens mais recentes até o limite."""
    linhas: list[str] = []
    total = 0
    for msg in reversed(mensagens):
        if not isinstance(msg, dict):
            continue
        role = str(msg.get('role', ''))
        content = msg.get('content', '')
        if not isinstance(content, str):
            content = str(content)
        content = PrivacyGuard.sanitize_content(content)
        linha = f'{role}: {content}'
        if total + len(linha) > max_chars and linhas:
            break
        linhas.append(linha)
        total += len(linha)
    linhas.reverse()
    return '\n'.join(linhas)


def propor_resumo(
    mensagens: list[dict],
    chamar_modelo: Callable[[str], str],
    *,
    max_notas: int = 5,
    max_chars_conversa: int = 12000,
) -> list[NotaProposta]:
    """Pede ao modelo propostas de notas e devolve as válidas."""
    conversa = _montar_conversa(mensagens, max_chars_conversa)
    prompt = _PROMPT + conversa
    try:
        cru = chamar_modelo(prompt)
    except Exception:
        return []
    dados = _extrair_json(cru)
    if not isinstance(dados, dict):
        return []
    notas = dados.get('notas')
    if not isinstance(notas, list):
        return []
    vistas: set[str] = set()
    resultado: list[NotaProposta] = []
    for item in notas:
        if not isinstance(item, dict):
            continue
        nome = item.get('nome')
        descricao = item.get('descricao', '')
        tipo = item.get('tipo')
        corpo = item.get('corpo')
        if not isinstance(nome, str) or not isinstance(tipo, str):
            continue
        if not isinstance(corpo, str) or not corpo.strip():
            continue
        if not isinstance(descricao, str):
            descricao = str(descricao)
        try:
            validar_nome(nome)
        except ValueError:
            continue
        if tipo not in TIPOS:
            continue
        if nome in vistas:
            continue
        vistas.add(nome)
        resultado.append(
            NotaProposta(nome=nome, descricao=descricao, tipo=tipo, corpo=corpo)
        )
        if len(resultado) >= max_notas:
            break
    return resultado


def salvar_aprovadas(
    store: MemoryStore,
    propostas: list[NotaProposta],
    aprovar: Callable[[NotaProposta], bool],
) -> dict:
    """Grava apenas as propostas aprovadas; nunca sobrescreve."""
    salvas: list[str] = []
    recusadas: list[str] = []
    puladas: list[str] = []
    for p in propostas:
        try:
            aprovada = aprovar(p)
        except Exception:
            aprovada = False
        if not aprovada:
            recusadas.append(p.nome)
            continue
        try:
            store.save(p.nome, p.descricao, p.tipo, p.corpo)
        except (FileExistsError, DuplicateNoteError):
            puladas.append(p.nome)
            continue
        salvas.append(p.nome)
    return {'salvas': salvas, 'recusadas': recusadas, 'puladas': puladas}
