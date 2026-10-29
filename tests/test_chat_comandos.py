from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cli.chat_client import ErroDoServidor
from cli.chat_comandos import ComandoSkill, Comandos, ConfigAgente, EstadoChat, formatar_uso
from cli.chat_repl import Sessao, rodar

try:
    from app.memory.index import MemoryIndex
    from app.memory.store import MemoryStore
except ImportError:
    try:
        from memoria.index import MemoryIndex  # type: ignore[no-redef]
        from memoria.store import MemoryStore  # type: ignore[no-redef]
    except ImportError:
        MemoryIndex = Any  # type: ignore[misc,assignment]
        MemoryStore = Any  # type: ignore[misc,assignment]

try:
    from app.skills import Skill
except ImportError:
    try:
        from skills import Skill  # type: ignore[no-redef]
    except ImportError:
        Skill = Any  # type: ignore[misc,assignment]


class ClienteFalso:
    '''Cliente de teste com modelo mutavel e lista de modelos assincrona.'''

    def __init__(self, modelos_retorno: list[str] | Exception | None = None) -> None:
        self.modelo = 'default'
        self.modelos_retorno = modelos_retorno if modelos_retorno is not None else ['default', 'modelo-a', 'modelo-b']
        self.chamadas_conversar: list[list[dict[str, Any]]] = []
        self.retorno_uso: Any = None

    async def modelos(self) -> list[str]:
        if isinstance(self.modelos_retorno, Exception):
            raise self.modelos_retorno
        return list(self.modelos_retorno)

    async def uso(self, dias: int = 1) -> dict:
        if isinstance(self.retorno_uso, Exception):
            raise self.retorno_uso
        return self.retorno_uso or {}

    async def conversar(self, mensagens: Any):
        self.chamadas_conversar.append([dict(m) for m in mensagens])
        yield 'resposta do modelo'


class SessoesFalsas:
    '''Sessoes simuladas para teste de comandos.'''

    def __init__(
        self,
        conversas: list[dict[str, Any]] | None = None,
        mensagens: list[dict[str, Any]] | None = None,
    ) -> None:
        self.conversas = list(conversas or [])
        self.mensagens = list(mensagens or [])
        self.turnos: list[tuple[str, str, str]] = []
        self.criadas = 0

    def resolver(self, ref: str | None) -> dict[str, Any] | None:
        if not ref:
            return self.conversas[0] if self.conversas else None
        for c in self.conversas:
            if c.get('id') == ref:
                return c
        alvo = ref.lower()
        for c in self.conversas:
            if alvo in (c.get('title') or c.get('titulo') or '').lower():
                return c
        return None

    def carregar(self, conversa_id: str) -> list[dict[str, Any]]:
        return list(self.mensagens)

    def nova(self, titulo: str = '') -> str:
        self.criadas += 1
        return f'c-nova-{self.criadas}'

    def registrar_turno(self, conversa_id: str, usuario: str, assistente: str) -> None:
        self.turnos.append((conversa_id, usuario, assistente))

    def listar(self, limite: int = 20) -> list[dict[str, Any]]:
        return list(self.conversas[:limite])


@dataclass
class RelatorioFalso:
    restaurados: tuple[str, ...] = ()
    pulados: tuple[str, ...] = ()
    avisos: tuple[str, ...] = ()


@dataclass
class EntradaFalsa:
    id: str
    arquivo: str
    revertido: bool


class CheckpointsFalso:
    '''Checkpoints simulado para teste de comandos.'''

    def __init__(self, relatorio: RelatorioFalso | None = None) -> None:
        self.relatorio = relatorio or RelatorioFalso(
            restaurados=('ckp001',), pulados=(), avisos=()
        )
        self.chamadas_reverter: list[str | None] = []
        self.entradas: list[EntradaFalsa] = [
            EntradaFalsa('ckp001', 'app/main.py', False),
            EntradaFalsa('ckp002', 'app/config.py', True),
        ]

    def reverter(self, cid: str | None = None) -> RelatorioFalso:
        self.chamadas_reverter.append(cid)
        return self.relatorio

    def listar(self, limite: int = 10) -> list[EntradaFalsa]:
        return list(self.entradas[:limite])


class EscritaFalsa:
    '''Escrita simulada para teste de comandos.'''

    def __init__(self, modo: str = 'guarded') -> None:
        self.modo = modo


