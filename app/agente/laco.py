from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable, Sequence

from cli.chat_ferramentas import (
    ChamadaFerramenta,
    Evento,
    detectar_chamada_em_texto,
    mensagem_assistente,
    mensagem_resultado,
)

from .escrita import Escrita, Pendencia
from .leitura import Leitura

try:
    from app.skills import Skill, ler_skill, resolver_skill
except ImportError:
    try:
        from skills import Skill, ler_skill, resolver_skill  # type: ignore[no-redef]
    except ImportError:
        Skill = Any  # type: ignore[misc,assignment]
        def ler_skill(s: Any) -> str:  # type: ignore[misc]
            return ''
        def resolver_skill(nome: str, s: Any) -> Any:  # type: ignore[misc]
            return None

Aprovar = Callable[[Pendencia], Awaitable[str]]
Escrever = Callable[[str], None]
Conversar = Callable[[Sequence[dict[str, Any]], Sequence[dict[str, Any]]], AsyncIterator[Evento]]
Delegar = Callable[[str, str], Awaitable[str]]

FERRAMENTAS: list[dict[str, Any]] = [
    {
        'type': 'function',
        'function': {
            'name': 'ler_arquivo',
            'description': 'Le o conteudo de um arquivo do projeto.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'caminho': {'type': 'string', 'description': 'Caminho do arquivo.'},
                    'inicio': {'type': 'integer', 'description': 'Linha inicial (1-based).'},
                    'fim': {'type': 'integer', 'description': 'Linha final (inclusive).'},
                },
                'required': ['caminho'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'listar_pasta',
            'description': 'Lista arquivos e pastas de um diretorio.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'caminho': {'type': 'string', 'description': 'Diretorio a listar.'},
                },
                'required': [],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'buscar',
            'description': 'Busca um padrao em arquivos do projeto.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'padrao': {'type': 'string', 'description': 'Padrao a buscar.'},
                    'caminho': {'type': 'string', 'description': 'Diretorio base.'},
                    'glob': {'type': 'string', 'description': 'Filtro glob de arquivos.'},
                },
                'required': ['padrao'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'editar_arquivo',
            'description': 'Propoe trocar um trecho de um arquivo.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'caminho': {'type': 'string', 'description': 'Caminho do arquivo.'},
                    'antigo': {'type': 'string', 'description': 'Trecho atual.'},
                    'novo': {'type': 'string', 'description': 'Trecho novo.'},
                    'todas': {'type': 'boolean', 'description': 'Trocar todas as ocorrencias.'},
                },
                'required': ['caminho', 'antigo', 'novo'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'escrever_arquivo',
            'description': 'Propoe escrever o conteudo completo de um arquivo.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'caminho': {'type': 'string', 'description': 'Caminho do arquivo.'},
                    'conteudo': {'type': 'string', 'description': 'Conteudo novo.'},
                },
                'required': ['caminho', 'conteudo'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'executar_comando',
            'description': 'Propoe executar um comando no workspace.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'comando': {'type': 'string', 'description': 'Comando a executar.'},
                    'timeout': {'type': 'integer', 'description': 'Timeout em segundos.'},
                },
                'required': ['comando'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'usar_skill',
            'description': 'Carrega as instrucoes completas de uma skill instalada.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'nome': {'type': 'string', 'description': 'Nome da skill.'},
                },
                'required': ['nome'],
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'delegar_tarefa',
            'description': 'Ajudante de contexto limpo que le, busca e analisa o projeto e devolve um resumo; nao edita nem executa comandos.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'tarefa': {
                        'type': 'string',
                        'description': 'Tarefa a ser executada pelo sub-agente.',
                    },
                    'faixa': {
                        'type': 'string',
                        'enum': ['leve', 'medio', 'pesado'],
                        'description': 'Faixa de modelo a usar (leve, medio ou pesado). Padrao: leve.',
                    },
                },
                'required': ['tarefa'],
            },
        },
    },
]

_NOMES_VALIDOS = [f['function']['name'] for f in FERRAMENTAS]


