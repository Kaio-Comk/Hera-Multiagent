from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx

from cli.chat_client import ClienteHera, ErroDoServidor


@dataclass(frozen=True)
class ChamadaFerramenta:
    '''Chamada de ferramenta completa.'''

    id: str
    nome: str
    argumentos: str
    extra: dict | None = None     # extra_content do provedor (Gemini 3: thought_signature); precisa voltar na proxima requisicao

    def como_dict(self) -> dict:
        '''Formato da mensagem do assistente (OpenAI).'''
        d = {
            'id': self.id,
            'type': 'function',
            'function': {'name': self.nome, 'arguments': self.argumentos},
        }
        if self.extra:
            d['extra_content'] = self.extra
        return d

    @property
    def argumentos_json(self) -> dict:
        '''Argumentos decodificados; invalido ou nao-dict => {}.'''
        try:
            dados = json.loads(self.argumentos) if self.argumentos else {}
        except (ValueError, TypeError):
            return {}
        if isinstance(dados, dict):
            return dados
        return {}

    @property
    def argumentos_validos(self) -> bool:
        '''True se argumentos formam um objeto JSON valido.'''
        try:
            dados = json.loads(self.argumentos) if self.argumentos else {}
        except (ValueError, TypeError):
            return False
        return isinstance(dados, dict)


@dataclass(frozen=True)
class Evento:
    '''Evento emitido durante o streaming.'''

    tipo: str
    texto: str = ''
    chamada: ChamadaFerramenta | None = None
    fim: str = ''


def _extrair_detalhe(resposta: httpx.Response) -> str:
    '''Extrai detail do JSON de erro ou devolve texto cru.'''
    try:
        dados = resposta.json()
    except Exception:
        return resposta.text
    if isinstance(dados, dict) and 'detail' in dados:
        return str(dados['detail'])
    return resposta.text


