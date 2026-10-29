from __future__ import annotations

import difflib
import hashlib
import os
import re
import shlex
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.agente.caminhos import Raiz, limpar_saida
from app.agente.checkpoints import Checkpoints
from app.agente.leitura import Resultado

_TIPOS = {'editar', 'escrever', 'comando'}
_MODOS = {'read-only', 'guarded', 'workspace', 'auto'}
_MAX_ESCRITA = 1024 * 1024

_OPERADORES = ('&&', '||', '|', '>', '<', ';', '`', '$(', '%')
_BLOQUEADOS = (
    'del /s', 'rd /s', 'rmdir /s', 'rm -rf', 'format', 'shutdown',
    'reg delete', 'diskpart', 'git push', 'git reset --hard', 'git clean',
    'curl', 'wget', 'iwr', 'invoke-webrequest', 'powershell', 'cmd',
)
_GIT_LEITURA = {'status', 'diff', 'log', 'show'}
# opcoes que fazem um git "de leitura" gravar arquivo ou executar programa externo
_GIT_PERIGOSOS = ('--output', '-o', '--ext-diff', '--textconv', '--exec', '--open-files-in-pager', '-c')
_BRANCH_LEITURA = {'-a', '-r', '-v', '-vv', '--list', '-l', '--show-current', '--all', '--remotes', '--verbose'}


def _eh_leitura(norm: tuple[str, ...]) -> bool:
    """So o que NAO grava, NAO apaga e NAO executa codigo do projeto roda sem aprovacao.
    pytest fica de fora de proposito: executa o codigo do repositorio (conftest, plugins)."""
    if norm[:1] == ('git',) and len(norm) >= 2:
        sub, resto = norm[1], norm[2:]
        if sub in _GIT_LEITURA:
            if any(a == p or a.startswith(p + '=') for a in resto for p in _GIT_PERIGOSOS):
                return False
            if sub == 'show' and any(':' in a for a in resto):     # git show rev:caminho le conteudo de arquivo (pode ser segredo)
                return False
            return True
        if sub == 'branch':                                         # 'git branch -D x' apaga; so listar e leitura
            return all(a in _BRANCH_LEITURA for a in resto)
        return False
    return norm[:2] in (('python', '--version'), ('pip', 'list')) or norm[:1] == ('where',)
# Modo auto (Shift+Tab): alem da leitura, roda sem perguntar so o que verifica o projeto sem grava-lo nem toca em producao.
_SEGUROS_AUTO = (
    ('pytest',), ('python', '-m', 'pytest'), ('python', '-m', 'unittest'), ('python', '-m', 'py_compile'),
    ('npm', 'test'), ('npm', 'run', 'test'), ('npm', 'run', 'lint'), ('node', '--check'), ('ruff', 'check'),
)


def _seguro_no_auto(norm: tuple[str, ...]) -> bool:
    return any(norm[: len(pref)] == pref for pref in _SEGUROS_AUTO)


_ENV_SECRETO = re.compile(r'KEY|TOKEN|SECRET|PASSWORD|API', re.IGNORECASE)
_CHAVE = re.compile(r'sk-[A-Za-z0-9]{20,}')


def _sha256(texto: str) -> str:
    return hashlib.sha256(texto.encode('utf-8')).hexdigest()


def _diff(rel: str, antigo: str, novo: str) -> str:
    linhas = difflib.unified_diff(
        antigo.splitlines(keepends=True),
        novo.splitlines(keepends=True),
        fromfile=f'a/{rel}',
        tofile=f'b/{rel}',
    )
    return ''.join(linhas)


def _contar(diff: str) -> tuple[int, int]:
    add = rem = 0
    for linha in diff.splitlines():
        if linha.startswith('+') and not linha.startswith('+++'):
            add += 1
        elif linha.startswith('-') and not linha.startswith('---'):
            rem += 1
    return add, rem


def _env_limpo() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not _ENV_SECRETO.search(k)}


def _executar_padrao(args: tuple[str, ...], cwd: Path, timeout: int) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            list(args),
            cwd=str(cwd),
            timeout=timeout,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace',
            shell=False,
            env=_env_limpo(),
        )
        saida = (proc.stdout or '') + (proc.stderr or '')
        return proc.returncode, saida
    except subprocess.TimeoutExpired:
        return -1, f'timeout apos {timeout}s'
    except FileNotFoundError as exc:
        return -1, f'comando nao encontrado: {exc}'
    except OSError as exc:
        return -1, f'erro ao executar: {exc}'


def classificar_comando(texto: str) -> tuple[str, tuple[str, ...]]:
    """Classifica um comando em bloqueado, leitura ou normal."""
    try:
        args = tuple(shlex.split(texto, posix=False))
    except ValueError:
        return 'bloqueado', ()
    if not args:
        return 'bloqueado', ()
    baixo = texto.lower()
    for op in _OPERADORES:
        if op in baixo:
            return 'bloqueado', args
    for b in _BLOQUEADOS:
        if b in baixo:
            return 'bloqueado', args
    norm = tuple(a.lower() for a in args)
    if _eh_leitura(norm):
        return 'leitura', args
    return 'normal', args


