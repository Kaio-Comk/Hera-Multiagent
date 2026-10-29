from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import urlparse

import httpx

from cli.chat_client import ClienteHera, ErroDoServidor, ler_token
from cli.chat_comandos import ConfigAgente, EstadoChat

try:
    from app.agente.caminhos import Raiz
    from app.agente.checkpoints import Checkpoints
    from app.agente.escrita import Escrita
    from app.agente.leitura import Leitura
except ImportError:
    try:
        from agente.caminhos import Raiz  # type: ignore[no-redef]
        from agente.checkpoints import Checkpoints  # type: ignore[no-redef]
        from agente.escrita import Escrita  # type: ignore[no-redef]
        from agente.leitura import Leitura  # type: ignore[no-redef]
    except ImportError:
        Raiz = Any  # type: ignore[misc,assignment]
        Checkpoints = Any  # type: ignore[misc,assignment]
        Escrita = Any  # type: ignore[misc,assignment]
        Leitura = Any  # type: ignore[misc,assignment]

try:
    from app.skills import catalogo_para_prompt, descobrir_skills
except ImportError:
    try:
        from skills import catalogo_para_prompt, descobrir_skills  # type: ignore[no-redef]
    except ImportError:
        def catalogo_para_prompt(*a: Any, **k: Any) -> str:  # type: ignore[misc]
            return ''
        def descobrir_skills(*a: Any, **k: Any) -> list[Any]:  # type: ignore[misc]
            return []

try:
    from cli.skills_cmd import pastas_de_skills
