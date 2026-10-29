from __future__ import annotations

import asyncio
import contextlib
import signal
import threading
from collections.abc import Iterator
from typing import Any


class InterruptorSigint:
    '''Instala um handler de SIGINT que cancela uma tarefa por vez.'''

    def __init__(self) -> None:
        self._acionado: bool = False
        self._anterior: Any = None

    @property
    def acionado(self) -> bool:
        '''True se algum SIGINT chegou durante o ultimo vigiar.'''
        return self._acionado

    @contextlib.contextmanager
    def vigiar(self, tarefa: asyncio.Future) -> Iterator[None]:
        '''Vigia SIGINT durante o bloco, cancelando a tarefa a cada sinal.'''
        self._acionado = False
        loop = tarefa.get_loop()
        anterior: Any = None
        instalado = False

        if threading.current_thread() is threading.main_thread():
            try:
                anterior = signal.getsignal(signal.SIGINT)

                def _handler(signum: int, frame: Any) -> None:
                    self._acionado = True
                    try:
                        loop.call_soon_threadsafe(tarefa.cancel)
                    except RuntimeError:
                        pass

                signal.signal(signal.SIGINT, _handler)
                instalado = True
            except (ValueError, OSError):
                instalado = False

        try:
            yield
        finally:
            if instalado:
                try:
                    signal.signal(signal.SIGINT, anterior)
                except (ValueError, OSError):
                    pass
