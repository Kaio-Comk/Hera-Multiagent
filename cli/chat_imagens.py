# cli/chat_imagens.py
'''Suporte a imagens no chat: extracao de mencoes, validacao e montagem de conteudo multimodal.'''

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Sequence

EXTENSOES: dict[str, str] = {
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.gif': 'image/gif',
    '.webp': 'image/webp',
}

MODELOS_COM_VISAO: tuple[str, ...] = (
    'deepseek-ai/DeepSeek-V4.1-Flash',
    'gemini',
    'nvidia/NVIDIA-Nemotron-3.5-Super-VL',
    'gpt-4o',
    'claude',
    'sonnet',
    'opus',
    'haiku',
    'qwen/qwen3.8',
    'meta-llama/llama-4',
)

_PREFERIDOS: tuple[str, ...] = (
    'deepseek-ai/DeepSeek-V4.1-Flash',
    'gemini-flash-lite-latest',
    'nvidia/NVIDIA-Nemotron-3.5-Super-VL-120B-A12B-NVFP4',
    'gemini-flash-latest',
)


@dataclass(frozen=True)
class Imagem:
    '''Representa uma imagem carregada em memoria.'''

    caminho: str
    nome: str
    mime: str
    dados: bytes

    @property
    def tamanho_kb(self) -> float:
        '''Tamanho da imagem em kilobytes.'''
        return len(self.dados) / 1024.0


def _mime_pelos_bytes(dados: bytes) -> str | None:
    '''Detecta o mime type pelos bytes magicos do arquivo.'''
    if dados.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if dados.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if dados.startswith(b'GIF87a') or dados.startswith(b'GIF89a'):
        return 'image/gif'
    if dados.startswith(b'RIFF') and len(dados) >= 12 and dados[8:12] == b'WEBP':
        return 'image/webp'
    return None


def modelo_enxerga(nome: str) -> bool:
    '''Indica se o modelo (pelo nome) tem suporte a visao.'''
    if not nome:
        return False
    baixo = nome.lower()
    for item in MODELOS_COM_VISAO:
        alvo = item.lower()
        if baixo.startswith(alvo):
            return True
        if alvo in ('gemini', 'claude') and alvo in baixo:
            return True
    return False


def escolher_modelo_visao(
    disponiveis: Iterable[str],
    indisponiveis: Sequence[str] = (),
) -> str | None:
    '''Escolhe o melhor modelo com visao disponivel.'''
    disp = list(disponiveis)
    indisp = set(indisponiveis)
    for pref in _PREFERIDOS:
        if pref in disp and pref not in indisp:
            return pref
    for nome in disp:
        if nome in indisp:
            continue
        if modelo_enxerga(nome):
            return nome
    return None


# Regex para mencoes @caminho e @"caminho com espacos"
_RE_MENCAO_AT = re.compile(r'@"([^"]+)"|@(\S+)')
# Regex para caminhos absolutos do Windows entre aspas
_RE_WIN_ASPAS = re.compile(r'"([A-Za-z]:\\[^"]+)"')
# Caminhos absolutos do Windows sem aspas, COM ou sem espacos (como cola o arrastar-e-soltar ou o copiar do Explorer):
# vai da letra do drive ate a extensao de imagem, sem atravessar outro caminho nem aspas.
_RE_WIN_SEM_ASPAS = re.compile(
    r'([A-Za-z]:\\(?:(?![A-Za-z]:\\)[^"\n])*?\.(?:png|jpe?g|gif|webp))(?![\w\\])',
    re.IGNORECASE,
)


def _tem_extensao_valida(caminho: str) -> bool:
    '''Verifica se a extensao do caminho esta em EXTENSOES.'''
    ext = Path(caminho).suffix.lower()
    return ext in EXTENSOES


def _resolver(caminho: str, pasta: Path) -> Path:
    '''Resolve caminho relativo contra a pasta base; absoluto fica como esta.'''
    p = Path(caminho)
    if p.is_absolute():
        return p
    return pasta / p


def _carregar(
    caminho: Path,
    max_bytes: int,
    avisos: list[str],
    rotulo: str,
) -> Imagem | None:
    '''Le e valida uma imagem, adicionando avisos em caso de problema.'''
    try:
        if not caminho.is_file():
            avisos.append(f'imagem nao encontrada: {rotulo}')
            return None
        tamanho = caminho.stat().st_size
        if tamanho > max_bytes:
            mb = tamanho / (1024 * 1024)
            limite = max_bytes / (1024 * 1024)
            avisos.append(
                f'imagem grande demais ({mb:.1f} MB, limite {limite:.1f} MB): {rotulo}'
            )
            return None
        dados = caminho.read_bytes()
    except OSError:
        avisos.append(f'imagem nao encontrada: {rotulo}')
        return None
    mime = _mime_pelos_bytes(dados)
    if mime is None:
        avisos.append(f'formato de imagem invalido: {rotulo}')
        return None
    return Imagem(
        caminho=str(caminho),
        nome=caminho.name,
        mime=mime,
        dados=dados,
    )