class Agente:
    def __init__(
        self,
        conversar: Conversar,
        leitura: Leitura,
        escrita: Escrita,
        *,
        aprovar: Aprovar,
        escrever: Escrever,
        max_passos: int | None = None,
        max_chamadas_por_passo: int = 8,
        skills: Any = None,
        delegar: Delegar | None = None,
    ) -> None:
        self.conversar = conversar
        self.leitura = leitura
        self.escrita = escrita
        self.aprovar = aprovar
        self.escrever = escrever
        if max_passos is None:  # investigar um bug passa de 15 chamadas ao modelo com folga: padrao 40, ajustavel
            try:
                max_passos = max(1, int(os.environ.get('HERA_MAX_PASSOS', '40')))
            except ValueError:
                max_passos = 40
        self.max_passos = max_passos
        self.max_chamadas_por_passo = max_chamadas_por_passo
        self.skills = skills
        self.delegar = delegar
        self._semaforo_delegar = asyncio.Semaphore(3)

    async def turno(
        self,
        historico: list[dict[str, Any]],
        sistema: str | Sequence[str] = '',
    ) -> str:
        msgs: list[dict[str, Any]] = []
        if isinstance(sistema, (list, tuple)):
            for s in sistema:
                if s:
                    msgs.append({'role': 'system', 'content': s})
        elif sistema:
            msgs.append({'role': 'system', 'content': sistema})
        msgs.extend(historico)

        texto_turno = ''
        houve_ferramenta = False
        tentou_texto = False

        for _ in range(self.max_passos):
            texto_passo = ''
            chamadas: list[ChamadaFerramenta] = []

            async for ev in self.conversar(msgs, FERRAMENTAS):
                if ev.tipo == 'texto' and ev.texto:
                    texto_passo += ev.texto
                    self.escrever(ev.texto)
                elif ev.tipo == 'chamada' and ev.chamada is not None:
                    chamadas.append(ev.chamada)

            texto_turno += texto_passo

            if not chamadas:
                if not tentou_texto:
                    tentou_texto = True
                    lida = detectar_chamada_em_texto(texto_passo)
                    if lida is not None and lida.nome in _NOMES_VALIDOS:       # exemplo JSON numa resposta nao vira execucao
                        self.escrever('(chamada de ferramenta lida do texto)')
                        chamadas = [lida]

            if not chamadas:
                if not texto_turno and houve_ferramenta:
                    final_msgs = list(msgs) + [
                        {'role': 'user', 'content': 'Resuma o que foi feito e o resultado.'}
                    ]
                    async for ev in self.conversar(final_msgs, []):
                        if ev.tipo == 'texto' and ev.texto:
                            texto_turno += ev.texto
                            self.escrever(ev.texto)
                return texto_turno

            houve_ferramenta = True
            msgs.append(mensagem_assistente(texto_passo, chamadas))

            chamadas_validas = chamadas[: self.max_chamadas_por_passo]
            indices_delegar = [
                i for i, ch in enumerate(chamadas_validas) if ch.nome == 'delegar_tarefa'
            ]
            indices_outras = [
                i for i, ch in enumerate(chamadas_validas) if ch.nome != 'delegar_tarefa'
            ]

            resultados: dict[int, str] = {}

            if indices_delegar:
                tarefas_delegar = [
                    asyncio.create_task(self._executar_delegar(chamadas_validas[i]))
                    for i in indices_delegar
                ]
                try:
                    res_delegar = await asyncio.gather(*tarefas_delegar)
                except asyncio.CancelledError:
                    for t in tarefas_delegar:
                        if not t.done():
                            t.cancel()
                    await asyncio.gather(*tarefas_delegar, return_exceptions=True)
                    raise
                for idx, r in zip(indices_delegar, res_delegar):
                    resultados[idx] = r

            for idx in indices_outras:
                resultados[idx] = await self._executar(chamadas_validas[idx])

            for i, ch in enumerate(chamadas):
                if i >= self.max_chamadas_por_passo:
                    resultado = 'ignorada: limite de chamadas por passo'
                else:
                    resultado = resultados[i]
                msgs.append(mensagem_resultado(ch, resultado))

        # Limite de passos: uma ultima chamada SEM ferramentas para ele dizer o que achou e o que falta, em vez de sumir.
        aviso = f'\n[limite de {self.max_passos} passos atingido; escreva "continue" para seguir]\n'
        self.escrever(aviso)
        final_msgs = list(msgs) + [
            {
                'role': 'user',
                'content': (
                    'Voce atingiu o limite de passos deste pedido. Sem usar ferramentas, resuma em poucas linhas: '
                    'o que descobriu (arquivos e linhas), qual e a causa provavel e o que falta para concluir.'
                ),
            }
        ]
        try:
            async for ev in self.conversar(final_msgs, []):
                if ev.tipo == 'texto' and ev.texto:
                    texto_turno += ev.texto
                    self.escrever(ev.texto)
        except Exception:  # noqa: BLE001 - o resumo e cortesia: se falhar, o aviso de limite ja foi dado
            pass
        return texto_turno + aviso

    def _usar_skill(self, nome: str) -> str:
        if not self.skills:
            return 'nenhuma skill instalada'

        if isinstance(self.skills, dict):
            skill = self.skills.get(nome)
            if skill is None:
                skill = resolver_skill(nome, list(self.skills.values()))
            if skill is not None:
                return ler_skill(skill)
            validas = ', '.join(sorted(self.skills.keys()))
            return f'skill desconhecida: {nome}. validas: {validas}'

        if callable(self.skills):
            try:
                res = self.skills(nome)
            except Exception as exc:
                return f'erro ao resolver skill: {exc}'

            if res is not None:
                if hasattr(res, 'caminho'):
                    return ler_skill(res)
                return str(res)

            validas = ''
            if hasattr(self.skills, 'skills'):
                sk = getattr(self.skills, 'skills')
                if isinstance(sk, dict):
                    validas = ', '.join(sorted(sk.keys()))
                elif isinstance(sk, (list, tuple)):
                    validas = ', '.join(sorted(getattr(s, 'nome', str(s)) for s in sk))
            elif hasattr(self.skills, 'nomes'):
                validas = ', '.join(sorted(getattr(self.skills, 'nomes')))
            elif hasattr(self.skills, '__closure__') and self.skills.__closure__:
                for cell in self.skills.__closure__:
                    val = cell.cell_contents
                    if isinstance(val, dict):
                        validas = ', '.join(sorted(val.keys()))
                        break
                    if isinstance(val, (list, tuple)) and val and hasattr(val[0], 'nome'):
                        validas = ', '.join(sorted(getattr(s, 'nome', str(s)) for s in val))
                        break

            if validas:
                return f'skill desconhecida: {nome}. validas: {validas}'
            return f'skill desconhecida: {nome}'

        if isinstance(self.skills, (list, tuple)):
            skill = resolver_skill(nome, self.skills)
            if skill is not None:
                return ler_skill(skill)
            validas = ', '.join(sorted(getattr(s, 'nome', str(s)) for s in self.skills))
            return f'skill desconhecida: {nome}. validas: {validas}'

        return 'nenhuma skill instalada'

    async def _executar_delegar(self, ch: ChamadaFerramenta) -> str:
        try:
            async with self._semaforo_delegar:
                return await self._executar(ch)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            return f'erro: {type(e).__name__}: {e}'

    async def _executar(self, ch: ChamadaFerramenta) -> str:
        if not ch.argumentos_validos:
            return 'argumentos invalidos (JSON)'
        if ch.nome not in _NOMES_VALIDOS:
            return 'ferramenta desconhecida. validas: ' + ', '.join(_NOMES_VALIDOS)

        args = ch.argumentos_json
        try:
            if ch.nome == 'ler_arquivo':
                self.escrever(f'-> ler_arquivo {args.get("caminho", "")}')
                r = self.leitura.ler_arquivo(
                    args['caminho'], inicio=args.get('inicio', 1), fim=args.get('fim')
                )
                return r.texto
            if ch.nome == 'listar_pasta':
                self.escrever(f'-> listar_pasta {args.get("caminho", ".")}')
                r = self.leitura.listar_pasta(args.get('caminho', '.'))
                return r.texto
            if ch.nome == 'buscar':
                self.escrever(f'-> buscar {args.get("padrao", "")}')
                r = self.leitura.buscar(
                    args['padrao'],
                    caminho=args.get('caminho', '.'),
                    glob=args.get('glob', '*'),
                )
                return r.texto
            if ch.nome == 'editar_arquivo':
                self.escrever(f'-> editar_arquivo {args.get("caminho", "")}')
                r = self.escrita.propor_edicao(
                    args['caminho'],
                    args['antigo'],
                    args['novo'],
                    todas=bool(args.get('todas', False)),
                )
                if not r.ok:
                    return r.texto
                return await self._confirmar()
            if ch.nome == 'escrever_arquivo':
                self.escrever(f'-> escrever_arquivo {args.get("caminho", "")}')
                r = self.escrita.propor_escrita(args['caminho'], args['conteudo'])
                if not r.ok:
                    return r.texto
                return await self._confirmar()
            if ch.nome == 'executar_comando':
                self.escrever(f'-> executar_comando {args.get("comando", "")}')
                r = self.escrita.propor_comando(
                    args['comando'], timeout=int(args.get('timeout', 120))
                )
                if not r.ok:
                    return r.texto
                return await self._confirmar()
            if ch.nome == 'usar_skill':
                nome = str(args.get('nome', '')).strip()
                self.escrever(f'-> usar_skill {nome}')
                return self._usar_skill(nome)
            if ch.nome == 'delegar_tarefa':
                faixa = str(args.get('faixa') or 'leve')
                tarefa = str(args.get('tarefa') or '')
                self.escrever(f'-> delegar_tarefa [{faixa}] {tarefa[:60]}')
                if self.delegar is None:
                    return 'sub-agentes indisponiveis'
                res = await self.delegar(tarefa, faixa)
                return str(res)[:4000]
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            return f'erro: {type(e).__name__}: {e}'

        return 'ferramenta desconhecida. validas: ' + ', '.join(_NOMES_VALIDOS)

    async def _confirmar(self) -> str:
        p = self.escrita.pendentes()[-1]
        self.escrever(p.diff if p.diff else p.resumo)
        if self.escrita.precisa_aprovacao(p):
            try:
                r = await self.aprovar(p)
            except asyncio.CancelledError:
                self.escrita.rejeitar(p.id)
                raise
            except Exception:                       # noqa: BLE001 - o prompt quebrou: na duvida, nao grava
                self.escrita.rejeitar(p.id)
                raise
            if r not in ('sim', 'sempre'):          # FALHA FECHADA: so um sim explicito aplica
                self.escrita.rejeitar(p.id)
                return 'o usuario recusou'
            if r == 'sempre':
                self.escrita.lembrar(p)
        res = self.escrita.aplicar(p.id)
        return res.texto
