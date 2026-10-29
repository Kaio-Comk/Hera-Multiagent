from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from cli import chat_cmd
from cli.chat_client import ErroDoServidor


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def test_servidor_no_ar_200() -> None:
    def get(url: str, timeout: float) -> _Resp:
        assert url.endswith('/healthz')
        assert timeout == 2.0
        return _Resp(200)

    assert chat_cmd.servidor_no_ar('http://127.0.0.1:8000', get=get) is True


def test_servidor_no_ar_500() -> None:
    def get(url: str, timeout: float) -> _Resp:
        return _Resp(500)

    assert chat_cmd.servidor_no_ar('http://127.0.0.1:8000', get=get) is False


def test_servidor_no_ar_excecao() -> None:
    def get(url: str, timeout: float) -> _Resp:
        raise OSError('sem rede')

    assert chat_cmd.servidor_no_ar('http://127.0.0.1:8000', get=get) is False


def test_garantir_servidor_ja_no_ar_nao_inicia() -> None:
    chamadas: list[object] = []

    def iniciar(comando: list[str], cwd: Path) -> object:
        chamadas.append((comando, cwd))
        return object()

    assert (
        chat_cmd.garantir_servidor(
            'http://127.0.0.1:8000',
            Path('.'),
            iniciar=iniciar,
            no_ar=lambda url: True,
        )
        is True
    )
    assert chamadas == []


def test_garantir_servidor_sobe_na_terceira() -> None:
    estado = {'n': 0}
    iniciados: list[tuple[list[str], Path]] = []

    def no_ar(url: str) -> bool:
        estado['n'] += 1
        return estado['n'] >= 3

    def iniciar(comando: list[str], cwd: Path) -> object:
        iniciados.append((comando, cwd))
        return object()

    raiz = Path('/proj')
    assert (
        chat_cmd.garantir_servidor(
            'http://127.0.0.1:8000',
            raiz,
            iniciar=iniciar,
            no_ar=no_ar,
            dormir=lambda s: None,
        )
        is True
    )
    assert len(iniciados) == 1
    comando, cwd = iniciados[0]
    assert comando[1:] == ['-m', 'main', 'web', '--no-browser']      # como modulo: funciona instalado e no checkout
    assert cwd == raiz


def test_garantir_servidor_estoura_limite() -> None:
    relogio = {'t': 0.0}
    mensagens: list[str] = []

    def agora() -> float:
        relogio['t'] += 10.0
        return relogio['t']

    def iniciar(comando: list[str], cwd: Path) -> object:
        return object()

    assert (
        chat_cmd.garantir_servidor(
            'http://127.0.0.1:8000',
            Path('.'),
            iniciar=iniciar,
            no_ar=lambda url: False,
            dormir=lambda s: None,
            agora=agora,
            limite_s=45.0,
            escrever=mensagens.append,
        )
        is False
    )
    assert any('nao subiu' in m for m in mensagens)


def test_garantir_servidor_url_remota() -> None:
    iniciados: list[object] = []
    mensagens: list[str] = []

    def iniciar(comando: list[str], cwd: Path) -> object:
        iniciados.append(comando)
        return object()

    assert (
        chat_cmd.garantir_servidor(
            'http://exemplo.com:8000',
            Path('.'),
            iniciar=iniciar,
            no_ar=lambda url: False,
            escrever=mensagens.append,
        )
        is False
    )
    assert iniciados == []
    assert mensagens


def _args(url: str = 'http://127.0.0.1:8000', modelo: str = 'default') -> argparse.Namespace:
    return argparse.Namespace(url=url, modelo=modelo)


async def test_executar_servidor_nao_sobe() -> None:
    codigo = await chat_cmd.executar(
        _args(),
        raiz=Path('.'),
        garantir=lambda url, raiz: False,
        principal=lambda *a, **k: None,
        token_de=lambda raiz: 'tok',
    )
    assert codigo == 1


