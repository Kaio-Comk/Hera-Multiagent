from __future__ import annotations

import fnmatch
from pathlib import Path

from app.privacy_guard import PrivacyGuard


class CaminhoNegado(Exception):
    '''Erro de caminho negado pela politica de seguranca.'''


_PADROES_SENSIVEIS = (
    '.env*',
    '*.pem',
    '*.key',
    'id_rsa*',
    '*.pfx',
    '.hera_token',
    'secrets*',
    'credentials*',
    '.npmrc',
    '.pypirc',
)

_PASTAS_SENSIVEIS = ('.git', '.ssh', '.aws')


class Raiz:
    '''Raiz de trabalho do agente; todo acesso fica confinado a ela.'''

    def __init__(self, pasta: Path) -> None:
        self.pasta = Path(pasta).resolve()

    def resolver(self, caminho: str | Path) -> Path:
        '''Resolve caminho relativo a raiz e exige que fique dentro dela.'''
        if caminho is None:
            raise CaminhoNegado('caminho vazio')
        texto = str(caminho)
        if texto == '' or '\x00' in texto:
            raise CaminhoNegado('caminho invalido')
        p = Path(caminho)
        if not p.is_absolute():
            p = self.pasta / p
        resolvido = p.resolve()
        if resolvido != self.pasta and self.pasta not in resolvido.parents:
            raise CaminhoNegado('fora da pasta de trabalho')
        return resolvido

    def relativo(self, caminho: str | Path) -> str:
        '''Devolve caminho relativo a raiz com barras "/".'''
        p = Path(caminho)
        if not p.is_absolute():
            p = self.pasta / p
        try:
            rel = p.resolve().relative_to(self.pasta)
        except ValueError:
            rel = p
        return str(rel).replace('\\', '/')

    def eh_sensivel(self, caminho: Path) -> bool:
        '''Indica se o caminho casa com nomes ou pastas sensiveis.'''
        p = Path(caminho)
        nome = p.name.lower()
        if ':' in nome:          # fluxo alternativo de dados do NTFS (arquivo:fluxo, ::$DATA): nunca
            return True
        nome = nome.rstrip(' .')  # o Windows ignora ponto e espaco no fim do nome
        for padrao in _PADROES_SENSIVEIS:
            if fnmatch.fnmatch(nome, padrao):
                return True
        for parte in p.parts:
            if parte.lower() in _PASTAS_SENSIVEIS:
                return True
        return False

    def exigir_leitura(self, caminho: str | Path) -> Path:
        '''Resolve e nega leitura de arquivos sensiveis.'''
        resolvido = self.resolver(caminho)
        if self.eh_sensivel(resolvido):
            raise CaminhoNegado('arquivo sensivel')
        return resolvido


def limpar_saida(texto: str, max_chars: int = 20000) -> str:
    '''Sanitiza o texto e corta no limite informado.'''
    limpo = PrivacyGuard.sanitize_content(texto)
    if len(limpo) <= max_chars:
        return limpo
    omitidos = len(limpo) - max_chars
    return limpo[:max_chars] + f'\n[cortado: {omitidos} caracteres omitidos]'
