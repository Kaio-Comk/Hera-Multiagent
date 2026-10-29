from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import yaml

from app.privacy_guard import PrivacyGuard

_NOME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$')

_PADROES_INCOMPAT: tuple[tuple[str, str], ...] = (
    ('#!/bin/bash', 'usa bash'),
    ('#!/usr/bin/env bash', 'usa bash'),
    ('chmod ', 'usa chmod'),
    ('/tmp/', 'usa /tmp'),
    ('sudo ', 'usa sudo'),
    ('apt-get ', 'usa apt-get/brew'),
    ('apt install', 'usa apt-get/brew'),
    ('brew install', 'usa apt-get/brew'),
    ('rm -rf /', 'usa rm -rf /'),
)

# ambientes virtuais, caches e dependencias nunca entram numa skill (ex.: a skill get-brolls carrega um .venv inteiro)
_PULAR_DIRS = {'.git', 'node_modules', '__pycache__', '.venv', 'venv', 'env', 'site-packages', '.tox', '.mypy_cache', '.pytest_cache', '.ruff_cache', '.idea', '.vscode'}
_PULAR_SUFIXOS = ('.pem', '.key')
_PULAR_NOMES_PREFIXO = ('.env', 'id_rsa')
_MAX_ARQUIVO = 1024 * 1024


@dataclass(frozen=True)
class Skill:
    nome: str
    descricao: str
    caminho: Path
    pasta: Path
    origem: str
    avisos: tuple[str, ...]


@dataclass(frozen=True)
class RelatorioImportacao:
    copiadas: tuple[str, ...]
    ignoradas: tuple[str, ...]
    sobrescritas: tuple[str, ...]
    avisos: tuple[str, ...]


def _normalizar_descricao(valor: Any) -> str:
    if valor is None:
        return ''
    texto = str(valor)
    return ' '.join(texto.split())


def separar_frontmatter(texto: str) -> tuple[dict, str]:
    if not texto.startswith('---'):
        return {}, texto
    linhas = texto.splitlines(keepends=True)
    if not linhas or linhas[0].strip() != '---':
        return {}, texto
    fim = None
    for i in range(1, len(linhas)):
        if linhas[i].strip() == '---':
            fim = i
            break
    if fim is None:
        return {}, texto
    bloco = ''.join(linhas[1:fim])
    corpo = ''.join(linhas[fim + 1:])
    dados: Any = None
    try:
        dados = yaml.safe_load(bloco)
    except Exception:
        dados = None
    if not isinstance(dados, dict):
        dados = _parser_simples(bloco)
    return dados, corpo


def _parser_simples(bloco: str) -> dict:
    resultado: dict[str, str] = {}
    for linha in bloco.splitlines():
        if not linha.strip() or linha.lstrip().startswith('#'):
            continue
        if ':' not in linha:
            continue
        chave, _, valor = linha.partition(':')
        chave = chave.strip()
        if not chave:
            continue
        resultado[chave] = valor.strip()
    return resultado


def detectar_incompatibilidade_windows(texto: str) -> tuple[str, ...]:
    motivos: list[str] = []
    for padrao, motivo in _PADROES_INCOMPAT:
        if padrao in texto and motivo not in motivos:
            motivos.append(motivo)
    return tuple(motivos)


def descobrir_skills(pastas: Sequence[Path]) -> list[Skill]:
    vistos: dict[str, Skill] = {}
    for pasta in pastas:
        try:
            if not pasta.is_dir():
                continue
            subpastas = sorted(p for p in pasta.iterdir() if p.is_dir())
        except OSError:
            continue
        for sub in subpastas:
            skill_md = sub / 'SKILL.md'
            try:
                if not skill_md.is_file():
                    continue
                texto = skill_md.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            fm, _ = separar_frontmatter(texto)
            nome = fm.get('name')
            if not isinstance(nome, str) or not nome.strip():
                nome = sub.name
            nome = nome.strip()
            if not _NOME_RE.match(nome):
                continue
            if nome in vistos:
                continue
            descricao = _normalizar_descricao(fm.get('description'))
            avisos = detectar_incompatibilidade_windows(texto)
            vistos[nome] = Skill(
                nome=nome,
                descricao=descricao,
                caminho=skill_md,
                pasta=sub,
                origem=str(pasta),
                avisos=avisos,
            )
    return sorted(vistos.values(), key=lambda s: s.nome)


def catalogo_para_prompt(skills: Sequence[Skill], max_chars: int = 3000) -> str:
    if not skills:
        return ''
    cabecalho = (
        'Skills disponiveis (para usar, leia o SKILL.md indicado; '
        'o usuario tambem pode chamar com /nome):'
    )
    linhas: list[str] = [cabecalho]
    omitidas = 0
    for skill in skills:
        desc = skill.descricao
        if len(desc) > 160:
            desc = desc[:160] + '...'
        linha = f'- {skill.nome}: {desc} ({skill.caminho})'
        candidato = '\n'.join(linhas + [linha])
        if len(candidato) > max_chars:
            omitidas = len(skills) - (len(linhas) - 1)
            break
        linhas.append(linha)
    if omitidas > 0:
        linhas.append(f'(+{omitidas} skills omitidas; use /skills)')
    return '\n'.join(linhas)


