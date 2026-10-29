from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from app.privacy_guard import PrivacyGuard

NOMES_CANDIDATOS = ('HERA.md', 'AGENTS.md', 'CLAUDE.md')

INVISIVEIS = (
    '\u200b', '\u200c', '\u200d', '\u200e', '\u200f',
    '\u2060', '\u2061', '\u2062', '\u2063', '\u2064',
    '\u202a', '\u202b', '\u202c', '\u202d', '\u202e',
    '\u2066', '\u2067', '\u2068', '\u2069',
    '\ufeff',
)

FRASES_INJECAO = (
    'ignore previous instructions',
    'ignore all previous',
    'disregard the above',
    'ignore as instrucoes anteriores',
    'ignore todas as instrucoes',
    'you are now',
    'system prompt:',
    'novo prompt de sistema',
)

REGRAS_BASE = (
    'Voce e o Hera, agente de engenharia de software.\n'
    'Responda sempre em portugues do Brasil, de forma clara e direta.\n'
    'Peca aprovacao explicita antes de qualquer acao destrutiva.\n'
    'Nunca revele segredos, chaves ou credenciais em respostas.\n'
    'Seja conciso: evite repetir o que ja foi dito.\n'
    'Leia o arquivo inteiro antes de editar qualquer trecho.\n'
    'Prefira mudancas pequenas e verificaveis.\n'
    'Se faltar informacao essencial, pergunte antes de agir.'
)


@dataclass(frozen=True)
class Fonte:
    caminho: str
    origem: str
    bytes_lidos: int
    truncada: bool


@dataclass(frozen=True)
class Instrucoes:
    texto: str
    fontes: tuple[Fonte, ...]
    avisos: tuple[str, ...]


def achar_raiz_git(pasta: Path) -> Path | None:
    atual = Path(pasta).resolve()
    for candidato in (atual, *atual.parents):
        git = candidato / '.git'
        if git.is_dir() or git.is_file():
            return candidato
    return None


def _limpar_invisiveis(texto: str) -> str:
    for ch in INVISIVEIS:
        texto = texto.replace(ch, '')
    return texto


def _detectar_injecao(texto: str) -> str | None:
    baixo = texto.lower()
    for frase in FRASES_INJECAO:
        if frase in baixo:
            return frase
    return None


def _rotulo(caminho: Path, home: Path) -> str:
    try:
        rel = caminho.relative_to(home)
        return '~/' + str(rel).replace('\\', '/')
    except ValueError:
        return str(caminho).replace('\\', '/')


def _ler_arquivo(caminho: Path) -> str | None:
    try:
        return caminho.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None


def _escolher_em_dir(pasta: Path) -> Path | None:
    for nome in NOMES_CANDIDATOS:
        alvo = pasta / nome
        if alvo.is_file():
            return alvo
    return None


def _global_path(home: Path) -> Path | None:
    a = home / '.hera' / 'HERA.md'
    if a.is_file():
        return a
    b = home / '.claude' / 'CLAUDE.md'
    if b.is_file():
        return b
    return None


def _cadeia_projeto(pasta_atual: Path) -> list[Path]:
    pasta_atual = Path(pasta_atual).resolve()
    raiz = achar_raiz_git(pasta_atual)
    if raiz is None:
        return [pasta_atual]
    cadeia: list[Path] = []
    atual = pasta_atual
    while True:
        cadeia.append(atual)
        if atual == raiz:
            break
        pai = atual.parent
        if pai == atual:
            break
        atual = pai
    cadeia.reverse()
    return cadeia