async def test_comando_help() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado)
    res = await cmd.executar('/help', sessao, saida.append)
    assert res is None
    assert len(saida) >= 9
    texto = ''.join(saida)
    for esperado in [
        '/help',
        '/clear',
        '/model',
        '/maestro',
        '/perfil',
        '/compact',
        '/resume',
        '/listar',
        '/memory',
        '/skills',
        '/cost',
        '/sair',
        '/instrucoes',
        '/rollback',
        '/checkpoints',
        '/modo',
    ]:
        assert esperado in texto


async def test_comando_exit_quit_sair() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado)
    for c in ['/exit', '/quit', '/sair']:
        res = await cmd.executar(c, sessao, saida.append)
        assert res == 'sair'


async def test_comando_clear_zera_conversa_id_e_mensagens() -> None:
    saida: list[str] = []
    estado = EstadoChat(conversa_id='c-antiga')
    cliente = ClienteFalso()
    sessao = Sessao()
    sessao.mensagens.append({'role': 'user', 'content': 'oi'})
    cmd = Comandos(cliente, estado)
    res = await cmd.executar('/clear', sessao, saida.append)
    assert res is None
    assert sessao.mensagens == []
    assert estado.conversa_id is None
    assert any('Conversa nova.' in s for s in saida)


async def test_comando_model_sem_argumento_com_lista() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso(['m1', 'm2'])
    cliente.modelo = 'm1'
    sessao = Sessao(modelo='m1')
    cmd = Comandos(cliente, estado)
    await cmd.executar('/model', sessao, saida.append)
    texto = ''.join(saida)
    assert 'm1' in texto
    assert 'm2' in texto


async def test_comando_model_sem_argumento_erro_de_rede() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso(ErroDoServidor(500, 'servidor offline'))
    cliente.modelo = 'm-atual'
    sessao = Sessao(modelo='m-atual')
    cmd = Comandos(cliente, estado)
    await cmd.executar('/model', sessao, saida.append)
    texto = ''.join(saida)
    assert 'm-atual' in texto
    assert 'servidor offline' not in texto


async def test_comando_model_com_nome_valido() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso(['m1', 'm2'])
    sessao = Sessao(modelo='default')
    cmd = Comandos(cliente, estado)
    await cmd.executar('/model m2', sessao, saida.append)
    assert cliente.modelo == 'm2'
    assert sessao.modelo == 'm2'


async def test_comando_model_desconhecido() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso(['m1', 'm2'])
    cliente.modelo = 'm1'
    sessao = Sessao(modelo='m1')
    cmd = Comandos(cliente, estado)
    await cmd.executar('/model inexistente', sessao, saida.append)
    assert cliente.modelo == 'm1'
    assert sessao.modelo == 'm1'
    assert any('Modelo desconhecido: inexistente' in s for s in saida)


async def test_comando_model_sem_lista_erro_de_rede_troca_modelo() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso(ErroDoServidor(500, 'erro de rede'))
    cliente.modelo = 'm1'
    sessao = Sessao(modelo='m1')
    cmd = Comandos(cliente, estado)
    await cmd.executar('/model m-remoto', sessao, saida.append)
    assert cliente.modelo == 'm-remoto'
    assert sessao.modelo == 'm-remoto'


async def test_comando_resume_com_conversa() -> None:
    saida: list[str] = []
    falsas = SessoesFalsas(
        conversas=[{'id': 'c-42', 'title': 'Sessao Antiga'}],
        mensagens=[{'role': 'user', 'content': 'ola'}],
    )
    estado = EstadoChat(sessoes_de=lambda: falsas)
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado)
    await cmd.executar('/resume c-42', sessao, saida.append)
    assert estado.conversa_id == 'c-42'
    assert sessao.mensagens == [{'role': 'user', 'content': 'ola'}]
    assert any('Retomando: Sessao Antiga (1 mensagens)' in s for s in saida)


async def test_comando_resume_sem_conversa() -> None:
    saida: list[str] = []
    falsas = SessoesFalsas(conversas=[])
    estado = EstadoChat(sessoes_de=lambda: falsas)
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado)
    await cmd.executar('/resume x', sessao, saida.append)
    assert any('Nenhuma conversa encontrada.' in s for s in saida)