async def test_executar_sem_token(capsys: pytest.CaptureFixture[str]) -> None:
    def token_de(raiz: Path) -> str:
        raise ErroDoServidor(401, 'token invalido')

    codigo = await chat_cmd.executar(
        _args(),
        raiz=Path('.'),
        garantir=lambda url, raiz: True,
        principal=lambda *a, **k: None,
        token_de=token_de,
    )
    assert codigo == 1
    assert 'token invalido' in capsys.readouterr().out


async def test_executar_caminho_feliz() -> None:
    chamadas: list[tuple[str, str, str]] = []

    async def principal(url: str, token: str, modelo: str, **_kw) -> None:
        chamadas.append((url, token, modelo))

    codigo = await chat_cmd.executar(
        _args(url='http://127.0.0.1:9000', modelo='m1'),
        raiz=Path('.'),
        garantir=lambda url, raiz: True,
        principal=principal,
        token_de=lambda raiz: 'tok',
    )
    assert codigo == 0
    assert chamadas == [('http://127.0.0.1:9000', 'tok', 'm1')]


def test_registrar_cria_subcomando() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    chat_cmd.registrar(subparsers)

    args = parser.parse_args(['chat'])
    assert args.modelo is None
    assert args.sem_maestro is False
    assert args.perfil is None
    assert args.janela == 32000
    assert args.url == 'http://127.0.0.1:8000'
    assert args.func is chat_cmd.executar


# ---- sessoes persistentes (2.2) -------------------------------------------------------------------------------------

class SessoesFalsas:
    def __init__(self, conversas=None, mensagens=None):
        self.conversas = conversas or []
        self.mensagens = mensagens or []
        self.turnos: list[tuple[str, str, str]] = []
        self.criadas = 0

    def resolver(self, ref):
        return self.conversas[0] if self.conversas else None

    def carregar(self, conversa_id):
        return list(self.mensagens)

    def nova(self, titulo=''):
        self.criadas += 1
        return f'c-nova-{self.criadas}'

    def registrar_turno(self, conversa_id, usuario, assistente):
        self.turnos.append((conversa_id, usuario, assistente))

    def listar(self, limite=20):
        return [{'id': 'c1', 'titulo': 'Minha conversa', 'atualizada': '2026-10-29 10:00', 'pasta': 'B:/x', 'mensagens': 4, 'desta_pasta': True}]


def _args2(**extra) -> argparse.Namespace:
    base = dict(url='http://127.0.0.1:8000', modelo='default', resume=None, listar=False)
    base.update(extra)
    return argparse.Namespace(**base)


async def test_registrar_aceita_resume_com_e_sem_valor_e_listar() -> None:
    parser = argparse.ArgumentParser()
    chat_cmd.registrar(parser.add_subparsers())
    assert parser.parse_args(['chat']).resume is None
    assert parser.parse_args(['chat', '--resume']).resume == ''
    assert parser.parse_args(['chat', '--resume', 'abc']).resume == 'abc'
    assert parser.parse_args(['chat', '--listar']).listar is True


async def test_listar_nao_sobe_servidor_e_devolve_0(capsys) -> None:
    def garantir(*_a, **_k):
        raise AssertionError('listar nao pode subir o servidor')

    codigo = await chat_cmd.executar(_args2(listar=True), raiz=Path('.'), garantir=garantir,
                                     sessoes_de=lambda: SessoesFalsas())
    saida = capsys.readouterr().out
    assert codigo == 0 and 'Minha conversa' in saida and 'c1' in saida