except ImportError:
    try:
        from skills_cmd import pastas_de_skills  # type: ignore[no-redef]
    except ImportError:
        def pastas_de_skills(*a: Any, **k: Any) -> list[Path]:  # type: ignore[misc]
            return []

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
    comando = [sys.executable, '-m', 'main', 'web', '--no-browser']      # como modulo: funciona instalado e no checkout
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
    estado: EstadoChat | None = None,
    agente_cfg: ConfigAgente | None = None,
    skills_de: Callable[[], list[Any]] | None = None,
    maestro_de: Callable[..., Any] | None = None,
) -> int:
    '''Executa o comando chat: garante servidor, le token e roda o REPL.'''
    if raiz is None:
        from app.paths import home_hera
        raiz = home_hera()       # checkout => pasta do projeto; instalado => ~/.hera
    if garantir is None:
        garantir = garantir_servidor
    if token_de is None:
        token_de = ler_token
    if principal is None:
        from cli.chat_repl import principal as principal

    if skills_de is None:
        def skills_padrao() -> list[Any]:
            try:
                return descobrir_skills(pastas_de_skills())
            except Exception:
                return []

        skills_de = skills_padrao

    skills = skills_de()

    if sessoes_de is None:
        def sessoes_padrao() -> Any:
            from app.chat.conversations import ConversationStore
            from app.paths import data_dir
            from cli.chat_sessoes import Sessoes

            return Sessoes(
                ConversationStore(data_dir() / 'conversations.sqlite3'),
                Path.cwd(),
            )

        sessoes_de = sessoes_padrao

    if estado is None:
        estado = EstadoChat(sessoes_de=sessoes_de, skills=skills)
    elif estado.sessoes_de is None:
        estado.sessoes_de = sessoes_de
    if getattr(estado, 'skills', None) is None:
        estado.skills = skills

    listar = getattr(args, 'listar', False)
    if listar:
        sessoes = estado.sessoes()
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
    mensagens_iniciais: list[dict[str, Any]] = []

    if resume is not None:
        sessoes = estado.sessoes()
        conv = sessoes.resolver(resume or None)
        if conv is None:
            print('Nenhuma conversa encontrada para retomar.')
            return 1
        estado.conversa_id = conv['id']
        mensagens_iniciais = sessoes.carregar(estado.conversa_id)
        titulo = conv.get('title') or conv.get('titulo') or ''
        estado.retomando = f'Retomando: {titulo} ({len(mensagens_iniciais)} mensagens)'
        if not sys.stdout.isatty():  # no terminal a tela e limpa ao abrir; la a abertura mostra esta linha
            print(estado.retomando)

    if not garantir(args.url, raiz):
        return 1

    try:
        token = token_de(raiz)
    except ErroDoServidor as exc:
        print(exc.mensagem)
        return 1

    cfg = agente_cfg
    if cfg is None and getattr(args, 'agente', False):
        modo = getattr(args, 'modo', 'guarded')
        raiz_agente = Raiz(Path.cwd())
        sha = hashlib.sha1(str(Path.cwd()).encode('utf-8')).hexdigest()[:12]
        pasta_checkpoints = Path.home() / '.hera' / 'checkpoints' / sha
        checkpoints = Checkpoints(pasta_checkpoints, raiz_agente)
        leitura = Leitura(raiz_agente)
        escrita = Escrita(raiz_agente, checkpoints, modo)
        cfg = ConfigAgente(
            raiz=raiz_agente,
            checkpoints=checkpoints,
            leitura=leitura,
            escrita=escrita,
            modo=modo,
            skills=skills,
        )
    elif cfg is not None and getattr(cfg, 'skills', None) is None:
        cfg.skills = skills

    catalogo = ''
    try:
        catalogo = catalogo_para_prompt(skills)
    except Exception:
        catalogo = ''

    sistema = ''
    try:
        from app.instrucoes import descobrir_instrucoes, montar_prompt_sistema

        instrucoes = descobrir_instrucoes(Path.cwd())
        sistema = montar_prompt_sistema(
            instrucoes,
            pasta=Path.cwd(),
            memoria='',
            catalogo_skills=catalogo,
        )
        estado.instrucoes = instrucoes
        total = len(instrucoes.fontes)
        for av in instrucoes.avisos:
            print(f'Aviso: {av}')
    except Exception as exc:
        print(f'Aviso: nao foi possivel carregar instrucoes ({exc})')

    if cfg is not None:
        paragrafo = (
            'Use as ferramentas para ler antes de editar, faça edições pequenas, '
            'rode os testes do projeto com executar_comando e nunca peça ao usuário para colar o que você pode ler. '
            'Para varrer ou analisar muito código, use delegar_tarefa (faixa leve) e junte os resumos; edite você mesmo.'
        )
        sistema = f'{sistema}\n\n{paragrafo}'.strip()

    def ao_turno(usuario: str, assistente: str) -> None:
        s = estado.sessoes()
        if estado.conversa_id is None:
            estado.conversa_id = s.nova()
        s.registrar_turno(estado.conversa_id, usuario, assistente)

    modelo_arg = getattr(args, 'modelo', None)
    sem_maestro = getattr(args, 'sem_maestro', False)
    perfil = getattr(args, 'perfil', None)
    janela = getattr(args, 'janela', 32000)

    cliente = ClienteHera(args.url, token, modelo_arg or 'default')

    if maestro_de is not None:
        maestro = maestro_de(cliente)
    else:
        try:
            from app.maestro import Maestro

            maestro = Maestro(
                disponiveis_de=cliente.modelos,
                ativo=not sem_maestro,
                forcado=None if sem_maestro else modelo_arg,
                perfil=perfil,
            )
        except Exception:
            maestro = None

    await principal(
        args.url,
        token,
        modelo_arg or 'default',
        mensagens_iniciais=mensagens_iniciais,
        ao_turno=ao_turno,
        estado=estado,
        sistema=sistema,
        agente_cfg=cfg,
        skills=skills,
        maestro=maestro,
        janela=janela,
        cliente=cliente,
    )
    return 0


def registrar(subparsers: Any) -> None:
    '''Registra o subcomando chat.'''
    parser = subparsers.add_parser('chat', help='Inicia o chat com o Hera')
    parser.add_argument('--modelo', default=None, help='Modelo a usar')
    parser.add_argument(
        '--sem-maestro',
        action='store_true',
        help='Desativa o maestro (usa modelo default)',
    )
    parser.add_argument(
        '--perfil',
        choices=['economico', 'qualidade'],
        default=None,
        help='Perfil de modelos do maestro (economico ou qualidade)',
    )
    parser.add_argument(
        '--janela',
        type=int,
        default=32000,
        help='Tamanho da janela de contexto em tokens',
    )
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
    parser.add_argument(
        '--agente',
        action='store_true',
        help='Ativa o agente de codigo',
    )
    parser.add_argument(
        '--modo',
        choices=['read-only', 'guarded', 'workspace'],
        default='guarded',
        help='Modo do agente de codigo (read-only, guarded ou workspace)',
    )
    parser.set_defaults(func=executar)