async def test_comando_listar() -> None:
    saida: list[str] = []
    falsas = SessoesFalsas(
        conversas=[
            {
                'id': 'c1',
                'atualizada': '2026-10-29 10:00',
                'titulo': 'Conversa 1',
                'mensagens': 3,
                'pasta': 'B:/proj',
            }
        ]
    )
    estado = EstadoChat(sessoes_de=lambda: falsas)
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado)
    await cmd.executar('/listar', sessao, saida.append)
    texto = ''.join(saida)
    assert 'c1 | 2026-10-29 10:00 | Conversa 1 | 3 msgs | B:/proj' in texto


async def test_comando_memory_pasta_inexistente(tmp_path: Path) -> None:
    saida: list[str] = []
    pasta = tmp_path / 'inexistente'
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado, pasta_memoria=pasta)
    await cmd.executar('/memory', sessao, saida.append)
    assert any('Memoria vazia.' in s for s in saida)


async def test_comando_memory_com_pasta_real_e_busca(tmp_path: Path) -> None:
    pasta = tmp_path / 'mem'
    pasta.mkdir()
    store = MemoryStore(pasta)
    store.save('arq-decisao', 'Decisao de arquitetura', 'projeto', 'Usar SQLite para persistencia local')
    index = MemoryIndex(store, pasta / '.indice.sqlite')
    index.sincronizar()
    index.close()

    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado, pasta_memoria=pasta)

    saida_lista: list[str] = []
    await cmd.executar('/memory', sessao, saida_lista.append)
    assert any('arq-decisao | projeto | Decisao de arquitetura' in s for s in saida_lista)

    saida_busca: list[str] = []
    await cmd.executar('/memory SQLite', sessao, saida_busca.append)
    texto_busca = ''.join(saida_busca)
    assert 'arq-decisao [projeto]' in texto_busca
    assert 'SQLite' in texto_busca


async def test_comando_skills_e_cost() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado, skills=[])
    await cmd.executar('/skills', sessao, saida.append)
    await cmd.executar('/cost', sessao, saida.append)
    assert any('Nenhuma skill disponivel.' in s for s in saida)
    assert any('Hoje: 0 requisicoes' in s for s in saida)


async def test_comando_desconhecido_com_sugestao() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado, skills=[])
    await cmd.executar('/clearr', sessao, saida.append)
    assert any('Comando desconhecido: /clearr. Quis dizer /clear?' in s for s in saida)


async def test_comando_desconhecido_sem_sugestao_aponta_help() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado, skills=[])
    await cmd.executar('/xyzabc', sessao, saida.append)
    assert any('Comando desconhecido: /xyzabc' in s and '/help' in s for s in saida)


async def test_comando_erro_interno_vira_erro() -> None:
    saida: list[str] = []

    class SessoesComFalha:
        def resolver(self, ref: str | None) -> dict[str, Any]:
            raise RuntimeError('banco corrompido')

    estado = EstadoChat(sessoes_de=lambda: SessoesComFalha())
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado)
    res = await cmd.executar('/resume x', sessao, saida.append)
    assert res is None
    assert any('Erro: banco corrompido' in s for s in saida)


async def test_integracao_rodar_clear_nao_vai_ao_modelo_e_sair_encerra() -> None:
    linhas = ['/clear', '/sair']

    async def leitor() -> str:
        if not linhas:
            raise EOFError
        return linhas.pop(0)

    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    sessao.mensagens.append({'role': 'user', 'content': 'antiga'})
    comandos = Comandos(cliente, estado)

    fim_sessao = await rodar(
        cliente,
        leitor,
        saida.append,
        sessao=sessao,
        comandos=comandos,
    )
    assert fim_sessao.mensagens == []
    assert cliente.chamadas_conversar == []
    assert any('Conversa nova.' in s for s in saida)


async def test_comando_instrucoes_sem_arquivos() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado)
    await cmd.executar('/instrucoes', sessao, saida.append)
    assert any('Nenhuma instrucao carregada. Crie um HERA.md na pasta do projeto.' in s for s in saida)