async def test_resume_carrega_mensagens_e_passa_ao_principal(capsys) -> None:
    falsas = SessoesFalsas(conversas=[{'id': 'c9', 'title': 'Antiga'}], mensagens=[{'role': 'user', 'content': 'oi'}, {'role': 'assistant', 'content': 'ola'}])
    recebido: dict = {}

    async def principal(url, token, modelo, **kw):
        recebido.update(kw)
        kw['ao_turno']('pergunta', 'resposta')

    codigo = await chat_cmd.executar(_args2(resume=''), raiz=Path('.'), garantir=lambda u, r: True, principal=principal,
                                     token_de=lambda r: 't', sessoes_de=lambda: falsas)
    assert codigo == 0
    assert recebido['mensagens_iniciais'] == falsas.mensagens
    assert falsas.turnos == [('c9', 'pergunta', 'resposta')]      # continua a MESMA conversa
    assert falsas.criadas == 0
    assert 'Retomando: Antiga (2 mensagens)' in capsys.readouterr().out


async def test_resume_sem_conversa_devolve_1_sem_subir_servidor(capsys) -> None:
    def garantir(*_a, **_k):
        raise AssertionError('nao deveria subir o servidor')

    codigo = await chat_cmd.executar(_args2(resume='x'), raiz=Path('.'), garantir=garantir, sessoes_de=lambda: SessoesFalsas())
    assert codigo == 1 and 'Nenhuma conversa' in capsys.readouterr().out


async def test_conversa_nova_so_e_criada_no_primeiro_turno() -> None:
    falsas = SessoesFalsas()

    async def principal(url, token, modelo, **kw):
        assert falsas.criadas == 0          # abrir o chat e sair sem falar nao deixa conversa vazia
        kw['ao_turno']('a', 'b')
        kw['ao_turno']('c', 'd')

    await chat_cmd.executar(_args2(), raiz=Path('.'), garantir=lambda u, r: True, principal=principal,
                            token_de=lambda r: 't', sessoes_de=lambda: falsas)
    assert falsas.criadas == 1
    assert falsas.turnos == [('c-nova-1', 'a', 'b'), ('c-nova-1', 'c', 'd')]


# ---- agente de codigo -----------------------------------------------------------------------------------------------

def test_registrar_aceita_agente_e_modo() -> None:
    parser = argparse.ArgumentParser()
    chat_cmd.registrar(parser.add_subparsers())

    args_padrao = parser.parse_args(['chat'])
    assert args_padrao.agente is False
    assert args_padrao.modo == 'guarded'

    args_agente = parser.parse_args(['chat', '--agente', '--modo', 'workspace'])
    assert args_agente.agente is True
    assert args_agente.modo == 'workspace'

    args_ro = parser.parse_args(['chat', '--agente', '--modo', 'read-only'])
    assert args_ro.agente is True
    assert args_ro.modo == 'read-only'


async def test_executar_sem_agente_passa_agente_cfg_none() -> None:
    recebido: dict = {}

    async def principal(*a, **kw):
        recebido.update(kw)

    args = argparse.Namespace(
        url='http://127.0.0.1:8000',
        modelo='default',
        resume=None,
        listar=False,
        agente=False,
        modo='guarded',
    )
    codigo = await chat_cmd.executar(
        args,
        raiz=Path('.'),
        garantir=lambda u, r: True,
        principal=principal,
        token_de=lambda r: 't',
    )
    assert codigo == 0
    assert recebido.get('agente_cfg') is None


