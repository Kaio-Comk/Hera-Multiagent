from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path

from app.agente.caminhos import CaminhoNegado, Raiz, limpar_saida

_MAX_LINHAS = 400
_MAX_CHARS = 20000
_MAX_BYTES_ARQUIVO = 2 * 1024 * 1024
_MAX_BYTES_BUSCA = 1024 * 1024
_MAX_RESULTADOS_BUSCA = 100
_MAX_ENTRADAS_LISTA = 200
_MAX_CHARS_LINHA_BUSCA = 200

_PASTAS_OCULTAS = {'.git', 'node_modules', '.venv', 'venv', '__pycache__'}


@dataclass(frozen=True)
class Resultado:
    '''Resultado de uma ferramenta de leitura.'''

    ok: bool
    texto: str


def _eh_binario(caminho: Path, limite: int = 4096) -> bool:
    '''Detecta byte nulo nos primeiros bytes do arquivo.'''
    try:
        with caminho.open('rb') as fh:
            amostra = fh.read(limite)
    except OSError:
        return False
    return b'\x00' in amostra


class Leitura:
    '''Ferramentas de leitura que nunca levantam excecao.'''

    def __init__(self, raiz: Raiz) -> None:
        self.raiz = raiz

    def ler_arquivo(
        self,
        caminho: str | Path,
        inicio: int = 1,
        fim: int | None = None,
    ) -> Resultado:
        '''Le arquivo de texto com linhas numeradas e limites.'''
        try:
            alvo = self.raiz.exigir_leitura(caminho)
        except CaminhoNegado as exc:
            return Resultado(False, str(exc))
        if not alvo.exists():
            return Resultado(False, 'arquivo inexistente')
        if not alvo.is_file():
            return Resultado(False, 'nao e um arquivo')
        try:
            tamanho = alvo.stat().st_size
        except OSError as exc:
            return Resultado(False, f'erro ao acessar: {exc}')
        if tamanho > _MAX_BYTES_ARQUIVO:
            return Resultado(
                False,
                'arquivo grande demais (>2 MB); use buscar para localizar trechos',
            )
        if _eh_binario(alvo):
            return Resultado(False, 'arquivo binario')
        try:
            conteudo = alvo.read_text(encoding='utf-8', errors='replace')
        except OSError as exc:
            return Resultado(False, f'erro ao ler: {exc}')
        linhas = conteudo.splitlines()
        total = len(linhas)
        if total == 0:
            return Resultado(True, limpar_saida(''))
        ini = max(1, int(inicio))
        f = int(fim) if fim is not None else total
        if f < ini:
            f = ini
        if f > total:
            f = total
        selecionadas = linhas[ini - 1 : f]
        aviso_corte = False
        if len(selecionadas) > _MAX_LINHAS:
            selecionadas = selecionadas[:_MAX_LINHAS]
            f = ini + _MAX_LINHAS - 1
            aviso_corte = True
        numeradas = [f'{i:5d}\t{texto}' for i, texto in enumerate(selecionadas, start=ini)]
        corpo = '\n'.join(numeradas)
        if aviso_corte or f < total:
            corpo += f'\n[mostrando linhas {ini}-{f} de {total}; use inicio/fim para ver mais]'
        return Resultado(True, limpar_saida(corpo, _MAX_CHARS))

    def listar_pasta(self, caminho: str = '.') -> Resultado:
        '''Lista entradas de uma pasta, ocultando pastas ruidosas.'''
        try:
            alvo = self.raiz.resolver(caminho)
        except CaminhoNegado as exc:
            return Resultado(False, str(exc))
        if not alvo.exists():
            return Resultado(False, 'pasta inexistente')
        if not alvo.is_dir():
            return Resultado(False, 'nao e uma pasta')
        try:
            entradas = sorted(alvo.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            return Resultado(False, f'erro ao listar: {exc}')
        visiveis = [p for p in entradas if p.name not in _PASTAS_OCULTAS]
        cortado = len(visiveis) > _MAX_ENTRADAS_LISTA
        visiveis = visiveis[:_MAX_ENTRADAS_LISTA]
        linhas = []
        for p in visiveis:
            nome = p.name + ('/' if p.is_dir() else '')
            linhas.append(nome)
        texto = '\n'.join(linhas)
        if cortado:
            texto += f'\n[mostrando {_MAX_ENTRADAS_LISTA} de {len(entradas)} entradas]'
        return Resultado(True, limpar_saida(texto, _MAX_CHARS))

    def buscar(
        self,
        padrao: str,
        caminho: str = '.',
        glob: str = '*',
    ) -> Resultado:
        '''Busca regex em arquivos que casem com o glob.'''
        try:
            regex = re.compile(padrao)
        except re.error as exc:
            return Resultado(False, f'regex invalida: {exc}')
        try:
            base = self.raiz.resolver(caminho)
        except CaminhoNegado as exc:
            return Resultado(False, str(exc))
        if not base.exists():
            return Resultado(False, 'caminho inexistente')
        arquivos: list[Path] = []
        if base.is_file():
            arquivos.append(base)
        else:
            for p in base.rglob('*'):
                if any(parte in _PASTAS_OCULTAS for parte in p.parts):
                    continue
                if not p.is_file():
                    continue
                if not fnmatch.fnmatch(p.name, glob):
                    continue
                arquivos.append(p)
        achados: list[str] = []
        for arq in arquivos:
            if len(achados) >= _MAX_RESULTADOS_BUSCA:
                break
            if self.raiz.eh_sensivel(arq):
                continue
            try:
                if arq.stat().st_size > _MAX_BYTES_BUSCA:
                    continue
            except OSError:
                continue
            if _eh_binario(arq):
                continue
            try:
                conteudo = arq.read_text(encoding='utf-8', errors='replace')
            except OSError:
                continue
            rel = self.raiz.relativo(arq)
            for num, linha in enumerate(conteudo.splitlines(), start=1):
                if regex.search(linha):
                    trecho = linha.strip()[:_MAX_CHARS_LINHA_BUSCA]
                    achados.append(f'{rel}:{num}: {trecho}')
                    if len(achados) >= _MAX_RESULTADOS_BUSCA:
                        break
        if not achados:
            return Resultado(True, limpar_saida('nenhuma ocorrencia'))
        return Resultado(True, limpar_saida('\n'.join(achados), _MAX_CHARS))
