from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

import httpx

from cli.chat_client import ErroDoServidor, ler_token

if TYPE_CHECKING:
    from cli.chat_sessoes import Sessoes


def servidor_no_ar(url: str, *, get: Callable[..., Any] | None = None) -> bool:
    '''Verifica se o servidor do Hera responde 200 em /healthz.'''
    if get is None:
        def get(u: str, timeout: float) -> Any:
            return httpx.get(u, timeout=timeout)

    try:
        resp = get(f'{url.rstrip("/")}/healthz', timeout=2.0)
    except Exception:
        return False
    return getattr(resp, 'status_code', None) == 200


def _url_local(url: str) -> bool:
    '''True se o host da URL for local.'''
    try:
        host = urlparse(url).hostname
    except Exception:
        return False
    return host in ('127.0.0.1', 'localhost', '::1')


def _iniciar_padrao(comando: list[str], cwd: Path) -> Any:
    '''Inicia o servidor destacado para sobreviver ao fim do chat.'''
    kwargs: dict[str, Any] = {
        'cwd': str(cwd),
        'stdin': subprocess.DEVNULL,
        'stdout': subprocess.DEVNULL,
        'stderr': subprocess.DEVNULL,
    }
    if sys.platform == 'win32':
        kwargs['creationflags'] = (
            subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        kwargs['start_new_session'] = True
    return subprocess.Popen(comando, **kwargs)


def garantir_servidor(
    url: str,
    raiz: Path,
    *,
    iniciar: Callable[[list[str], Path], Any] | None = None,
    no_ar: Callable[[str], bool] | None = None,
    dormir: Callable[[float], None] = time.sleep,
    agora: Callable[[], float] = time.monotonic,
    limite_s: float = 45.0,
    escrever: Callable[[str], None] = print,
) -> bool:
    '''Garante que o servidor esteja no ar, iniciando-o se necessario.'''
    if no_ar is None:
        no_ar = servidor_no_ar
    if iniciar is None:
        iniciar = _iniciar_padrao

    if no_ar(url):
        return True

    if not _url_local(url):
        escrever('O servidor remoto nao esta no ar. Verifique a URL informada.')
        return False

    escrever('Iniciando o servidor do Hera...')
    comando = [sys.executable, 'main.py', 'web', '--no-browser']
    iniciar(comando, raiz)

    inicio = agora()
    while True:
        if no_ar(url):
            return True
        if agora() - inicio >= limite_s:
            escrever(
                f'O servidor nao subiu em {int(limite_s)} s. '
                'Rode: python main.py web --no-browser'
            )
            return False
        dormir(0.5)


async def executar(
    args: Any,
    *,
    raiz: Path | None = None,
    principal: Callable[..., Any] | None = None,
    garantir: Callable[..., bool] | None = None,
    token_de: Callable[..., str] | None = None,
    sessoes_de: Callable[[], Sessoes] | None = None,
) -> int:
    '''Executa o comando chat: garante servidor, le token e roda o REPL.'''
    if raiz is None:
        raiz = Path(__file__).resolve().parent.parent
    if garantir is None:
        garantir = garantir_servidor
    if token_de is None:
        token_de = ler_token
    if principal is None:
        from cli.chat_repl import principal as principal

    if sessoes_de is None:
        def sessoes_padrao() -> Any:
            from app.chat.conversations import ConversationStore
            from cli.chat_sessoes import Sessoes

            return Sessoes(
                ConversationStore(raiz / 'data' / 'conversations.sqlite3'),
                Path.cwd(),
            )

        sessoes_de = sessoes_padrao

    _instancia_sessoes: Any = None

    def obter_sessoes() -> Any:
        nonlocal _instancia_sessoes
        if _instancia_sessoes is None:
            _instancia_sessoes = sessoes_de()
        return _instancia_sessoes

    listar = getattr(args, 'listar', False)
    if listar:
        sessoes = obter_sessoes()
        itens = sessoes.listar()
        if not itens:
            print('Nenhuma conversa salva.')
        else:
            for item in itens:
                marca = '* ' if item.get('desta_pasta') else '  '
                cid = item['id']
                atual = item['atualizada']
                tit = item['titulo']
                msgs = item['mensagens']
                pasta = item['pasta']
                print(f'{marca}{cid} | {atual} | {tit} | {msgs} msgs | {pasta}')
        return 0

    resume = getattr(args, 'resume', None)
    conversa_id: str | None = None
    mensagens_iniciais: list[dict[str, Any]] = []

    if resume is not None:
        sessoes = obter_sessoes()
        conv = sessoes.resolver(resume or None)
        if conv is None:
            print('Nenhuma conversa encontrada para retomar.')
            return 1
        conversa_id = conv['id']
        mensagens_iniciais = sessoes.carregar(conversa_id)
        titulo = conv.get('title') or conv.get('titulo') or ''
        print(f'Retomando: {titulo} ({len(mensagens_iniciais)} mensagens)')

    if not garantir(args.url, raiz):
        return 1

    try:
        token = token_de(raiz)
    except ErroDoServidor as exc:
        print(exc.mensagem)
        return 1

    def ao_turno(usuario: str, assistente: str) -> None:
        nonlocal conversa_id
        s = obter_sessoes()
        if conversa_id is None:
            conversa_id = s.nova()
        s.registrar_turno(conversa_id, usuario, assistente)

    await principal(
        args.url,
        token,
        args.modelo,
        mensagens_iniciais=mensagens_iniciais,
        ao_turno=ao_turno,
    )
    return 0


def registrar(subparsers: Any) -> None:
    '''Registra o subcomando chat.'''
    parser = subparsers.add_parser('chat', help='Inicia o chat com o Hera')
    parser.add_argument('--modelo', default='default', help='Modelo a usar')
    parser.add_argument(
        '--url',
        default='http://127.0.0.1:8000',
        help='URL base do servidor do Hera',
    )
    parser.add_argument(
        '--resume',
        nargs='?',
        const='',
        default=None,
        metavar='ID_OU_TITULO',
        help='Retoma uma conversa (sem valor: a mais recente desta pasta)',
    )
    parser.add_argument(
        '--listar',
        action='store_true',
        help='Lista conversas salvas',
    )
    parser.set_defaults(func=executar)
