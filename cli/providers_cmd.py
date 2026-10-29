from __future__ import annotations

import argparse
import difflib
import getpass
import json
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

from cli.chat_client import ErroDoServidor, ler_token

URL_PADRAO = 'http://127.0.0.1:8000'


def _mensagem_erro(detail: Any) -> str:
    if isinstance(detail, dict):
        msg = detail.get('message') or detail.get('error') or ''
        return str(msg)
    if detail is None:
        return ''
    return str(detail)


def _chamar_padrao(raiz: Path | None, url: str) -> Callable[..., dict]:
    base = (url or URL_PADRAO).rstrip('/')

    def chamar(metodo: str, caminho: str, corpo: dict | None = None) -> dict:
        token = ler_token(raiz) if raiz is not None else None
        headers = {'X-Hera-Token': token} if token else {}
        try:
            with httpx.Client(timeout=60.0) as cliente:
                resposta = cliente.request(
                    metodo,
                    base + caminho,
                    json=corpo,
                    headers=headers,
                )
        except httpx.ConnectError:
            raise ErroDoServidor(
                0,
                'Servidor do Hera fora do ar. Rode: python main.py web --no-browser',
            )
        if resposta.status_code >= 400:
            try:
                dados = resposta.json()
                detail = dados.get('detail') if isinstance(dados, dict) else None
            except Exception:
                detail = None
            raise ErroDoServidor(resposta.status_code, _mensagem_erro(detail))
        if not resposta.content:
            return {}
        try:
            return resposta.json()
        except Exception:
            return {}

    return chamar


def _confirmar_padrao(pergunta: str) -> bool:
    try:
        resposta = input(pergunta)
    except EOFError:
        return False
    return resposta.strip().lower() in ('s', 'sim')


def _pedir_segredo_padrao(pergunta: str) -> str:
    try:
        return getpass.getpass(pergunta)
    except EOFError:
        return ''


def _parse_param(texto: str) -> tuple[str, str]:
    if '=' not in texto:
        raise ValueError(f'Parametro invalido: {texto!r}. Use K=V.')
    chave, valor = texto.split('=', 1)
    chave = chave.strip()
    if not chave:
        raise ValueError(f'Parametro invalido: {texto!r}. Use K=V.')
    return chave, valor


def _parecidos(preset: str, ids: Iterable[str], limite: int = 5) -> list[str]:
    return difflib.get_close_matches(preset, list(ids), n=limite, cutoff=0.3)


def _fmt_hoje(hoje: Any) -> str:
    if hoje is None:
        return '-'
    if isinstance(hoje, dict):
        req = hoje.get('requests')
        tok = hoje.get('tokens')
        partes = []
        if req is not None:
            partes.append(f'{req} req')
        if tok is not None:
            partes.append(f'{tok} tok')
        return ', '.join(partes) if partes else '-'
    return str(hoje)


def _acao_list(args: argparse.Namespace, chamar: Callable[..., dict], saida: Callable[..., None]) -> int:
    estado = chamar('GET', '/api/router/state')
    if args.catalogo:
        presets = estado.get('presets') or []
        if args.gratis:
            presets = [p for p in presets if p.get('tier') == 'cloud_free']
        if not presets:
            saida('Nenhum preset encontrado.')
            return 0
        for p in presets:
            marca = ' [gratis]' if p.get('tier') == 'cloud_free' else ''
            saida(
                f"{p.get('id', '')} | {p.get('tier', '')} | "
                f"{p.get('label', '')}{marca} | {p.get('default_model', '')}"
            )
        return 0
    providers = estado.get('providers') or []
    if args.gratis:
        providers = [p for p in providers if p.get('tier') == 'cloud_free']
    if not providers:
        saida('Nenhum provider configurado.')
        return 0
    for p in providers:
        ligado = 'sim' if p.get('enabled') else 'nao'
        chaves = p.get('keys_found')
        if chaves is None:
            chaves = 0
        saida(
            f"{p.get('name', '')} | {p.get('tier', '')} | {ligado} | "
            f"{chaves} | {p.get('default_model', '')} | {_fmt_hoje(p.get('today'))}"
        )
    _rodape_orcamento(estado, saida)
    return 0


def _rodape_orcamento(estado: dict, saida: Callable[..., None]) -> None:
    """Teto de gasto diario, gasto de hoje e aderencia ao modelo de fallback (servidor antigo sem os campos: nada)."""
    if 'daily_budget_usd' not in estado and 'today_cost_usd' not in estado:
        return
    teto = estado.get('daily_budget_usd')
    if teto is None:
        texto_teto = 'sem teto'
    elif float(teto) == 0:
        texto_teto = 'US$ 0.00 (so gratuitos e locais)'
    else:
        texto_teto = f'US$ {float(teto):.2f}'
    gasto = float(estado.get('today_cost_usd') or 0)
    sticky = int(estado.get('sticky_minutes') or 0)
    aderencia = f'{sticky} min' if sticky > 0 else 'desligada'
    saida(f'Teto diario: {texto_teto} | gasto hoje: US$ {gasto:.4f} | aderencia: {aderencia}')


