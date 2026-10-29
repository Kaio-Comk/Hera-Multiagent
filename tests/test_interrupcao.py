from __future__ import annotations

import asyncio
import signal
import threading

import pytest

from cli.interrupcao import InterruptorSigint


def _restaurar(anterior) -> None:
    signal.signal(signal.SIGINT, anterior)


async def test_handler_instalado_e_restaurado() -> None:
    anterior = signal.getsignal(signal.SIGINT)
    try:
        tarefa = asyncio.create_task(asyncio.sleep(10))
        inter = InterruptorSigint()
        with inter.vigiar(tarefa):
            dentro = signal.getsignal(signal.SIGINT)
            assert dentro is not anterior
        assert signal.getsignal(signal.SIGINT) is anterior
        tarefa.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarefa
    finally:
        _restaurar(anterior)


async def test_sigint_cancela_tarefa() -> None:
    anterior = signal.getsignal(signal.SIGINT)
    try:
        tarefa = asyncio.create_task(asyncio.sleep(10))
        inter = InterruptorSigint()
        with inter.vigiar(tarefa):
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(0)
            with pytest.raises(asyncio.CancelledError):
                await tarefa
        assert inter.acionado is True
    finally:
        _restaurar(anterior)


async def test_varios_sigint_seguidos() -> None:
    anterior = signal.getsignal(signal.SIGINT)
    try:
        inter = InterruptorSigint()
        for _ in range(3):
            tarefa = asyncio.create_task(asyncio.sleep(10))
            with inter.vigiar(tarefa):
                signal.raise_signal(signal.SIGINT)
                await asyncio.sleep(0)
                with pytest.raises(asyncio.CancelledError):
                    await tarefa
            assert inter.acionado is True
    finally:
        _restaurar(anterior)


async def test_acionado_reseta_sem_sinal() -> None:
    anterior = signal.getsignal(signal.SIGINT)
    try:
        inter = InterruptorSigint()
        tarefa = asyncio.create_task(asyncio.sleep(10))
        with inter.vigiar(tarefa):
            signal.raise_signal(signal.SIGINT)
            await asyncio.sleep(0)
            with pytest.raises(asyncio.CancelledError):
                await tarefa
        assert inter.acionado is True

        tarefa2 = asyncio.create_task(asyncio.sleep(10))
        with inter.vigiar(tarefa2):
            assert inter.acionado is False
        tarefa2.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarefa2
    finally:
        _restaurar(anterior)


async def test_handler_restaurado_com_excecao() -> None:
    anterior = signal.getsignal(signal.SIGINT)
    try:
        tarefa = asyncio.create_task(asyncio.sleep(10))
        inter = InterruptorSigint()
        with pytest.raises(RuntimeError):
            with inter.vigiar(tarefa):
                raise RuntimeError('falha')
        assert signal.getsignal(signal.SIGINT) is anterior
        tarefa.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarefa
    finally:
        _restaurar(anterior)


async def test_thread_nao_principal() -> None:
    anterior = signal.getsignal(signal.SIGINT)
    loop = asyncio.get_running_loop()
    tarefa = loop.create_future()
    inter = InterruptorSigint()
    erro: list[BaseException] = []

    def _run() -> None:
        try:
            with inter.vigiar(tarefa):
                pass
        except BaseException as exc:  # noqa: BLE001
            erro.append(exc)

    thread = threading.Thread(target=_run)
    thread.start()
    thread.join()

    assert not erro
    assert signal.getsignal(signal.SIGINT) is anterior
    tarefa.cancel()