async def conversar_com_ferramentas(
    cliente: ClienteHera,
    mensagens: list[dict],
    ferramentas: list[dict],
    *,
    max_tokens: int | None = None,
) -> AsyncIterator[Evento]:
    '''Faz POST em streaming com function calling e emite Eventos.'''
    payload: dict[str, Any] = {
        'model': cliente.modelo,
        'messages': mensagens,
        'stream': True,
        'tools': ferramentas,
        'tool_choice': 'auto',
    }
    if max_tokens is not None:
        payload['max_tokens'] = max_tokens

    headers = {'Authorization': f'Bearer {cliente.token}'}
    url = f'{cliente.base_url}/v1/chat/completions'

    acumulado: dict[int, dict[str, str]] = {}
    extras: dict[int, dict] = {}
    ordem: list[int] = []
    finish: str = ''

    try:
        async with httpx.AsyncClient(
            transport=cliente._transporte, timeout=cliente._timeout
        ) as http:
            async with http.stream('POST', url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    await resp.aread()
                    raise ErroDoServidor(resp.status_code, _extrair_detalhe(resp))

                async for linha in resp.aiter_lines():
                    if not linha.startswith('data:'):
                        continue
                    dado = linha[len('data:'):].strip()
                    if not dado:
                        continue
                    if dado == '[DONE]':
                        break
                    try:
                        chunk = json.loads(dado)
                    except ValueError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    choices = chunk.get('choices') or []
                    if not choices:
                        continue
                    escolha = choices[0] or {}
                    delta = escolha.get('delta') or {}
                    if not isinstance(delta, dict):
                        delta = {}

                    texto = delta.get('content')
                    if isinstance(texto, str) and texto:
                        yield Evento(tipo='texto', texto=texto)

                    tool_calls = delta.get('tool_calls')
                    if isinstance(tool_calls, list):
                        for tc in tool_calls:
                            if not isinstance(tc, dict):
                                continue
                            idx = tc.get('index')
                            if not isinstance(idx, int):
                                idx = len(ordem)
                            if idx not in acumulado:
                                acumulado[idx] = {'id': '', 'nome': '', 'args': ''}
                                ordem.append(idx)
                            slot = acumulado[idx]
                            extra = tc.get('extra_content')
                            if isinstance(extra, dict) and extra:
                                extras[idx] = extra
                            cid = tc.get('id')
                            if isinstance(cid, str) and cid:
                                slot['id'] = cid
                            fn = tc.get('function')
                            if isinstance(fn, dict):
                                nome = fn.get('name')
                                if isinstance(nome, str) and nome:
                                    slot['nome'] = nome
                                args = fn.get('arguments')
                                if isinstance(args, str):
                                    slot['args'] += args

                    fr = escolha.get('finish_reason')
                    if isinstance(fr, str) and fr:
                        finish = fr
    except (httpx.ConnectError, httpx.ConnectTimeout):
        raise ErroDoServidor(0, f'Servidor do Hera fora do ar em {cliente.base_url}')

    chamadas: list[ChamadaFerramenta] = []
    for idx in sorted(acumulado):
        slot = acumulado[idx]
        if not slot['nome']:
            continue
        cid = slot['id'] or f'call_{idx}'
        chamadas.append(
            ChamadaFerramenta(id=cid, nome=slot['nome'], argumentos=slot['args'], extra=extras.get(idx))
        )

    for chamada in chamadas:
        yield Evento(tipo='chamada', chamada=chamada)

    if not finish:
        finish = 'tool_calls' if chamadas else 'stop'
    yield Evento(tipo='fim', fim=finish)


def _normalizar_argumentos(valor: Any) -> str | None:
    '''Converte argumentos em string JSON; None se invalido.'''
    if isinstance(valor, str):
        return valor
    if isinstance(valor, dict):
        try:
            return json.dumps(valor)
        except (ValueError, TypeError):
            return None
    return None


def _chamada_de_objeto(obj: Any) -> ChamadaFerramenta | None:
    '''Tenta extrair uma ChamadaFerramenta de um dict solto.'''
    if not isinstance(obj, dict):
        return None
    nome: Any = None
    for chave in ('name', 'tool', 'function'):
        valor = obj.get(chave)
        if isinstance(valor, str) and valor:
            nome = valor
            break
    if not nome:
        return None
    args: Any = None
    for chave in ('arguments', 'args', 'parameters'):
        if chave in obj:
            args = obj[chave]
            break
    argumentos = _normalizar_argumentos(args) if args is not None else '{}'
    if argumentos is None:
        return None
    cid = obj.get('id')
    if not isinstance(cid, str) or not cid:
        cid = 'call_0'
    return ChamadaFerramenta(id=cid, nome=nome, argumentos=argumentos)


def detectar_chamada_em_texto(texto: str) -> ChamadaFerramenta | None:
    '''Detecta chamada de ferramenta escrita como texto. Nunca levanta.'''
    if not isinstance(texto, str) or not texto:
        return None
    try:
        for cerca in ('```json', '```tool'):
            inicio = texto.find(cerca)
            if inicio == -1:
                continue
            resto = texto[inicio + len(cerca):]
            fim = resto.find('```')
            if fim == -1:
                continue
            bloco = resto[:fim].strip()
            try:
                obj = json.loads(bloco)
            except ValueError:
                continue
            chamada = _chamada_de_objeto(obj)
            if chamada is not None:
                return chamada

        for i, ch in enumerate(texto):
            if ch != '{':
                continue
            profundidade = 0
            dentro_str = False
            escape = False
            for j in range(i, len(texto)):
                c = texto[j]
                if escape:
                    escape = False
                    continue
                if c == '\\':
                    escape = True
                    continue
                if c == '"':
                    dentro_str = not dentro_str
                    continue
                if dentro_str:
                    continue
                if c == '{':
                    profundidade += 1
                elif c == '}':
                    profundidade -= 1
                    if profundidade == 0:
                        candidato = texto[i:j + 1]
                        try:
                            obj = json.loads(candidato)
                        except ValueError:
                            break
                        chamada = _chamada_de_objeto(obj)
                        if chamada is not None:
                            return chamada
                        break
    except Exception:
        return None
    return None


def mensagem_assistente(texto: str, chamadas: list[ChamadaFerramenta]) -> dict:
    '''Monta a mensagem do assistente com eventuais tool_calls.'''
    return {
        'role': 'assistant',
        'content': texto or None,
        'tool_calls': [c.como_dict() for c in chamadas],
    }


def mensagem_resultado(chamada: ChamadaFerramenta, resultado: str) -> dict:
    '''Monta a mensagem de resultado de ferramenta.'''
    return {
        'role': 'tool',
        'tool_call_id': chamada.id,
        'content': resultado,
    }
