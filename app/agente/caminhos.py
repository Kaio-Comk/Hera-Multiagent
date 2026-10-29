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
    '.netrc',
    '.credentials*',      # ex.: ~/.claude/.credentials.json
    '.claude.json',
    '*.session',          # sessoes do Telegram (Telethon)
    '*.kdbx',
    '*.ppk',
    'id_ed25519*',
    'cookies*',
    'login data*',
)

_PASTAS_SENSIVEIS = ('.git', '.ssh', '.aws', '.gnupg', '.kube', '.docker')
# So contam quando estao DENTRO da pasta de trabalho (abrir o chat na pasta pessoal expoe o AppData; trabalhar la dentro e outra coisa).
_PASTAS_SENSIVEIS_NA_RAIZ = ('appdata',)


class Raiz:
    '''Raiz de trabalho do agente; todo acesso fica confinado a ela.'''

    def __init__(self, pasta: Path) -> None:
        self.pasta = Path(pasta).resolve()
        self.leitura_extra: list[Path] = []   # pastas liberadas so para LEITURA (/pasta); escrever segue so na raiz

    def permitir_leitura(self, pasta: str | Path) -> Path:
        '''Libera uma pasta extra SO para leitura (o usuario pediu: /pasta ou --pasta). Nunca libera pasta sensivel.'''
        alvo = Path(pasta).expanduser().resolve()
        if not alvo.is_dir():
            raise CaminhoNegado('pasta inexistente')
        if self.eh_sensivel(alvo):
            raise CaminhoNegado('pasta sensivel')
        if alvo != self.pasta and alvo not in self.leitura_extra:
            self.leitura_extra.append(alvo)
        return alvo

    def resolver(self, caminho: str | Path, *, leitura: bool = False) -> Path:
        '''Resolve caminho relativo a raiz e exige que fique dentro dela (para leitura, tambem dentro das pastas liberadas).'''
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
            if leitura and any(resolvido == e or e in resolvido.parents for e in self.leitura_extra):
                return resolvido
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
        try:
            relativas = p.resolve().relative_to(self.pasta).parts
        except (ValueError, OSError):
            relativas = ()
        return any(parte.lower() in _PASTAS_SENSIVEIS_NA_RAIZ for parte in relativas)

    def exigir_leitura(self, caminho: str | Path) -> Path:
        '''Resolve e nega leitura de arquivos sensiveis.'''
        resolvido = self.resolver(caminho, leitura=True)
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
