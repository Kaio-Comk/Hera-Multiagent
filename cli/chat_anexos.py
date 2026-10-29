from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from app.agente.caminhos import Raiz, CaminhoNegado, limpar_saida
from app.agente.leitura import Leitura


@dataclass(frozen=True)
class Anexo:
    token: str
    caminho: str
    linhas: str


_PADRAO = re.compile(
    r'(?:(?<=^)|(?<=[\s(\"\']))@'
    r'(?:\"([^\"]+)\"|([^\s]+))'
)


def _parse_intervalo(s: str) -> tuple[str, str | None]:
    m = re.match(r'^(.*?):(\d+)(?:-(\d+))?$', s)
    if not m:
        return s, None
    caminho = m.group(1)
    a = m.group(2)
    b = m.group(3)
    if b is None:
        return caminho, f'{a}-{a}'
    return caminho, f'{a}-{b}'


def _parece_mencao(token: str) -> bool:
    return '/' not in token and '\\' not in token and '.' not in token


def expandir_anexos(
    texto: str,
    raiz: Raiz,
    *,
    max_anexos: int = 5,
    max_chars: int = 16000,
) -> tuple[str, list[str], list[Anexo]]:
    avisos: list[str] = []
    anexos: list[Anexo] = []
    conteudos: list[str] = []          # conteudo lido de cada anexo, na mesma ordem de `anexos`
    vistos: set[tuple[str, str]] = set()
    leitura = Leitura(raiz)
    total = 0

    for m in _PADRAO.finditer(texto):
        bruto = m.group(1) if m.group(1) is not None else m.group(2)
        if bruto is None:
            continue
        if '@' in bruto:
            continue
        caminho_str, intervalo = _parse_intervalo(bruto)
        if not caminho_str:
            continue

        try:
            p = raiz.resolver(caminho_str)
        except CaminhoNegado:
            avisos.append(f'fora da pasta de trabalho: {caminho_str}')
            continue

        if not p.exists():
            if _parece_mencao(caminho_str):
                continue
            avisos.append(f'nao encontrado: {caminho_str}')
            continue

        if p.is_file() and raiz.eh_sensivel(p):
            avisos.append(f'arquivo sensivel recusado: {caminho_str}')
            continue

        chave = (str(p), intervalo or 'todas')
        if chave in vistos:
            continue

        if len(anexos) >= max_anexos:
            avisos.append('limite de anexos')
            continue

        if p.is_dir():
            res = leitura.listar_pasta(caminho_str)
            linhas = 'todas'
        else:
            if intervalo:
                a, b = intervalo.split('-')
                res = leitura.ler_arquivo(caminho_str, inicio=int(a), fim=int(b))
            else:
                res = leitura.ler_arquivo(caminho_str)
            linhas = intervalo or 'todas'

        if not res.ok:
            avisos.append(f'erro ao ler: {caminho_str}')
            continue

        conteudo = limpar_saida(res.texto, max_chars)
        if total + len(conteudo) > max_chars:
            restante = max_chars - total
            if restante <= 0:
                conteudo = '[cortado]'
            else:
                conteudo = conteudo[:restante] + '[cortado]'
        total += len(conteudo)

        vistos.add(chave)
        rel = raiz.relativo(caminho_str)
        anexos.append(Anexo(token=m.group(0), caminho=rel, linhas=linhas))
        conteudos.append(conteudo)

    if not anexos:
        return texto, avisos, anexos

    partes = [texto, '\n\n[Arquivos anexados]\n']
    for a, conteudo_a in zip(anexos, conteudos):
        partes.append(f'### {a.caminho} (linhas {a.linhas})\n```\n')
        partes.append(conteudo_a)
        partes.append('\n```\n')

    return ''.join(partes), avisos, anexos


def resumo_anexos(anexos: list[Anexo]) -> str:
    if not anexos:
        return ''
    itens = ', '.join(f'{a.caminho} ({a.linhas})' for a in anexos)
    return f'[anexado: {itens}]'