async def test_comando_instrucoes_com_arquivos() -> None:
    from app.instrucoes import Fonte, Instrucoes

    saida: list[str] = []
    inst = Instrucoes(
        texto='conteudo',
        fontes=(
            Fonte(caminho='HERA.md', origem='projeto', bytes_lidos=150, truncada=False),
            Fonte(caminho='AGENTS.md', origem='projeto', bytes_lidos=9000, truncada=True),
        ),
        avisos=('Arquivo AGENTS.md truncado',),
    )
    estado = EstadoChat(instrucoes=inst)
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado)
    await cmd.executar('/instrucoes', sessao, saida.append)
    texto = ''.join(saida)
    assert 'HERA.md (projeto, 150 bytes)' in texto
    assert 'AGENTS.md (projeto, 9000 bytes, cortado)' in texto
    assert 'Aviso: Arquivo AGENTS.md truncado' in texto


async def test_comando_clear_mantem_sistema() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao(sistema='Prompt do sistema')
    sessao.mensagens.append({'role': 'user', 'content': 'mensagem'})
    cmd = Comandos(cliente, estado)
    await cmd.executar('/clear', sessao, saida.append)
    assert sessao.mensagens == []
    assert sessao.sistema == 'Prompt do sistema'


async def test_comando_instrucoes_sugestao() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado)
    await cmd.executar('/instrucoe', sessao, saida.append)
    assert any('Comando desconhecido: /instrucoe. Quis dizer /instrucoes?' in s for s in saida)


async def test_comando_rollback_sem_agente() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado)
    await cmd.executar('/rollback', sessao, saida.append)
    assert any('Agente desligado. Use: hera chat --agente' in s for s in saida)


async def test_comando_rollback_com_agente() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    ckp = CheckpointsFalso(
        RelatorioFalso(restaurados=('c1',), pulados=('p1',), avisos=('aviso x',))
    )
    cfg = ConfigAgente(
        raiz=Path('.'),
        checkpoints=ckp,
        leitura=object(),
        escrita=EscritaFalsa(),
        modo='guarded',
    )
    cmd = Comandos(cliente, estado, agente_cfg=cfg)
    await cmd.executar('/rollback c1', sessao, saida.append)
    assert ckp.chamadas_reverter == ['c1']
    assert any('Restaurado: c1' in s for s in saida)
    assert any('Pulado: p1' in s for s in saida)
    assert any('Aviso: aviso x' in s for s in saida)


async def test_comando_rollback_sem_id_passa_none() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    ckp = CheckpointsFalso()
    cfg = ConfigAgente(
        raiz=Path('.'),
        checkpoints=ckp,
        leitura=object(),
        escrita=EscritaFalsa(),
        modo='guarded',
    )
    cmd = Comandos(cliente, estado, agente_cfg=cfg)
    await cmd.executar('/rollback', sessao, saida.append)
    assert ckp.chamadas_reverter == [None]
    assert any('Restaurado: ckp001' in s for s in saida)


async def test_comando_checkpoints_sem_agente() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado)
    await cmd.executar('/checkpoints', sessao, saida.append)
    assert any('Agente desligado. Use: hera chat --agente' in s for s in saida)


async def test_comando_checkpoints_com_agente() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    ckp = CheckpointsFalso()
    cfg = ConfigAgente(
        raiz=Path('.'),
        checkpoints=ckp,
        leitura=object(),
        escrita=EscritaFalsa(),
        modo='guarded',
    )
    cmd = Comandos(cliente, estado, agente_cfg=cfg)
    await cmd.executar('/checkpoints', sessao, saida.append)
    texto = ''.join(saida)
    assert 'ckp001 | app/main.py | nao' in texto
    assert 'ckp002 | app/config.py | sim' in texto


async def test_comando_checkpoints_vazio() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    ckp = CheckpointsFalso()
    ckp.entradas = []
    cfg = ConfigAgente(
        raiz=Path('.'),
        checkpoints=ckp,
        leitura=object(),
        escrita=EscritaFalsa(),
        modo='guarded',
    )
    cmd = Comandos(cliente, estado, agente_cfg=cfg)
    await cmd.executar('/checkpoints', sessao, saida.append)
    assert any('Nenhum checkpoint registrado.' in s for s in saida)


async def test_comando_modo_sem_agente() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado)
    await cmd.executar('/modo', sessao, saida.append)
    assert any('Agente desligado. Use: hera chat --agente' in s for s in saida)