async def test_executar_com_agente_monta_config_agente(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    recebido: dict = {}

    async def principal(*a, **kw):
        recebido.update(kw)

    args = argparse.Namespace(
        url='http://127.0.0.1:8000',
        modelo='default',
        resume=None,
        listar=False,
        agente=True,
        modo='workspace',
    )
    codigo = await chat_cmd.executar(
        args,
        raiz=Path('.'),
        garantir=lambda u, r: True,
        principal=principal,
        token_de=lambda r: 't',
    )
    assert codigo == 0
    cfg = recebido.get('agente_cfg')
    assert cfg is not None
    assert cfg.modo == 'workspace'
    assert cfg.escrita.modo == 'workspace'
    assert cfg.raiz.pasta == tmp_path.resolve()
    assert 'ferramentas para ler antes de editar' in recebido.get('sistema', '')
    assert 'executar_comando' in recebido.get('sistema', '')


async def test_executar_com_agente_cfg_injetado() -> None:
    from cli.chat_comandos import ConfigAgente

    cfg_injetado = ConfigAgente(
        raiz=Path('.'),
        checkpoints=object(),
        leitura=object(),
        escrita=object(),
        modo='read-only',
    )
    recebido: dict = {}

    async def principal(*a, **kw):
        recebido.update(kw)

    args = argparse.Namespace(
        url='http://127.0.0.1:8000',
        modelo='default',
        resume=None,
        listar=False,
        agente=False,
        modo='guarded',
    )
    codigo = await chat_cmd.executar(
        args,
        raiz=Path('.'),
        garantir=lambda u, r: True,
        principal=principal,
        token_de=lambda r: 't',
        agente_cfg=cfg_injetado,
    )
    assert codigo == 0
    assert recebido.get('agente_cfg') is cfg_injetado
    assert 'ferramentas para ler antes de editar' in recebido.get('sistema', '')


# ---- opcoes maestro e janela ----------------------------------------------------------------------------------------

def test_registrar_opcoes_maestro_e_janela() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers()
    chat_cmd.registrar(subparsers)

    args = parser.parse_args(
        ['chat', '--modelo', 'gpt-4o', '--sem-maestro', '--perfil', 'economico', '--janela', '16000']
    )
    assert args.modelo == 'gpt-4o'
    assert args.sem_maestro is True
    assert args.perfil == 'economico'
    assert args.janela == 16000


async def test_executar_com_modelo_forcado_no_maestro() -> None:
    recebido: dict = {}

    async def principal(*a, **kw) -> None:
        recebido.update(kw)

    args = argparse.Namespace(
        url='http://127.0.0.1:8000',
        modelo='gpt-4o-mini',
        sem_maestro=False,
        perfil=None,
        janela=32000,
        resume=None,
        listar=False,
        agente=False,
        modo='guarded',
    )
    codigo = await chat_cmd.executar(
        args,
        raiz=Path('.'),
        garantir=lambda u, r: True,
        principal=principal,
        token_de=lambda r: 'tok',
    )
    assert codigo == 0
    maestro = recebido.get('maestro')
    assert maestro is not None
    assert maestro.forcado == 'gpt-4o-mini'
    assert maestro.ativo is True


async def test_executar_com_sem_maestro() -> None:
    recebido: dict = {}

    async def principal(*a, **kw) -> None:
        recebido.update(kw)

    args = argparse.Namespace(
        url='http://127.0.0.1:8000',
        modelo=None,
        sem_maestro=True,
        perfil=None,
        janela=32000,
        resume=None,
        listar=False,
        agente=False,
        modo='guarded',
    )
    codigo = await chat_cmd.executar(
        args,
        raiz=Path('.'),
        garantir=lambda u, r: True,
        principal=principal,
        token_de=lambda r: 'tok',
    )
    assert codigo == 0
    maestro = recebido.get('maestro')
    assert maestro is not None
    assert maestro.ativo is False


async def test_executar_com_perfil_e_janela() -> None:
    recebido: dict = {}

    async def principal(*a, **kw) -> None:
        recebido.update(kw)

    args = argparse.Namespace(
        url='http://127.0.0.1:8000',
        modelo=None,
        sem_maestro=False,
        perfil='qualidade',
        janela=16000,
        resume=None,
        listar=False,
        agente=False,
        modo='guarded',
    )
    codigo = await chat_cmd.executar(
        args,
        raiz=Path('.'),
        garantir=lambda u, r: True,
        principal=principal,
        token_de=lambda r: 'tok',
    )
    assert codigo == 0
    maestro = recebido.get('maestro')
    assert maestro is not None
    assert maestro.perfil == 'qualidade'
    assert recebido.get('janela') == 16000