def extrair_imagens(
    texto: str,
    pasta: Path,
    *,
    max_imagens: int = 4,
    max_bytes: int = 5_000_000,
    exportar_clipboard: Callable[[], Path | None] | None = None,
) -> tuple[str, list[Imagem], list[str]]:
    '''Extrai mencoes a imagens do texto, devolvendo texto limpo, imagens e avisos.'''
    avisos: list[str] = []
    imagens: list[Imagem] = []
    vistos: set[str] = set()
    pasta = Path(pasta)

    # Coleta todas as mencoes com suas posicoes no texto original
    mencoes: list[tuple[int, int, str, str]] = []  # (inicio, fim, tipo, valor)

    for m in _RE_MENCAO_AT.finditer(texto):
        if m.group(1) is not None:
            valor = m.group(1)
        else:
            valor = m.group(2)
        mencoes.append((m.start(), m.end(), 'at', valor))

    for m in _RE_WIN_ASPAS.finditer(texto):
        mencoes.append((m.start(), m.end(), 'win_aspas', m.group(1)))

    for m in _RE_WIN_SEM_ASPAS.finditer(texto):
        # Evita sobreposicao com mencoes ja capturadas
        sobrepoe = False
        for ini, fim, _, _ in mencoes:
            if not (m.end() <= ini or m.start() >= fim):
                sobrepoe = True
                break
        if not sobrepoe:
            mencoes.append((m.start(), m.end(), 'win_sem_aspas', m.group(1)))

    # Ordena por posicao e processa
    mencoes.sort(key=lambda t: t[0])

    # Aplica substituicoes de tras para frente para nao baguncar indices
    resultado = texto
    substituicoes: list[tuple[int, int, str]] = []

    for ini, fim, tipo, valor in mencoes:
        if tipo == 'at':
            if valor in ('clip', 'clipboard'):
                if exportar_clipboard is None:
                    avisos.append('area de transferencia sem imagem')
                    continue
                try:
                    caminho_clip = exportar_clipboard()
                except Exception:
                    caminho_clip = None
                if caminho_clip is None:
                    avisos.append('area de transferencia sem imagem')
                    continue
                caminho_clip = Path(caminho_clip)
                chave = str(caminho_clip)
                if chave in vistos:
                    substituicoes.append((ini, fim, f'[imagem: {caminho_clip.name}]'))
                    continue
                if len(imagens) >= max_imagens:
                    avisos.append(f'limite de {max_imagens} imagens')
                    continue
                img = _carregar(caminho_clip, max_bytes, avisos, caminho_clip.name)
                if img is None:
                    continue
                vistos.add(chave)
                imagens.append(img)
                substituicoes.append((ini, fim, f'[imagem: {img.nome}]'))
                continue

            if not _tem_extensao_valida(valor):
                continue
            caminho = _resolver(valor, pasta)
            chave = str(caminho)
            if chave in vistos:
                substituicoes.append((ini, fim, f'[imagem: {caminho.name}]'))
                continue
            if len(imagens) >= max_imagens:
                avisos.append(f'limite de {max_imagens} imagens')
                continue
            img = _carregar(caminho, max_bytes, avisos, valor)
            if img is None:
                continue
            vistos.add(chave)
            imagens.append(img)
            substituicoes.append((ini, fim, f'[imagem: {img.nome}]'))
            continue

        if tipo in ('win_aspas', 'win_sem_aspas'):
            if not _tem_extensao_valida(valor):
                continue
            caminho = Path(valor)
            chave = str(caminho)
            if chave in vistos:
                substituicoes.append((ini, fim, f'[imagem: {caminho.name}]'))
                continue
            if len(imagens) >= max_imagens:
                avisos.append(f'limite de {max_imagens} imagens')
                continue
            img = _carregar(caminho, max_bytes, avisos, valor)
            if img is None:
                continue
            vistos.add(chave)
            imagens.append(img)
            substituicoes.append((ini, fim, f'[imagem: {img.nome}]'))

    # Aplica substituicoes de tras para frente
    for ini, fim, novo in sorted(substituicoes, key=lambda t: t[0], reverse=True):
        resultado = resultado[:ini] + novo + resultado[fim:]

    return resultado, imagens, avisos


def montar_conteudo(texto: str, imagens: list[Imagem]) -> list[dict] | str:
    '''Monta o conteudo multimodal para envio ao modelo.'''
    if not imagens:
        return texto
    conteudo: list[dict] = [{'type': 'text', 'text': texto}]
    for img in imagens:
        b64 = base64.b64encode(img.dados).decode('ascii')
        conteudo.append({
            'type': 'image_url',
            'image_url': {'url': f'data:{img.mime};base64,{b64}'},
        })
    return conteudo


_SCRIPT_POWERSHELL = (
    'Add-Type -AssemblyName System.Windows.Forms;'
    'Add-Type -AssemblyName System.Drawing;'
    '$img = [System.Windows.Forms.Clipboard]::GetImage();'
    'if ($img -eq $null) { exit 1 };'
    '$img.Save($destino, [System.Drawing.Imaging.ImageFormat]::Png);'
    'exit 0'
)


def imagem_da_area_de_transferencia(
    executar: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Path | None:
    '''Obtem imagem da area de transferencia (Windows) e salva em PNG temporario.'''
    if sys.platform != 'win32':
        return None
    fd, caminho_str = tempfile.mkstemp(suffix='.png')
    os.close(fd)
    caminho = Path(caminho_str)
    try:
        resultado = executar(
            [
                'powershell',
                '-NoProfile',
                '-STA',
                '-Command',
                # no powershell -Command os argumentos finais nao viram $args: o destino vai dentro do script
                "$destino = '" + caminho_str.replace("'", "''") + "';" + _SCRIPT_POWERSHELL,
            ],
            timeout=10,
            capture_output=True,
        )
    except Exception:
        try:
            caminho.unlink()
        except OSError:
            pass
        return None
    if resultado.returncode != 0:
        try:
            caminho.unlink()
        except OSError:
            pass
        return None
    try:
        if not caminho.is_file() or caminho.stat().st_size == 0:
            caminho.unlink()
            return None
    except OSError:
        return None
    return caminho