async def test_comando_modo_com_agente_exibe_e_altera() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    escrita = EscritaFalsa('guarded')
    cfg = ConfigAgente(
        raiz=Path('.'),
        checkpoints=object(),
        leitura=object(),
        escrita=escrita,
        modo='guarded',
    )
    cmd = Comandos(cliente, estado, agente_cfg=cfg)

    await cmd.executar('/modo', sessao, saida.append)
    assert any('Modo atual: guarded' in s for s in saida)

    saida.clear()
    await cmd.executar('/modo workspace', sessao, saida.append)
    assert escrita.modo == 'workspace'
    assert cfg.modo == 'workspace'
    assert any('Modo alterado para workspace.' in s for s in saida)

    saida.clear()
    await cmd.executar('/modo desconhecido', sessao, saida.append)
    assert escrita.modo == 'workspace'
    assert any('Modo invalido: desconhecido' in s for s in saida)


async def test_help_inclui_novos_comandos_agente() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado)
    await cmd.executar('/help', sessao, saida.append)
    texto = ''.join(saida)
    assert '/rollback' in texto
    assert '/checkpoints' in texto
    assert '/modo' in texto


async def test_sugestoes_novos_comandos() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado)

    await cmd.executar('/rollbac', sessao, saida.append)
    assert any('Quis dizer /rollback?' in s for s in saida)

    saida.clear()
    await cmd.executar('/checkpoint', sessao, saida.append)
    assert any('Quis dizer /checkpoints?' in s for s in saida)

    saida.clear()
    await cmd.executar('/mod', sessao, saida.append)
    assert any('Quis dizer /modo?' in s for s in saida)


# ---- testes de skills e custo --------------------------------------------------------------------------------------

def _skill_teste(tmp_path: Path, nome: str, desc: str, corpo: str = 'instrucoes') -> Skill:
    pasta = tmp_path / nome
    pasta.mkdir(parents=True, exist_ok=True)
    caminho = pasta / 'SKILL.md'
    caminho.write_text(f'---\nname: {nome}\ndescription: {desc}\n---\n{corpo}', encoding='utf-8')
    return Skill(
        nome=nome,
        descricao=desc,
        caminho=caminho,
        pasta=pasta,
        origem='teste',
        avisos=('aviso de teste',) if nome == 'com-aviso' else (),
    )


async def test_comando_skills_com_skills(tmp_path: Path) -> None:
    s1 = _skill_teste(tmp_path, 'skill-um', 'Primeira skill util')
    s2 = _skill_teste(tmp_path, 'com-aviso', 'Segunda skill com aviso')
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado, skills=[s1, s2])

    await cmd.executar('/skills', sessao, saida.append)
    texto = ''.join(saida)
    assert 'skill-um - Primeira skill util' in texto
    assert 'com-aviso - Segunda skill com aviso' in texto
    assert 'aviso: aviso de teste' in texto


async def test_linha_barra_skill_vira_comando_skill(tmp_path: Path) -> None:
    s = _skill_teste(tmp_path, 'deploy', 'Deploy do sistema', 'Rode deploy.sh')
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado, skills=[s])

    res = await cmd.executar('/deploy em producao', sessao, saida.append)
    assert isinstance(res, ComandoSkill)
    assert 'Use a skill "deploy"' in res.texto
    assert 'Rode deploy.sh' in res.texto
    assert 'Pedido do usuario: em producao' in res.texto


async def test_linha_barra_desconhecido_sem_skill_sugere_comando() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado, skills=[])

    res = await cmd.executar('/clearr', sessao, saida.append)
    assert res is None
    assert any('Comando desconhecido: /clearr. Quis dizer /clear?' in s for s in saida)


def test_formatar_uso_gratuito() -> None:
    dados = {
        'totals': {'requests': 10, 'tokens': 500, 'cost': 0},
        'by_provider': [
            {'provider': 'groq', 'model': 'llama-3', 'requests': 10, 'tokens': 500, 'cost': 0}
        ],
    }
    linhas = formatar_uso(dados, perguntas=2)
    assert linhas[0] == 'Hoje: 10 requisicoes, 500 tokens, gasto real: US$ 0 (cota gratuita)'
    assert linhas[1] == 'groq/llama-3: 10 req, 500 tok, US$ 0'
    assert linhas[2] == 'Nesta sessao: 2 perguntas'