def _acao_add(
    args: argparse.Namespace,
    chamar: Callable[..., dict],
    pedir_segredo: Callable[[str], str],
    saida: Callable[..., None],
    perguntar: Callable[[str], str] = input,
) -> int:
    estado = chamar('GET', '/api/router/state')
    presets = estado.get('presets') or []
    por_id = {p.get('id'): p for p in presets}
    preset = por_id.get(args.preset)
    if preset is None:
        ids = [p.get('id') for p in presets if p.get('id')]
        sugestoes = _parecidos(args.preset, ids)
        if sugestoes:
            saida(f"Preset '{args.preset}' nao encontrado. Parecidos: {', '.join(sugestoes)}")
        else:
            saida(f"Preset '{args.preset}' nao encontrado.")
        return 1

    params: dict[str, Any] = {}
    for item in args.param or []:
        try:
            k, v = _parse_param(item)
        except ValueError as exc:
            saida(str(exc))
            return 1
        params[k] = v

    campos_params = preset.get('params') or []
    for campo in campos_params:
        chave = campo.get('key')
        if not chave or chave in params:
            continue
        rotulo = campo.get('label') or chave
        try:
            valor = perguntar(f'{rotulo}: ')
        except EOFError:
            valor = ''
        params[chave] = valor

    corpo: dict[str, Any] = {
        'preset': args.preset,
        'enabled': not args.desligado,
    }
    if args.nome:
        corpo['name'] = args.nome
    if args.limite_diario is not None:
        corpo['daily_request_limit'] = args.limite_diario
    if params:
        corpo['params'] = params

    resultado = chamar('POST', '/api/router/providers', corpo)
    nome = resultado.get('name') or args.nome or args.preset

    chave = args.chave
    if chave:
        saida('Aviso: a chave passada em --chave fica no historico do shell.')
    elif preset.get('key_env'):
        try:
            chave = pedir_segredo(f"Chave para {nome} (vazio para pular): ")
        except Exception:
            chave = ''
        if not chave:
            saida('Nenhuma chave informada; provider criado sem chave.')
    if chave:
        chamar('POST', f'/api/router/providers/{nome}/keys', {'keys': [chave], 'mode': 'replace'})

    try:
        teste = chamar('POST', '/api/router/test', {'provider': nome})
        latencia = teste.get('latency_ms')
        if latencia is None:
            saida('OK')
        else:
            saida(f'OK em {latencia} ms')
    except ErroDoServidor as exc:
        saida(f'Erro ({exc.status}): {exc.mensagem}')

    note = preset.get('note')
    if note:
        saida(note)
    return 0


def _acao_key(
    args: argparse.Namespace,
    chamar: Callable[..., dict],
    pedir_segredo: Callable[[str], str],
    saida: Callable[..., None],
) -> int:
    chave = args.chave
    if chave:
        saida('Aviso: a chave passada em --chave fica no historico do shell.')
    else:
        try:
            chave = pedir_segredo(f"Chave para {args.nome}: ")
        except Exception:
            chave = ''
    if not chave:
        saida('Nenhuma chave informada.')
        return 1
    modo = 'append' if args.acrescentar else 'replace'
    chamar('POST', f'/api/router/providers/{args.nome}/keys', {'keys': [chave], 'mode': modo})
    saida('Chave atualizada.')
    return 0


def _acao_test(args: argparse.Namespace, chamar: Callable[..., dict], saida: Callable[..., None]) -> int:
    corpo: dict[str, Any] = {'provider': args.nome}
    if args.modelo:
        corpo['model'] = args.modelo
    resultado = chamar('POST', '/api/router/test', corpo)
    latencia = resultado.get('latency_ms')
    if latencia is None:
        saida('OK')
    else:
        saida(f'OK em {latencia} ms')
    return 0


def _acao_enabled(
    args: argparse.Namespace,
    chamar: Callable[..., dict],
    saida: Callable[..., None],
    valor: bool,
) -> int:
    chamar('POST', f'/api/router/providers/{args.nome}/enabled', {'enabled': valor})
    saida('Provider ligado.' if valor else 'Provider desligado.')
    return 0


def _acao_remove(
    args: argparse.Namespace,
    chamar: Callable[..., dict],
    confirmar: Callable[[str], bool],
    saida: Callable[..., None],
) -> int:
    if not args.sim:
        if not confirmar(f"Remover provider '{args.nome}'? [s/N] "):
            saida('Cancelado.')
            return 0
    chamar('DELETE', f'/api/router/providers/{args.nome}')
    saida('Provider removido.')
    return 0


