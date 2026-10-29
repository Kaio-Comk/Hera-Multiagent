from __future__ import annotations

import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from app.memory.store import MemoryStore

logger = logging.getLogger(__name__)

_VERSAO_ESQUEMA = 2  # 2: coluna `titulo` (nome da nota com hifens viram espacos) tambem e buscavel


_STOPWORDS = frozenset({
    'de', 'da', 'do', 'das', 'dos', 'para', 'por', 'com', 'uma', 'uns',
    'que', 'nao', 'the', 'and', 'for', 'with', 'this', 'that', 'are', 'was',
})
_MAX_TERMOS = 8


class IndiceIndisponivel(RuntimeError):
    '''O indice FTS5 nao respondeu (banco travado, corrompido ou tabela ausente).'''


def termos_da_consulta(consulta: str) -> list[str]:
    '''Termos uteis da consulta: sem palavras curtas/comuns, sem repeticao, no maximo 8 (os mais longos), na ordem original.'''
    vistos: set[str] = set()
    termos: list[str] = []
    for t in re.findall(r'\w+', (consulta or '').lower()):
        if len(t) < 3 or t in _STOPWORDS or t in vistos:
            continue
        vistos.add(t)
        termos.append(t)
    if len(termos) <= _MAX_TERMOS:
        return termos
    escolhidos = set(sorted(range(len(termos)), key=lambda i: (-len(termos[i]), i))[:_MAX_TERMOS])
    return [t for i, t in enumerate(termos) if i in escolhidos]


@dataclass(frozen=True)
class Resultado:
    nome: str
    descricao: str
    tipo: str
    trecho: str
    score: float


class MemoryIndex:
    def __init__(self, store: MemoryStore, db_path: str | Path = ':memory:') -> None:
        self.store = store
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        # O indice e dado derivado das notas: se o esquema mudou, apaga e deixa o sincronizar() refazer.
        if self._conn.execute('PRAGMA user_version').fetchone()[0] < _VERSAO_ESQUEMA:
            self._conn.execute('DROP TABLE IF EXISTS busca')
            self._conn.execute('DROP TABLE IF EXISTS meta')
            self._conn.execute(f'PRAGMA user_version = {_VERSAO_ESQUEMA}')
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS busca USING fts5("
            "nome UNINDEXED, tipo UNINDEXED, titulo, descricao, corpo, "
            "tokenize='unicode61 remove_diacritics 2')"
        )
        self._conn.execute(
            'CREATE TABLE IF NOT EXISTS meta ('
            'nome TEXT PRIMARY KEY, mtime_ns INTEGER, tamanho INTEGER)'
        )
        self._conn.commit()

    def _arquivos(self) -> dict[str, tuple[int, int]]:
        base = Path(self.store.base_dir)
        resultado: dict[str, tuple[int, int]] = {}
        if not base.is_dir():
            return resultado
        for p in base.glob('*.md'):
            try:
                st = p.stat()
            except OSError:
                continue
            resultado[p.stem] = (st.st_mtime_ns, st.st_size)
        return resultado

    def sincronizar(self) -> dict:
        with self._lock:
            adicionadas = 0
            atualizadas = 0
            removidas = 0
            disco = self._arquivos()
            meta = {
                row[0]: (row[1], row[2])
                for row in self._conn.execute(
                    'SELECT nome, mtime_ns, tamanho FROM meta'
                ).fetchall()
            }
            for nome, (mtime_ns, tamanho) in disco.items():
                anterior = meta.get(nome)
                if anterior == (mtime_ns, tamanho):
                    continue
                try:
                    nota = self.store.get(nome)
                except Exception:
                    # Nota ilegivel nao pode ficar achavel pelo conteudo antigo.
                    self._conn.execute('DELETE FROM busca WHERE nome = ?', (nome,))
                    self._conn.execute('DELETE FROM meta WHERE nome = ?', (nome,))
                    if anterior is not None:
                        removidas += 1
                    continue
                self._conn.execute('DELETE FROM busca WHERE nome = ?', (nome,))
                self._conn.execute(
                    'INSERT INTO busca (nome, tipo, titulo, descricao, corpo) VALUES (?, ?, ?, ?, ?)',
                    (nota.nome, nota.tipo, nota.nome.replace('-', ' '), nota.descricao, nota.corpo),
                )
                self._conn.execute(
                    'INSERT OR REPLACE INTO meta (nome, mtime_ns, tamanho) VALUES (?, ?, ?)',
                    (nome, mtime_ns, tamanho),
                )
                if anterior is None:
                    adicionadas += 1
                else:
                    atualizadas += 1
            for nome in list(meta.keys()):
                if nome not in disco:
                    self._conn.execute('DELETE FROM busca WHERE nome = ?', (nome,))
                    self._conn.execute('DELETE FROM meta WHERE nome = ?', (nome,))
                    removidas += 1
            self._conn.commit()
            return {
                'adicionadas': adicionadas,
                'atualizadas': atualizadas,
                'removidas': removidas,
            }

    def buscar(self, consulta: str, limite: int = 5) -> list[Resultado]:
        with self._lock:
            self.sincronizar()
            termos = termos_da_consulta(consulta)
            if not termos:
                return []
            fts = ' OR '.join('"' + t.replace('"', '') + '"*' for t in termos)
            try:
                rows = self._conn.execute(
                    'SELECT nome, descricao, tipo, '
                    "snippet(busca, 4, '', '', '...', 20), "
                    'bm25(busca, 0, 0, 4.0, 5.0, 1.0) AS s '
                    'FROM busca WHERE busca MATCH ? ORDER BY s LIMIT ?',
                    (fts, limite),
                ).fetchall()
            except sqlite3.OperationalError as exc:
                logger.warning('indice da memoria indisponivel: %s', exc)
                raise IndiceIndisponivel(str(exc)) from exc
            resultado: list[Resultado] = []
            for nome, descricao, tipo, trecho, s in rows:
                texto = trecho if trecho else ''
                if len(texto) > 160:
                    texto = texto[:160]
                resultado.append(
                    Resultado(
                        nome=nome,
                        descricao=descricao,
                        tipo=tipo,
                        trecho=texto,
                        score=float(-s),
                    )
                )
            return resultado

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> 'MemoryIndex':
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