def test_formatar_uso_com_custo_e_por_provedor() -> None:
    dados = {
        'totals': {'requests': 15, 'prompt_tokens': 1000, 'completion_tokens': 500, 'cost': 0.0543},
        'by_provider': [
            {'provider': 'openai', 'model': 'gpt-4o', 'requests': 10, 'tokens': 1200, 'cost': 0.05},
            {'provider': 'anthropic', 'model': 'claude-3', 'requests': 5, 'tokens': 300, 'cost': 0.0043},
        ],
    }
    linhas = formatar_uso(dados, perguntas=5)
    assert 'Hoje: 15 requisicoes, 1500 tokens, gasto US$ 0.0543' in linhas[0]
    assert 'openai/gpt-4o: 10 req, 1200 tok, US$ 0.05' in linhas[1]
    assert 'anthropic/claude-3: 5 req, 300 tok, US$ 0.0043' in linhas[2]
    assert 'Nesta sessao: 5 perguntas' in linhas[3]


async def test_comando_cost_sucesso() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    cliente.retorno_uso = {
        'totals': {'requests': 3, 'tokens': 150, 'cost': 0.01},
        'by_provider': [{'provider': 'groq', 'model': 'llama-3', 'requests': 3, 'tokens': 150, 'cost': 0.01}],
    }
    sessao = Sessao()
    sessao.mensagens.append({'role': 'user', 'content': 'pergunta 1'})
    sessao.mensagens.append({'role': 'assistant', 'content': 'resposta 1'})
    sessao.mensagens.append({'role': 'user', 'content': 'pergunta 2'})

    cmd = Comandos(cliente, estado)
    await cmd.executar('/cost', sessao, saida.append)
    texto = ''.join(saida)
    assert 'Hoje: 3 requisicoes, 150 tokens, gasto US$ 0.0100' in texto
    assert 'groq/llama-3: 3 req, 150 tok, US$ 0.01' in texto
    assert 'Nesta sessao: 2 perguntas' in texto


async def test_comando_cost_erro_de_rede() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    cliente.retorno_uso = ErroDoServidor(503, 'roteador fora do ar')
    sessao = Sessao()
    cmd = Comandos(cliente, estado)

    await cmd.executar('/cost', sessao, saida.append)
    texto = ''.join(saida)
    assert 'Nao foi possivel obter custo: 503: roteador fora do ar' in texto


def test_formatar_uso_com_o_formato_real_do_servidor():
    """Regressao: o servidor devolve `cost_usd`; o formatador procurava `cost`/`custo` e mostrava 'US$ 0 (cota gratuita)' com gasto real."""
    from cli.chat_comandos import formatar_uso
    dados = {'days': 1, 'totals': {'requests': 75, 'prompt_tokens': 59925, 'completion_tokens': 123637, 'cost_usd': 0.067096, 'unpriced': 0},
             'by_provider': [{'provider': 'river', 'model': 'deepseek-ai/DeepSeek-V4.1-Flash', 'requests': 53, 'prompt_tokens': 48928,
                              'completion_tokens': 102770, 'cost_usd': 0.055203},
                             {'provider': 'agnes', 'model': 'agnes-2.5-flash', 'requests': 5, 'prompt_tokens': 900, 'completion_tokens': 147, 'cost_usd': 0.0}]}
    texto = '\n'.join(formatar_uso(dados, perguntas=3))
    assert 'gasto US$ 0.0671' in texto, texto
    assert 'cota gratuita' not in texto
    assert 'river/deepseek-ai/DeepSeek-V4.1-Flash: 53 req, 151698 tok, US$ 0.0552' in texto, texto
    assert 'agnes/agnes-2.5-flash: 5 req, 1047 tok, US$ 0' in texto
    assert 'Nesta sessao: 3 perguntas' in texto


def test_formatar_uso_zero_de_verdade_diz_cota_gratuita():
    from cli.chat_comandos import formatar_uso
    texto = '\n'.join(formatar_uso({'totals': {'requests': 2, 'prompt_tokens': 10, 'completion_tokens': 5, 'cost_usd': 0.0}, 'by_provider': []}))
    assert 'US$ 0 (cota gratuita)' in texto


# ---- maestro, perfil e compactacao ---------------------------------------------------------------------------------