def _acao_models(args: argparse.Namespace, chamar: Callable[..., dict], saida: Callable[..., None]) -> int:
    resultado = chamar('POST', f'/api/router/providers/{args.nome}/discover')
    modelos = resultado.get('models')
    if modelos is None:
        contagem = resultado.get('count')
        if contagem is None:
            saida('Nenhum modelo encontrado.')
        else:
            saida(f'{contagem} modelos encontrados.')
        return 0
    if not modelos:
        saida('Nenhum modelo encontrado.')
        return 0
    for m in modelos:
        saida(str(m))
    return 0


def _acao_limits(args: argparse.Namespace, chamar: Callable[..., dict], saida: Callable[..., None]) -> int:
    corpo: dict[str, Any] = {}
    if args.prioridade is not None:
        corpo['priority'] = args.prioridade
    if args.rpm is not None:
        corpo['rpm'] = args.rpm
    if args.diario_req is not None:
        corpo['daily_requests'] = args.diario_req
    if args.diario_tokens is not None:
        corpo['daily_tokens'] = args.diario_tokens
    if not corpo:
        saida('Nenhum limite informado.')
        return 1
    chamar('POST', f'/api/router/providers/{args.nome}/limits', corpo)
    saida('Limites atualizados.')
    return 0


def executar(
    args: argparse.Namespace,
    *,
    raiz: Path | None = None,
    chamar: Callable[..., dict] | None = None,
    pedir_segredo: Callable[[str], str] | None = None,
    confirmar: Callable[[str], bool] | None = None,
    saida: Callable[..., None] = print,
    perguntar: Callable[[str], str] = input,
) -> int:
    if raiz is None:
        from app.paths import home_hera
        raiz = home_hera()      # checkout => pasta do projeto (data/.hera_token); instalado => ~/.hera
    if chamar is None:
        chamar = _chamar_padrao(raiz, getattr(args, 'url', URL_PADRAO))
    if pedir_segredo is None:
        pedir_segredo = _pedir_segredo_padrao
    if confirmar is None:
        confirmar = _confirmar_padrao

    try:
        acao = args.acao
        if acao == 'list':
            return _acao_list(args, chamar, saida)
        if acao == 'add':
            return _acao_add(args, chamar, pedir_segredo, saida, perguntar)
        if acao == 'key':
            return _acao_key(args, chamar, pedir_segredo, saida)
        if acao == 'test':
            return _acao_test(args, chamar, saida)
        if acao == 'enable':
            return _acao_enabled(args, chamar, saida, True)
        if acao == 'disable':
            return _acao_enabled(args, chamar, saida, False)
        if acao == 'remove':
            return _acao_remove(args, chamar, confirmar, saida)
        if acao == 'models':
            return _acao_models(args, chamar, saida)
        if acao == 'limits':
            return _acao_limits(args, chamar, saida)
        saida(f'Acao desconhecida: {acao}')
        return 1
    except ErroDoServidor as exc:
        saida(f'Erro ({exc.status}): {exc.mensagem}')
        return 1


def registrar(subparsers: Any) -> None:
    parser = subparsers.add_parser('providers', help='Gerencia providers do roteador.')
    parser.add_argument('--url', default=URL_PADRAO, help='URL base do servidor Hera.')
    sub = parser.add_subparsers(dest='acao', required=True)

    p_list = sub.add_parser('list', help='Lista providers ou catalogo.')
    p_list.add_argument('--catalogo', action='store_true', help='Lista presets do catalogo.')
    p_list.add_argument('--gratis', action='store_true', help='Filtra tier cloud_free.')

    p_add = sub.add_parser('add', help='Adiciona provider a partir de um preset.')
    p_add.add_argument('preset')
    p_add.add_argument('--nome')
    p_add.add_argument('--chave')
    p_add.add_argument('--param', action='append', default=[])
    p_add.add_argument('--limite-diario', type=int)
    p_add.add_argument('--desligado', action='store_true')

    p_key = sub.add_parser('key', help='Define chaves de um provider.')
    p_key.add_argument('nome')
    p_key.add_argument('--chave')
    p_key.add_argument('--acrescentar', action='store_true')

    p_test = sub.add_parser('test', help='Testa um provider.')
    p_test.add_argument('nome')
    p_test.add_argument('--modelo')

    p_enable = sub.add_parser('enable', help='Liga um provider.')
    p_enable.add_argument('nome')

    p_disable = sub.add_parser('disable', help='Desliga um provider.')
    p_disable.add_argument('nome')

    p_remove = sub.add_parser('remove', help='Remove um provider.')
    p_remove.add_argument('nome')
    p_remove.add_argument('--sim', action='store_true')

    p_models = sub.add_parser('models', help='Descobre modelos de um provider.')
    p_models.add_argument('nome')

    p_limits = sub.add_parser('limits', help='Atualiza limites de um provider.')
    p_limits.add_argument('nome')
    p_limits.add_argument('--prioridade', type=int)
    p_limits.add_argument('--rpm', type=int)
    p_limits.add_argument('--diario-req', type=int)
    p_limits.add_argument('--diario-tokens', type=int)
