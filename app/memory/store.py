"""Store de notas de memoria em arquivos Markdown."""

from __future__ import annotations

import os
import tempfile
from datetime import date
from pathlib import Path

from app.privacy_guard import PrivacyGuard

from .hashes import IndiceDeHash, hash_do_corpo
from .schema import Nota, validar_nome, validar_tipo


class DuplicateNoteError(Exception):
    """Erro levantado quando outra nota ja tem o mesmo corpo."""


class MemoryStore:
    """Armazena notas Markdown em um diretorio base."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._hashes = IndiceDeHash(self.base_dir, self._corpo_de)

    def _corpo_de(self, caminho: Path) -> str | None:
        """Corpo gravado de uma nota (None se ilegivel); usado pelo cache de hashes."""
        nota = self._ler_nota(caminho)
        return None if nota is None else nota.corpo

    def _caminho(self, nome: str) -> Path:
        """Valida o nome e devolve o caminho resolvido dentro de base_dir."""
        validar_nome(nome)
        base = self.base_dir.resolve()
        caminho = (base / f'{nome}.md').resolve()
        if caminho != base and base not in caminho.parents:
            raise ValueError(f'caminho fora de base_dir: {nome!r}')
        return caminho

    def _ler_nota(self, caminho: Path) -> Nota | None:
        """Le uma nota do disco; devolve None se o frontmatter for invalido."""
        try:
            texto = caminho.read_text(encoding='utf-8')
        except OSError:
            return None
        linhas = texto.split('\n')
        if not linhas or linhas[0].strip() != '---':
            return None
        meta: dict[str, str] = {}
        i = 1
        while i < len(linhas) and linhas[i].strip() != '---':
            linha = linhas[i]
            if ':' not in linha:
                return None
            chave, _, valor = linha.partition(':')
            meta[chave.strip()] = valor.strip()
            i += 1
        if i >= len(linhas):
            return None
        corpo = '\n'.join(linhas[i + 1:])
        if corpo.startswith('\n'):
            corpo = corpo[1:]
        nome = meta.get('name')
        descricao = meta.get('description', '')
        tipo = meta.get('type', '')
        data = meta.get('date', '')
        if not nome or not tipo or not data:
            return None
        try:
            validar_nome(nome)
            validar_tipo(tipo)
        except ValueError:
            return None
        return Nota(nome=nome, descricao=descricao, tipo=tipo, corpo=corpo, data=data)

    def save(
        self,
        nome: str,
        descricao: str,
        tipo: str,
        corpo: str,
        *,
        overwrite: bool = False,
    ) -> Path:
        """Sanitiza e grava a nota de forma atomica."""
        caminho = self._caminho(nome)
        validar_tipo(tipo)

        descricao_limpa = PrivacyGuard.sanitize_content(descricao).replace('\n', ' ').replace('\r', ' ')
        corpo_limpo = PrivacyGuard.sanitize_content(corpo)

        if caminho.exists() and not overwrite:
            raise FileExistsError(f'nota ja existe: {nome}')

        outra = self._hashes.buscar_duplicata(hash_do_corpo(corpo_limpo), excluir=caminho)
        if outra is not None:
            raise DuplicateNoteError(f'corpo duplicado com nota {outra!r}')

        hoje = date.today().isoformat()
        conteudo = (
            '---\n'
            f'name: {nome}\n'
            f'description: {descricao_limpa}\n'
            f'type: {tipo}\n'
            f'date: {hoje}\n'
            '---\n'
            '\n'
            f'{corpo_limpo}'
        )

        fd, tmp_nome = tempfile.mkstemp(dir=str(self.base_dir), suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write(conteudo)
            os.replace(tmp_nome, caminho)
            self._hashes.invalidar(caminho)
        except BaseException:
            try:
                os.unlink(tmp_nome)
            except OSError:
                pass
            raise
        return caminho

    def get(self, nome: str) -> Nota:
        """Le a nota pelo nome."""
        caminho = self._caminho(nome)
        if not caminho.exists():
            raise FileNotFoundError(f'nota nao encontrada: {nome}')
        nota = self._ler_nota(caminho)
        if nota is None:
            raise FileNotFoundError(f'nota invalida: {nome}')
        return nota

    def listar(self) -> list[Nota]:
        """Lista notas validas ordenadas por nome."""
        notas: list[Nota] = []
        for caminho in self.base_dir.glob('*.md'):
            nota = self._ler_nota(caminho)
            if nota is not None:
                notas.append(nota)
        notas.sort(key=lambda n: n.nome)
        return notas

    def delete(self, nome: str) -> bool:
        """Apaga a nota; devolve True se removeu."""
        caminho = self._caminho(nome)
        if caminho.exists():
            caminho.unlink()
            self._hashes.invalidar(caminho)
            return True
        return False
