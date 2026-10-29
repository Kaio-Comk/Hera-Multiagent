from __future__ import annotations

import json
import os
from pathlib import Path
from typing import AsyncIterator, Iterable, Mapping, Optional

import httpx


class ErroDoServidor(Exception):
    '''Erro retornado pelo servidor do Hera ou falha de conexao.'''

    def __init__(self, status: int, mensagem: str) -> None:
        super().__init__(f'{status}: {mensagem}')
        self.status = status
        self.mensagem = mensagem


def ler_token(raiz: Path, environ: Optional[Mapping[str, str]] = None) -> str:
    '''Le o token do Hera do ambiente ou do arquivo data/.hera_token.'''
    env = os.environ if environ is None else environ
    token = env.get('HERA_API_KEY', '')
    if token and token.strip():
        return token.strip()
    caminho = Path(raiz) / 'data' / '.hera_token'
    if caminho.is_file():
        conteudo = caminho.read_text(encoding='utf-8').strip()
        if conteudo:
            return conteudo
    raise ErroDoServidor(
        0,
        'Token do Hera nao encontrado. Defina HERA_API_KEY ou crie data/.hera_token.',
    )


class ClienteHera:
    '''Cliente assincrono para o endpoint OpenAI-compativel do Hera.'''

    def __init__(
        self,
        base_url: str,
        token: str,
        modelo: str = 'default',
        *,
        transporte: Optional[httpx.AsyncBaseTransport] = None,
        timeout: float = 120.0,
    ) -> None:
        self.base_url = base_url.rstrip('/')
        self.token = token
        self.modelo = modelo
        self._transporte = transporte
        self._timeout = timeout

    def _montar_cliente(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transporte,
            timeout=self._timeout,
        )

    async def conversar(self, mensagens: Iterable[dict]) -> AsyncIterator[str]:
        '''Envia mensagens e produz os pedacos de texto da resposta.'''
        url = f'{self.base_url}/v1/chat/completions'
        cabecalhos = {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
        }
        corpo = {
            'model': self.modelo,
            'messages': list(mensagens),
            'stream': True,
        }
        try:
            async with self._montar_cliente() as cliente:
                async with cliente.stream(
                    'POST', url, headers=cabecalhos, json=corpo
                ) as resposta:
                    if resposta.status_code >= 400:
                        raise ErroDoServidor(
                            resposta.status_code, await self._ler_erro(resposta)
                        )
                    async for linha in resposta.aiter_lines():
                        if not linha.startswith('data:'):
                            continue
                        dado = linha[len('data:'):].strip()
                        if dado == '[DONE]':
                            break
                        if not dado:
                            continue
                        try:
                            evento = json.loads(dado)
                        except json.JSONDecodeError:
                            continue
                        pedaco = self._extrair_conteudo(evento)
                        if pedaco:
                            yield pedaco
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ErroDoServidor(
                0, f'Servidor do Hera fora do ar em {self.base_url}'
            ) from exc

    @staticmethod
    def _extrair_conteudo(evento: object) -> str:
        if not isinstance(evento, dict):
            return ''
        escolhas = evento.get('choices')
        if not isinstance(escolhas, list) or not escolhas:
            return ''
        primeira = escolhas[0]
        if not isinstance(primeira, dict):
            return ''
        delta = primeira.get('delta')
        if not isinstance(delta, dict):
            return ''
        conteudo = delta.get('content')
        if isinstance(conteudo, str):
            return conteudo
        return ''

    @staticmethod
    async def _ler_erro(resposta: httpx.Response) -> str:
        texto = await resposta.aread()
        bruto = texto.decode('utf-8', errors='replace')
        try:
            dados = json.loads(bruto)
        except json.JSONDecodeError:
            return bruto
        if isinstance(dados, dict) and isinstance(dados.get('detail'), str):
            return dados['detail']
        return bruto