def _processar(
    caminho: Path,
    origem: str,
    home: Path,
    max_chars_arquivo: int,
    permitir_injecao: bool,
) -> tuple[str | None, Fonte | None, list[str]]:
    avisos: list[str] = []
    bruto = _ler_arquivo(caminho)
    if bruto is None:
        avisos.append(f'Arquivo {caminho} ilegivel; ignorado.')
        return None, None, avisos
    bytes_lidos = len(bruto.encode('utf-8', errors='replace'))
    limpo = _limpar_invisiveis(bruto)
    limpo = PrivacyGuard.sanitize_content(limpo)
    truncada = False
    if len(limpo) > max_chars_arquivo:
        total = len(limpo)
        limpo = limpo[:max_chars_arquivo]
        limpo += (
            f'\n[cortado: {caminho.name} tem {total} caracteres; '
            f'mostrando {max_chars_arquivo}. Leia o arquivo para o restante.]'
        )
        truncada = True
    if origem == 'projeto':
        frase = _detectar_injecao(limpo)
        if frase is not None:
            if permitir_injecao:
                avisos.append(
                    f'Arquivo {caminho} contem possivel injecao de prompt '
                    f'(frase: {frase}); permitido por configuracao.'
                )
            else:
                avisos.append(
                    f'Arquivo {caminho} bloqueado: possivel injecao de prompt '
                    f'(frase: {frase}). Revise o arquivo.'
                )
                return None, None, avisos
    rotulo = _rotulo(caminho, home)
    bloco = f'## Instrucoes de {rotulo} ({origem})\n{limpo}'
    fonte = Fonte(
        caminho=str(caminho),
        origem=origem,
        bytes_lidos=bytes_lidos,
        truncada=truncada,
    )
    return bloco, fonte, avisos


def descobrir_instrucoes(
    pasta_atual: Path,
    *,
    home: Path | None = None,
    max_chars_arquivo: int = 8000,
    max_chars_total: int = 20000,
    permitir_injecao: bool = False,
) -> Instrucoes:
    home = Path(home) if home is not None else Path.home()
    pasta_atual = Path(pasta_atual).resolve()

    blocos: list[tuple[str, Fonte]] = []
    avisos: list[str] = []

    g = _global_path(home)
    if g is not None:
        bloco, fonte, av = _processar(
            g, 'global', home, max_chars_arquivo, permitir_injecao
        )
        avisos.extend(av)
        if bloco is not None and fonte is not None:
            blocos.append((bloco, fonte))

    for pasta in _cadeia_projeto(pasta_atual):
        escolhido = _escolher_em_dir(pasta)
        if escolhido is None:
            continue
        bloco, fonte, av = _processar(
            escolhido, 'projeto', home, max_chars_arquivo, permitir_injecao
        )
        avisos.extend(av)
        if bloco is not None and fonte is not None:
            blocos.append((bloco, fonte))

    def _total() -> int:
        # soma dos blocos mais os separadores '\n\n' que o join coloca entre eles
        return sum(len(b) for b, _ in blocos) + 2 * max(len(blocos) - 1, 0)

    while _total() > max_chars_total and blocos:
        bloco, fonte = blocos[0]
        excesso = _total() - max_chars_total
        nota = (
            f'\n[cortado: teto total de {max_chars_total} caracteres atingido; '
            f'bloco {fonte.caminho} truncado.]'
        )
        if len(bloco) <= excesso + len(nota):      # nao sobra espaco nem para o aviso: remove o bloco inteiro
            avisos.append(
                f'Bloco {fonte.caminho} removido por exceder o teto total de caracteres.'
            )
            blocos.pop(0)
            continue
        avisos.append(
            f'Bloco {fonte.caminho} truncado por exceder o teto total de caracteres.'
        )
        blocos[0] = (bloco[: len(bloco) - excesso - len(nota)] + nota, fonte)

    texto = '\n\n'.join(b for b, _ in blocos)
    fontes = tuple(f for _, f in blocos)
    return Instrucoes(texto=texto, fontes=fontes, avisos=tuple(avisos))


def montar_prompt_sistema(
    instrucoes: Instrucoes,
    *,
    agora: datetime | None = None,
    memoria: str = '',
    catalogo_skills: str = '',
    pasta: Path | None = None,
) -> str:
    estavel: list[str] = [REGRAS_BASE]
    if instrucoes.texto:
        estavel.append(instrucoes.texto)
    if catalogo_skills:
        estavel.append('## Catalogo de skills\n' + catalogo_skills)
    for aviso in instrucoes.avisos:
        estavel.append(f'[aviso] {aviso}')
    prefixo = '\n\n'.join(estavel)

    quando = agora if agora is not None else datetime.now()
    volatil: list[str] = [quando.strftime('%Y-%m-%d %H:%M')]
    if pasta is not None:
        volatil.append(str(pasta))
    if memoria:
        volatil.append(memoria)

    return prefixo + '\n\n---\n' + '\n'.join(volatil)