class MaestroFalsoParaComandos:
    def __init__(self) -> None:
        self.ativo = True
        self.forcado = None
        self._perfil = None

    @property
    def perfil(self) -> str | None:
        return self._perfil

    @perfil.setter
    def perfil(self, valor: str | None) -> None:
        if valor is not None and valor not in ('economico', 'qualidade'):
            raise ValueError(f'perfil desconhecido: {valor}')
        self._perfil = valor

    def _prefs_efetivas(self) -> dict[str, list[str]]:
        return {
            'leve': ['mod-leve'],
            'medio': ['mod-medio'],
            'pesado': ['mod-pesado'],
        }


async def test_comando_model_auto() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso(['m1', 'm2'])
    maestro = MaestroFalsoParaComandos()
    maestro.forcado = 'm1'
    sessao = Sessao()
    cmd = Comandos(cliente, estado, maestro=maestro)

    await cmd.executar('/model auto', sessao, saida.append)
    assert maestro.forcado is None
    assert maestro.ativo is True
    assert any('Maestro ativado' in s for s in saida)


async def test_comando_model_nome_atualiza_forcado() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso(['m1', 'm2'])
    maestro = MaestroFalsoParaComandos()
    sessao = Sessao()
    cmd = Comandos(cliente, estado, maestro=maestro)

    await cmd.executar('/model m2', sessao, saida.append)
    assert maestro.forcado == 'm2'
    assert cliente.modelo == 'm2'
    assert sessao.modelo == 'm2'


async def test_comando_maestro_on_off_status() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    maestro = MaestroFalsoParaComandos()
    sessao = Sessao()
    cmd = Comandos(cliente, estado, maestro=maestro)

    await cmd.executar('/maestro off', sessao, saida.append)
    assert maestro.ativo is False
    assert any('Maestro desativado.' in s for s in saida)

    saida.clear()
    await cmd.executar('/maestro on', sessao, saida.append)
    assert maestro.ativo is True
    assert any('Maestro ativado.' in s for s in saida)

    saida.clear()
    await cmd.executar('/maestro status', sessao, saida.append)
    texto = ''.join(saida)
    assert 'Ativo: sim' in texto
    assert 'Perfil: padrao' in texto
    assert 'Forcado: nenhum' in texto
    assert 'mod-leve' in texto


async def test_comando_perfil() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    maestro = MaestroFalsoParaComandos()
    sessao = Sessao()
    cmd = Comandos(cliente, estado, maestro=maestro)

    await cmd.executar('/perfil', sessao, saida.append)
    assert any('Perfil atual: padrao' in s for s in saida)

    saida.clear()
    await cmd.executar('/perfil economico', sessao, saida.append)
    assert maestro.perfil == 'economico'
    assert any('Perfil alterado para economico.' in s for s in saida)

    saida.clear()
    await cmd.executar('/perfil qualidade', sessao, saida.append)
    assert maestro.perfil == 'qualidade'
    assert any('Perfil alterado para qualidade.' in s for s in saida)

    saida.clear()
    await cmd.executar('/perfil padrao', sessao, saida.append)
    assert maestro.perfil is None
    assert any('Perfil alterado para padrao.' in s for s in saida)

    saida.clear()
    await cmd.executar('/perfil turbo', sessao, saida.append)
    assert any('Perfil invalido: turbo.' in s for s in saida)


async def test_comando_compact() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado)

    await cmd.executar('/compact', sessao, saida.append)
    assert any('Nenhuma mensagem para compactar.' in s for s in saida)

    saida.clear()
    sessao.mensagens.append({'role': 'user', 'content': 'mensagem 1'})
    sessao.mensagens.append({'role': 'assistant', 'content': 'resposta 1'})
    await cmd.executar('/compact', sessao, saida.append)
    assert any('Contexto compactado:' in s for s in saida)


async def test_sugestoes_comandos_maestro_perfil_compact() -> None:
    saida: list[str] = []
    estado = EstadoChat()
    cliente = ClienteFalso()
    sessao = Sessao()
    cmd = Comandos(cliente, estado)

    await cmd.executar('/maestr', sessao, saida.append)
    assert any('Quis dizer /maestro?' in s for s in saida)

    saida.clear()
    await cmd.executar('/perfi', sessao, saida.append)
    assert any('Quis dizer /perfil?' in s for s in saida)

    saida.clear()
    await cmd.executar('/compac', sessao, saida.append)
    assert any('Quis dizer /compact?' in s for s in saida)
