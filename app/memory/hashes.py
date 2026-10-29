from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Callable


def hash_do_corpo(corpo: str) -> str:
    '''Calcula o sha256 do corpo sem espacos nas pontas.'''
    return hashlib.sha256(corpo.strip().encode('utf-8')).hexdigest()


class IndiceDeHash:
    '''Cache de hashes de notas Markdown em um diretorio base.'''

    def __init__(
        self,
        base_dir: Path,
        ler_corpo: Callable[[Path], str | None],
    ) -> None:
        self._base_dir = Path(base_dir).resolve()  # resolvido uma vez: resolve() por arquivo e lento no Windows
        self._ler_corpo = ler_corpo
        self._cache: dict[Path, tuple[int, int, str | None]] = {}
        self._lock = threading.Lock()

    def _atualizar(self) -> None:
        '''Sincroniza o cache com o disco, relendo apenas o que mudou.'''
        vistos: set[Path] = set()
        for caminho in self._base_dir.glob('*.md'):
            try:
                st = caminho.stat()
            except OSError:
                continue
            chave = caminho
            vistos.add(chave)
            mtime_ns = st.st_mtime_ns
            tamanho = st.st_size
            entrada = self._cache.get(chave)
            if entrada is not None and entrada[0] == mtime_ns and entrada[1] == tamanho:
                continue
            corpo = self._ler_corpo(caminho)
            if corpo is None:
                self._cache[chave] = (mtime_ns, tamanho, None)
            else:
                self._cache[chave] = (mtime_ns, tamanho, hash_do_corpo(corpo))
        for chave in list(self._cache):
            if chave not in vistos:
                del self._cache[chave]

    def buscar_duplicata(self, hash_corpo: str, excluir: Path | None = None) -> str | None:
        '''Devolve o stem de outra nota com o mesmo hash, ou None.'''
        alvo = excluir.resolve() if excluir is not None else None
        with self._lock:
            self._atualizar()
            for chave, (_, _, h) in self._cache.items():
                if h is None:
                    continue
                if h != hash_corpo:
                    continue
                if alvo is not None and chave == alvo:
                    continue
                return chave.stem
        return None

    def invalidar(self, caminho: Path) -> None:
        '''Descarta a entrada de cache de um arquivo.'''
        chave = Path(caminho).resolve()
        with self._lock:
            self._cache.pop(chave, None)
