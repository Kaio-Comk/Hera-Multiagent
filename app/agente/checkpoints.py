from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

APAGADO = 'APAGADO'


@dataclass(frozen=True)
class Entrada:
    id: str
    arquivo: str
    existia: bool
    hash_antes: str
    hash_depois: str
    ts: float
    revertido: bool


@dataclass(frozen=True)
class Relatorio:
    restaurados: tuple[str, ...]
    pulados: tuple[str, ...]
    avisos: tuple[str, ...]


def _sha256(dados: bytes) -> str:
    return hashlib.sha256(dados).hexdigest()


class Checkpoints:
    def __init__(self, pasta_dados: Path, raiz: Path) -> None:
        self.pasta_dados = Path(pasta_dados)
        self.raiz = Path(getattr(raiz, 'pasta', raiz)).resolve()      # aceita Path ou o objeto Raiz
        self.permitir_externos = bool(getattr(raiz, 'acesso_total', False))   # so com acesso total ha alvo fora da raiz
        self.pasta_dados.mkdir(parents=True, exist_ok=True)
        self.blobs = self.pasta_dados / 'blobs'
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.ledger = self.pasta_dados / 'razao.jsonl'

    def _rel(self, alvo: Path) -> str:
        p = Path(alvo)
        if not p.is_absolute():
            p = self.raiz / p
        p = p.resolve()
        try:
            rel = p.relative_to(self.raiz)
        except ValueError:
            if not self.permitir_externos:
                raise ValueError(f'alvo fora da raiz: {alvo}')
            # fora da pasta de trabalho (acesso total): '@ext/B/Projetos/x' (unidade) ou '@ext//pasta/x' (posix)
            posix = p.as_posix()
            if len(posix) > 1 and posix[1] == ':':
                return f'@ext/{posix[0]}{posix[2:]}'
            return f'@ext/{posix}'
        return rel.as_posix()

    def _abs(self, rel: str) -> Path:
        if rel.startswith('@ext/') and self.permitir_externos:
            resto = rel[5:]
            if resto.startswith('/'):
                return Path(resto)
            unidade, _, caminho = resto.partition('/')
            return Path(f'{unidade}:/{caminho}')
        p = (self.raiz / rel).resolve()
        try:
            p.relative_to(self.raiz)
        except ValueError:
            raise ValueError(f'caminho fora da raiz: {rel}')
        return p

    def _ler_ledger(self) -> list[Entrada]:
        if not self.ledger.exists():
            return []
        entradas: list[Entrada] = []
        for linha in self.ledger.read_text(encoding='utf-8').splitlines():
            linha = linha.strip()
            if not linha:
                continue
            try:
                dados: dict[str, Any] = json.loads(linha)
                entradas.append(Entrada(**dados))
            except (json.JSONDecodeError, TypeError):
                continue
        return entradas

    def _gravar_ledger(self, entradas: list[Entrada]) -> None:
        tmp = self.ledger.with_suffix('.jsonl.tmp')
        with tmp.open('w', encoding='utf-8') as f:
            for e in entradas:
                f.write(json.dumps(asdict(e), ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.ledger)

    def _append(self, entrada: Entrada) -> None:
        with self.ledger.open('a', encoding='utf-8') as f:
            f.write(json.dumps(asdict(entrada), ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())

    def _blob(self, cid: str) -> Path:
        return self.blobs / f'{cid}.bak'

    def salvar(self, alvo: Path) -> str:
        rel = self._rel(alvo)
        cid = uuid.uuid4().hex[:8]
        p = self._abs(rel)
        if p.exists():
            dados = p.read_bytes()
            self._blob(cid).write_bytes(dados)
            existia = True
            hash_antes = _sha256(dados)
        else:
            existia = False
            hash_antes = ''
        entrada = Entrada(
            id=cid,
            arquivo=rel,
            existia=existia,
            hash_antes=hash_antes,
            hash_depois='',
            ts=__import__('time').time(),
            revertido=False,
        )
        self._append(entrada)
        return cid

    def confirmar(self, cid: str, alvo: Path) -> None:
        entradas = self._ler_ledger()
        idx = next((i for i, e in enumerate(entradas) if e.id == cid), None)
        if idx is None:
            raise KeyError(cid)
        rel = self._rel(alvo)
        p = self._abs(rel)
        if p.exists():
            hash_depois = _sha256(p.read_bytes())
        else:
            hash_depois = APAGADO
        entradas[idx] = replace(entradas[idx], hash_depois=hash_depois)
        self._gravar_ledger(entradas)

    def listar(self, limite: int = 20) -> list[Entrada]:
        entradas = self._ler_ledger()
        entradas.reverse()      # ordem do livro-razao (nao o relogio: no Windows ele tem ~15 ms de resolucao e empata)
        return entradas[:limite]

    def _restaurar(self, entrada: Entrada) -> None:
        p = self._abs(entrada.arquivo)
        if entrada.existia:
            dados = self._blob(entrada.id).read_bytes()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + '.tmp')
            tmp.write_bytes(dados)
            os.replace(tmp, p)
        else:
            if p.exists():
                p.unlink()

    def reverter(self, cid: str | None = None, *, forcar: bool = False) -> Relatorio:
        entradas = self._ler_ledger()
        if not entradas:
            if cid is not None:
                return Relatorio((), (), (f'id inexistente: {cid}',))
            return Relatorio((), (), ('nenhuma entrada',))

        if cid is None:
            alvo = next((e for e in reversed(entradas) if not e.revertido), None)
            if alvo is None:
                return Relatorio((), (), ('nada a reverter',))
            cid = alvo.id
        else:
            alvo = next((e for e in entradas if e.id == cid), None)
            if alvo is None:
                return Relatorio((), (), (f'id inexistente: {cid}',))
            if alvo.revertido:
                return Relatorio((), (), (f'ja revertido: {cid}',))

        arquivo = alvo.arquivo
        idx_alvo = entradas.index(alvo)
        alvos = [
            e for i, e in enumerate(entradas)
            if i >= idx_alvo and e.arquivo == arquivo and not e.revertido
        ]
        alvos.reverse()         # mais novo primeiro, pela posicao no livro-razao

        restaurados: list[str] = []
        pulados: list[str] = []
        avisos: list[str] = []

        for entrada in alvos:
            try:
                p = self._abs(entrada.arquivo)
                if p.exists():
                    atual = _sha256(p.read_bytes())
                else:
                    atual = APAGADO
                if atual != entrada.hash_depois and not forcar:
                    pulados.append(entrada.id)
                    avisos.append(
                        f'{entrada.id}: editado depois pelo usuario; '
                        'use forcar=True para sobrescrever'
                    )
                    continue
                self._restaurar(entrada)
                restaurados.append(entrada.id)
                idx = entradas.index(entrada)
                entradas[idx] = replace(entrada, revertido=True)
            except Exception as exc:
                avisos.append(f'{entrada.id}: {exc}')

        self._gravar_ledger(entradas)
        return Relatorio(tuple(restaurados), tuple(pulados), tuple(avisos))