def ler_skill(skill: Skill, max_chars: int = 20000) -> str:
    try:
        texto = skill.caminho.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    _, corpo = separar_frontmatter(texto)
    if len(corpo) > max_chars:
        cortado = len(corpo) - max_chars
        corpo = corpo[:max_chars] + f'\n[cortado: {cortado} caracteres]'
    return corpo


def resolver_skill(nome: str, skills: Sequence[Skill]) -> Skill | None:
    if not nome:
        return None
    alvo = nome.lower()
    for skill in skills:
        if skill.nome.lower() == alvo:
            return skill
    candidatos = [s for s in skills if s.nome.lower().startswith(alvo)]
    if len(candidatos) == 1:
        return candidatos[0]
    return None


def _nome_valido(nome: str) -> bool:
    return bool(_NOME_RE.match(nome))


def _deve_pular_arquivo(caminho: Path) -> bool:
    nome = caminho.name
    if nome in _PULAR_DIRS:
        return True
    for prefixo in _PULAR_NOMES_PREFIXO:
        if nome.startswith(prefixo):
            return True
    for sufixo in _PULAR_SUFIXOS:
        if nome.endswith(sufixo):
            return True
    return False


def _dentro_de(base: Path, alvo: Path) -> bool:
    try:
        base_r = base.resolve()
        alvo_r = alvo.resolve()
    except OSError:
        return False
    try:
        alvo_r.relative_to(base_r)
        return True
    except ValueError:
        return False


def importar_do_claude(
    origem: Path | None = None,
    destino: Path | None = None,
    *,
    sobrescrever: bool = False,
) -> RelatorioImportacao:
    origem = origem if origem is not None else Path.home() / '.claude' / 'skills'
    destino = destino if destino is not None else Path.home() / '.hera' / 'skills'

    copiadas: list[str] = []
    ignoradas: list[str] = []
    sobrescritas: list[str] = []
    avisos: list[str] = []

    if not origem.is_dir():
        return RelatorioImportacao((), (), (), (f'origem inexistente: {origem}',))

    try:
        destino.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return RelatorioImportacao((), (), (), (f'nao foi possivel criar destino: {exc}',))

    try:
        subpastas = sorted(p for p in origem.iterdir() if p.is_dir())
    except OSError as exc:
        return RelatorioImportacao((), (), (), (f'origem ilegivel: {exc}',))

    for sub in subpastas:
        if sub.is_symlink():
            ignoradas.append(sub.name)
            continue
        skill_md = sub / 'SKILL.md'
        if not skill_md.is_file():
            continue
        if not _nome_valido(sub.name):
            ignoradas.append(sub.name)
            continue

        destino_skill = destino / sub.name
        if not _dentro_de(destino, destino_skill):
            ignoradas.append(sub.name)
            avisos.append(f'{sub.name}: caminho fora do destino')
            continue

        if destino_skill.exists():
            if not sobrescrever:
                ignoradas.append(sub.name)
                continue

        try:
            texto_skill = skill_md.read_text(encoding='utf-8', errors='replace')
        except OSError:
            ignoradas.append(sub.name)
            continue

        avisos_skill = detectar_incompatibilidade_windows(texto_skill)
        for motivo in avisos_skill:
            avisos.append(f'{sub.name}: {motivo}')

        arquivos: list[tuple[Path, Path]] = []
        segredo_encontrado = False
        try:
            for raiz, dirs, files in _walk_seguro(sub):
                dirs[:] = [d for d in dirs if d not in _PULAR_DIRS]
                for f in files:
                    src = raiz / f
                    if src.is_symlink():
                        continue
                    if _deve_pular_arquivo(src):
                        continue
                    try:
                        if src.stat().st_size > _MAX_ARQUIVO:
                            continue
                    except OSError:
                        continue
                    rel = src.relative_to(sub)
                    dst = destino_skill / rel
                    if not _dentro_de(destino, dst):
                        continue
                    if _parece_texto(src):
                        try:
                            conteudo = src.read_text(encoding='utf-8', errors='replace')
                        except OSError:
                            continue
                        achou, _ = PrivacyGuard.scan_content(conteudo)
                        if achou:
                            segredo_encontrado = True
                            avisos.append(f'{sub.name}: segredo detectado em {rel}, arquivo nao copiado')
                            continue
                    arquivos.append((src, dst))
        except OSError:
            ignoradas.append(sub.name)
            continue

        if segredo_encontrado and not arquivos:
            ignoradas.append(sub.name)
            continue

        try:
            if destino_skill.exists():
                shutil.rmtree(destino_skill)
            destino_skill.mkdir(parents=True, exist_ok=True)
            for src, dst in arquivos:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        except OSError as exc:
            avisos.append(f'{sub.name}: falha ao copiar: {exc}')
            ignoradas.append(sub.name)
            continue

        if destino_skill.exists() and sub.name in sobrescritas:
            pass
        if sobrescrever and (destino / sub.name).exists():
            sobrescritas.append(sub.name)
        else:
            copiadas.append(sub.name)

    return RelatorioImportacao(
        tuple(copiadas),
        tuple(ignoradas),
        tuple(sobrescritas),
        tuple(avisos),
    )


def _walk_seguro(base: Path):
    for raiz, dirs, files in base.walk():
        yield raiz, dirs, files


def _parece_texto(caminho: Path) -> bool:
    try:
        with caminho.open('rb') as fh:
            amostra = fh.read(4096)
    except OSError:
        return False
    if b'\x00' in amostra:
        return False
    return True