@dataclass(frozen=True)
class Pendencia:
    id: str
    tipo: str
    resumo: str
    diff: str
    alvo: Path | None
    comando: tuple[str, ...] | None
    hash_base: str
    conteudo_novo: str | None


class Escrita:
    """Agente que propoe mudancas; nada e gravado sem aplicar."""

    def __init__(
        self,
        raiz: Raiz,
        checkpoints: Checkpoints,
        modo: str = 'guarded',
        executar: Callable[[tuple[str, ...], Path, int], tuple[int, str]] | None = None,
    ) -> None:
        if modo not in _MODOS:
            raise ValueError(f'modo invalido: {modo}')
        self.raiz = raiz
        self.checkpoints = checkpoints
        self.modo = modo
        self.executar = executar or _executar_padrao
        self._pendentes: dict[str, Pendencia] = {}
        self._lembrados: set[tuple[tuple[str, ...], str]] = set()
        self._seq = 0

    def _novo_id(self) -> str:
        self._seq += 1
        return f'p{self._seq:04d}'

    def _resolver(self, caminho: str | Path) -> Path | None:
        try:
            return self.raiz.resolver(caminho)
        except Exception:
            return None

    def _na_pasta(self, path: Path) -> bool:
        try:
            path.relative_to(self.raiz.pasta)
            return True
        except ValueError:
            return False

    def _dentro(self, path: Path) -> bool:
        return True if self.raiz.acesso_total else self._na_pasta(path)

    def _bloqueio_modo(self) -> Resultado | None:
        if self.modo == 'read-only':
            return Resultado(False, 'modo somente leitura')
        return None

    def propor_edicao(
        self,
        caminho: str | Path,
        antigo: str,
        novo: str,
        todas: bool = False,
    ) -> Resultado:
        """Propoe editar um arquivo existente substituindo trechos."""
        bloq = self._bloqueio_modo()
        if bloq:
            return bloq
        if not antigo:
            return Resultado(False, 'texto antigo vazio')
        path = self._resolver(caminho)
        if path is None or not self._dentro(path):
            return Resultado(False, 'caminho fora da raiz')
        if self.raiz.eh_sensivel(path):
            return Resultado(False, 'arquivo sensivel')
        if not path.is_file():
            return Resultado(False, 'arquivo nao existe')
        try:
            atual = path.read_text(encoding='utf-8')
        except (OSError, UnicodeDecodeError) as exc:
            return Resultado(False, f'falha ao ler: {exc}')
        ocorrencias = atual.count(antigo)
        if ocorrencias == 0:
            return Resultado(False, 'texto antigo nao encontrado')
        if ocorrencias > 1 and not todas:
            return Resultado(False, f'texto antigo aparece {ocorrencias} vezes')
        conteudo_novo = atual.replace(antigo, novo) if todas else atual.replace(antigo, novo, 1)
        rel = self.raiz.relativo(path)
        diff = _diff(rel, atual, conteudo_novo)
        pid = self._novo_id()
        p = Pendencia(
            id=pid,
            tipo='editar',
            resumo=f'editar {rel}',
            diff=diff,
            alvo=path,
            comando=None,
            hash_base=_sha256(atual),
            conteudo_novo=conteudo_novo,
        )
        self._pendentes[pid] = p
        return Resultado(True, f'{pid}\n{diff}')

    def propor_escrita(self, caminho: str | Path, conteudo: str) -> Resultado:
        """Propoe criar ou sobrescrever um arquivo."""
        bloq = self._bloqueio_modo()
        if bloq:
            return bloq
        if len(conteudo.encode('utf-8')) > _MAX_ESCRITA:
            return Resultado(False, 'conteudo excede 1 MB')
        path = self._resolver(caminho)
        if path is None or not self._dentro(path):
            return Resultado(False, 'caminho fora da raiz')
        if self.raiz.eh_sensivel(path):
            return Resultado(False, 'arquivo sensivel')
        if '.git' in path.parts:
            return Resultado(False, 'caminho dentro de .git')
        if path.exists() and not path.is_file():
            return Resultado(False, 'alvo nao e arquivo')
        atual = ''
        if path.is_file():
            try:
                atual = path.read_text(encoding='utf-8')
            except (OSError, UnicodeDecodeError) as exc:
                return Resultado(False, f'falha ao ler: {exc}')
        rel = self.raiz.relativo(path)
        diff = _diff(rel, atual, conteudo)
        pid = self._novo_id()
        p = Pendencia(
            id=pid,
            tipo='escrever',
            resumo=f'escrever {rel}',
            diff=diff,
            alvo=path,
            comando=None,
            hash_base=_sha256(atual),
            conteudo_novo=conteudo,
        )
        self._pendentes[pid] = p
        return Resultado(True, f'{pid}\n{diff}')

    def propor_comando(self, comando: str, timeout: int = 120) -> Resultado:
        """Propoe executar um comando."""
        bloq = self._bloqueio_modo()
        if bloq:
            return bloq
        classe, args = classificar_comando(comando)
        if classe == 'bloqueado':
            return Resultado(False, f'comando bloqueado: {comando}')
        pid = self._novo_id()
        p = Pendencia(
            id=pid,
            tipo='comando',
            resumo=f'comando: {comando}',
            diff='',
            alvo=None,
            comando=args,
            hash_base='',
            conteudo_novo=None,
        )
        self._pendentes[pid] = p
        return Resultado(True, f'{pid}\n{comando}')

    def precisa_aprovacao(self, p: Pendencia) -> bool:
        """Indica se a pendencia exige aprovacao explicita."""
        if p.tipo == 'comando':
            classe, _ = classificar_comando(' '.join(p.comando or ()))
            if classe == 'leitura':
                return False
            if self.modo == 'auto' and classe == 'normal' and _seguro_no_auto(tuple(a.lower() for a in (p.comando or ()))):
                return False
            chave = (p.comando or (), str(self.raiz.pasta))
            return chave not in self._lembrados
        if self.modo in ('workspace', 'auto'):
            # grava sem perguntar so dentro da pasta de trabalho; em qualquer outro lugar do PC, sempre pergunta
            alvo = getattr(p, 'alvo', None)
            return bool(self.raiz.acesso_total and alvo is not None and not self._na_pasta(Path(alvo).resolve()))
        return True

    def lembrar(self, p: Pendencia) -> None:
        """Lembra um comando normal para nao pedir aprovacao de novo."""
        if p.tipo == 'comando' and p.comando:
            self._lembrados.add((p.comando, str(self.raiz.pasta)))

    def _gravar_atomico(self, path: Path, conteudo: str) -> None:
        pasta = path.parent
        fd, tmp = tempfile.mkstemp(dir=str(pasta), prefix='.tmp_escrita_')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as fh:
                fh.write(conteudo)
            os.replace(tmp, path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def aplicar(self, id: str) -> Resultado:
        """Aplica uma pendencia aprovada."""
        p = self._pendentes.get(id)
        if p is None:
            return Resultado(False, 'pendencia nao encontrada')
        if p.tipo in ('editar', 'escrever'):
            assert p.alvo is not None
            atual = ''
            if p.alvo.is_file():
                try:
                    atual = p.alvo.read_text(encoding='utf-8')
                except (OSError, UnicodeDecodeError) as exc:
                    return Resultado(False, f'falha ao ler: {exc}')
            if _sha256(atual) != p.hash_base:
                return Resultado(False, 'o arquivo mudou desde a proposta; peca de novo')
            try:
                cid = self.checkpoints.salvar(p.alvo)     # id do CHECKPOINT (diferente do id da pendencia)
            except Exception as exc:
                return Resultado(False, f'falha no checkpoint: {exc}')
            try:
                self._gravar_atomico(p.alvo, p.conteudo_novo or '')
            except OSError as exc:
                return Resultado(False, f'falha ao gravar: {exc}')
            aviso = ''
            try:
                self.checkpoints.confirmar(cid, p.alvo)
            except Exception as exc:                  # noqa: BLE001 - nao desfaz a edicao, mas avisa que nao da para reverter
                aviso = f' [aviso: checkpoint nao confirmado ({exc}); /rollback pode nao conseguir desfazer]'
            add, rem = _contar(p.diff)
            rel = self.raiz.relativo(p.alvo)
            del self._pendentes[id]
            return Resultado(True, f'aplicado: {rel} (+{add} -{rem}){aviso}')
        assert p.comando is not None
        try:
            codigo, saida = self.executar(p.comando, self.raiz.pasta, 120)
        except Exception as exc:
            del self._pendentes[id]
            return Resultado(False, f'erro ao executar: {exc}')
        del self._pendentes[id]
        texto = limpar_saida(saida, max_chars=8000)
        return Resultado(True, f'codigo {codigo}\n{texto}')

    def rejeitar(self, id: str) -> Resultado:
        """Descarta uma pendencia sem aplicar."""
        if id in self._pendentes:
            del self._pendentes[id]
            return Resultado(True, f'rejeitado: {id}')
        return Resultado(False, 'pendencia nao encontrada')

    def pendentes(self) -> list[Pendencia]:
        """Lista as pendencias ativas."""
        return list(self._pendentes.values())


def redigir(texto: str) -> str:
    """Remove chaves de API aparentes de um texto."""
    return _CHAVE.sub('[REDIGIDO]', texto)
