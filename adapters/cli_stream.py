"""Roda um CLI de agente em modo `stream-json` e transforma a saida em eventos de progresso (console ao vivo).

Claude Code (`--output-format stream-json --verbose`) e Antigravity (`--output-format stream-json`) emitem uma linha JSON
por acontecimento: texto do agente, chamada de ferramenta, resultado da ferramenta e o resultado final. Os parsers daqui
convertem isso para `adapters.progress.say(...)` e guardam o resultado final (texto, custo, tokens).
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess  # nosec B404
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from adapters.progress import STATUS, TEXT, THINKING, TOOL, TOOL_RESULT, say, tool_summary

MAX_RAW = 2 * 1024 * 1024
MAX_ERR = 200 * 1024
STREAM_LIMIT = 32 * 1024 * 1024          # uma linha de resultado de ferramenta pode ser enorme


def _flat(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content)
    return str(content or "")


class ClaudeStream:
    """Parser do stream-json do Claude Code."""

    def __init__(self) -> None:
        self.final: Optional[str] = None
        self.is_error = False
        self.text_parts: List[str] = []
        self.meta: Dict[str, Any] = {}
        self._last = ""

    def feed(self, line: str) -> None:
        try:
            d = json.loads(line)
        except ValueError:
            say(STATUS, line[:300])
            return
        kind = d.get("type")
        if kind == "system":
            if d.get("subtype") == "init":
                say(STATUS, f"Claude Code iniciado (modelo {d.get('model', '?')}).")
        elif kind == "assistant":
            for block in (d.get("message") or {}).get("content") or []:
                btype = block.get("type")
                if btype == "text" and block.get("text"):
                    self.text_parts.append(block["text"])
                    say(TEXT, block["text"])
                    self._last = TEXT
                elif btype == "tool_use":
                    say(TOOL, tool_summary(block.get("name", "ferramenta"), block.get("input")))
                    self._last = TOOL
                elif btype == "thinking" and self._last != THINKING:
                    say(THINKING, "pensando...")
                    self._last = THINKING
        elif kind == "user":
            for block in (d.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    say(TOOL_RESULT, _flat(block.get("content"))[:1500], error=bool(block.get("is_error")))
                    self._last = TOOL_RESULT
        elif kind == "result":
            self.final = d.get("result") if isinstance(d.get("result"), str) else None
            self.is_error = bool(d.get("is_error"))
            usage = d.get("usage") or {}
            self.meta = {"cost_usd": d.get("total_cost_usd"), "duration_ms": d.get("duration_ms"),
                         "input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
                         "turns": d.get("num_turns")}
        elif kind == "rate_limit_event":
            info = d.get("rate_limit_info") or {}
            if info.get("status") not in (None, "allowed"):
                say(STATUS, f"Aviso de limite da assinatura: {info.get('status')}.")

    def text(self) -> str:
        return (self.final if self.final is not None else "\n\n".join(self.text_parts)).strip()


class AntigravityStream:
    """Parser do stream-json do Antigravity (`agy`)."""

    FLUSH_SECONDS = 0.6

    def __init__(self) -> None:
        self.final: Optional[str] = None
        self.ok: Optional[bool] = None
        self.error: str = ""
        self.meta: Dict[str, Any] = {}
        self._buffer: List[str] = []
        self._chunks: List[str] = []
        self._last_flush = time.monotonic()
        self._thinking = False

    def _flush(self) -> None:
        if self._buffer:
            text = "".join(self._buffer)
            self._buffer.clear()
            say(TEXT, text)
        self._last_flush = time.monotonic()

    def feed(self, line: str) -> None:
        try:
            d = json.loads(line)
        except ValueError:
            say(STATUS, line[:300])
            return
        event = d.get("event")
        if event == "init":
            say(STATUS, f"Antigravity iniciado (modelo {(d.get('init') or {}).get('model', '?')}).")
        elif event == "step_update":
            su = d.get("step_update") or {}
            stype, state = su.get("step_type"), su.get("state")
            if stype == "tool":
                info = su.get("tool_info") or {}
                if state == "ACTIVE":
                    self._flush()
                    say(TOOL, tool_summary(su.get("tool_name") or info.get("name") or "ferramenta", info.get("parameters")))
                elif state == "DONE" and info.get("output"):
                    say(TOOL_RESULT, str(info["output"])[:1500])
            elif stype == "agent_response":
                delta = su.get("text_delta") or ""
                if delta:
                    self._chunks.append(delta)
                    self._buffer.append(delta)
                    if "\n" in delta or len("".join(self._buffer)) >= 240 or time.monotonic() - self._last_flush >= self.FLUSH_SECONDS:
                        self._flush()
                if state == "DONE":
                    self._flush()
                usage = su.get("usage")
                if usage:
                    self.meta["input_tokens"] = (self.meta.get("input_tokens") or 0) + (usage.get("input_tokens") or 0)
                    self.meta["output_tokens"] = (self.meta.get("output_tokens") or 0) + (usage.get("output_tokens") or 0)
            elif stype not in ("user_input",) and not self._thinking and state == "ACTIVE":
                self._thinking = True
                say(THINKING, "pensando...")
        elif event == "result":
            self._flush()
            res = d.get("result") or {}
            self.final = res.get("response") if isinstance(res.get("response"), str) else None
            self.ok = str(res.get("status", "")).upper() == "SUCCESS"
            self.error = str(res.get("error") or res.get("message") or "")
            self.meta["duration_ms"] = int((res.get("duration_seconds") or 0) * 1000) or None

    def text(self) -> str:
        return (self.final if self.final is not None else "".join(self._chunks)).strip()


def _kill_tree(proc) -> None:
    try:
        if os.name == "nt" and proc.pid:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], stdout=subprocess.DEVNULL,  # nosec B603 B607
                           stderr=subprocess.DEVNULL, timeout=5, check=False)
        else:
            proc.kill()
    except Exception:       # noqa: BLE001 - encerrar nunca pode levantar
        pass


IDLE_LIMIT_SECONDS = 600.0      # agente sem NENHUMA saida por tanto tempo = comando travado (ex.: varredura de pasta gigante)


class IdleTimeout(asyncio.TimeoutError):
    """O agente ficou em silencio alem do limite: quase sempre um comando preso, nao trabalho em andamento."""


async def run_streaming(cmd: List[str], cwd: Optional[str], timeout: float, parser,
                        on_proc: Optional[Callable[[Any], None]] = None,
                        idle_limit: float = IDLE_LIMIT_SECONDS, stdin_text: Optional[str] = None) -> Tuple[int, str, str]:
    """Executa `cmd`, alimenta `parser` linha a linha e devolve (codigo, stdout_bruto, stderr). Levanta TimeoutError.
    `stdin_text`: texto enviado pela entrada padrao (prompts grandes demais para a linha de comando)."""
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, limit=STREAM_LIMIT,
                                                stdin=asyncio.subprocess.PIPE if stdin_text is not None else None)
    if on_proc:
        on_proc(proc)
    if stdin_text is not None and proc.stdin is not None:
        async def feed_stdin() -> None:
            try:
                proc.stdin.write(stdin_text.encode("utf-8"))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass                                   # o CLI fechou a entrada cedo: o resultado/erro vem pela saida
            finally:
                try:
                    proc.stdin.close()
                except Exception:       # noqa: BLE001
                    pass
        asyncio.ensure_future(feed_stdin())
    raw: List[str] = []
    raw_size = 0
    err_chunks: List[bytes] = []
    last_output = [time.monotonic()]

    async def read_out() -> None:
        nonlocal raw_size
        assert proc.stdout is not None
        while True:
            try:
                chunk = await proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError):
                await proc.stdout.read(STREAM_LIMIT)           # linha gigante: descarta em vez de travar
                continue
            if not chunk:
                break
            last_output[0] = time.monotonic()
            text = chunk.decode("utf-8", errors="replace").rstrip("\r\n")
            if raw_size < MAX_RAW:
                raw.append(text)
                raw_size += len(text)
            if text.strip():
                parser.feed(text)

    async def read_err() -> None:
        assert proc.stderr is not None
        size = 0
        while True:
            chunk = await proc.stderr.read(65536)
            if not chunk:
                break
            if size < MAX_ERR:
                err_chunks.append(chunk)
                size += len(chunk)

    try:
        work = asyncio.ensure_future(asyncio.gather(read_out(), read_err(), proc.wait()))
        deadline = time.monotonic() + timeout
        try:
            while not work.done():
                await asyncio.wait({work}, timeout=5.0)
                now = time.monotonic()
                if now > deadline:
                    raise asyncio.TimeoutError()
                if idle_limit and now - last_output[0] > idle_limit:
                    raise IdleTimeout(f"sem nenhuma saida ha {idle_limit / 60:.0f} min")
            work.result()
        finally:
            if not work.done():
                work.cancel()
    except (asyncio.TimeoutError, asyncio.CancelledError):
        _kill_tree(proc)                      # prazo estourou ou cancelaram: nao deixa o agente (e os filhos) rodando solto
        raise
    return proc.returncode or 0, "\n".join(raw), b"".join(err_chunks).decode("utf-8", errors="replace").strip()
