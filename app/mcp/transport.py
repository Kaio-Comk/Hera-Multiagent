"""Transportes MCP: JSON-RPC 2.0 sobre stdio (uma mensagem JSON por linha) e sobre HTTP (streamable).

Limites de segurança em todos os caminhos: tamanho máximo de linha/resposta, timeout por requisição,
no máximo uma requisição por vez por sessão (lock), e QUALQUER pedido que o servidor faça ao cliente
(sampling, roots, elicitation...) é RECUSADO: o servidor MCP não usa o nosso LLM nem lê o nosso disco.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
from typing import Any, Dict, List, Optional

import httpx

log = logging.getLogger("hera.mcp")

MAX_LINE = 1024 * 1024            # 1 MiB por mensagem
MAX_STDERR = 64 * 1024            # guarda só o final do stderr (diagnóstico)
METHOD_NOT_FOUND = -32601


class McpTransportError(RuntimeError):
    """Falha de transporte/protocolo com o servidor MCP (timeout, processo morreu, resposta inválida)."""


class McpRemoteError(McpTransportError):
    """O servidor respondeu com um erro JSON-RPC."""

    def __init__(self, code: int, message: str):
        super().__init__(f"MCP erro {code}: {message}"[:300])
        self.code = code


def _kill_tree_sync(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=10, check=False)


class StdioTransport:
    def __init__(self, argv: List[str], env: Dict[str, str], cwd: Optional[str] = None):
        self.argv, self.env, self.cwd = argv, env, cwd
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._reader: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._stderr_tail = b""
        self._closed = False

    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and not self._closed

    @property
    def stderr_tail(self) -> str:
        return self._stderr_tail.decode("utf-8", "replace")[-500:]

    async def start(self) -> None:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._proc = await asyncio.create_subprocess_exec(
            *self.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=self.env, cwd=self.cwd, limit=MAX_LINE, creationflags=flags)
        self._reader = asyncio.ensure_future(self._read_loop())
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        try:
            while True:
                chunk = await self._proc.stderr.read(4096)
                if not chunk:
                    return
                self._stderr_tail = (self._stderr_tail + chunk)[-MAX_STDERR:]
        except Exception:                                  # noqa: BLE001
            return

    def _fail_all(self, exc: Exception) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _read_loop(self) -> None:
        try:
            while True:
                try:
                    line = await self._proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    self._fail_all(McpTransportError(f"Mensagem do servidor maior que {MAX_LINE} bytes"))
                    await self._terminate()
                    return
                if not line:
                    self._fail_all(McpTransportError(f"Servidor MCP encerrou (stderr: {self.stderr_tail!r})"))
                    return
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except ValueError:
                    log.debug("[mcp] linha não-JSON ignorada: %r", text[:80])
                    continue
                if not isinstance(msg, dict):
                    continue
                if "method" in msg:                        # pedido/notificação DO servidor
                    await self._on_server_message(msg)
                    continue
                fut = self._pending.pop(msg.get("id"), None)
                if fut is None or fut.done():
                    continue
                if "error" in msg and isinstance(msg["error"], dict):
                    fut.set_exception(McpRemoteError(int(msg["error"].get("code", -32000)),
                                                     str(msg["error"].get("message", "erro"))))
                else:
                    fut.set_result(msg.get("result"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:                           # noqa: BLE001
            self._fail_all(McpTransportError(f"Falha lendo do servidor MCP: {type(exc).__name__}"))

    async def _on_server_message(self, msg: Dict[str, Any]) -> None:
        """Pedidos do servidor ao cliente (com `id`) são RECUSADOS; `ping` recebe resposta vazia;
        notificações (sem `id`) são ignoradas."""
        if "id" not in msg:
            return
        if msg.get("method") == "ping":
            await self._write({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
        else:
            await self._write({"jsonrpc": "2.0", "id": msg["id"],
                               "error": {"code": METHOD_NOT_FOUND, "message": "Não suportado por este cliente"}})

    async def _write(self, obj: Dict[str, Any]) -> None:
        if not self.alive:
            raise McpTransportError("Servidor MCP não está em execução")
        data = (json.dumps(obj, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")
        try:
            self._proc.stdin.write(data)
            await self._proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as exc:
            raise McpTransportError(f"Falha escrevendo no servidor MCP: {type(exc).__name__}") from exc

    async def request(self, method: str, params: Optional[dict], timeout: float) -> Any:
        async with self._lock:                             # uma requisição por vez por sessão
            self._next_id += 1
            rid = self._next_id
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending[rid] = fut
            msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                msg["params"] = params
            try:
                await self._write(msg)
                return await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
            except asyncio.TimeoutError:
                self._pending.pop(rid, None)
                try:
                    await self._write({"jsonrpc": "2.0", "method": "notifications/cancelled",
                                       "params": {"requestId": rid, "reason": "timeout"}})
                except McpTransportError:
                    pass
                await self._terminate()                    # um servidor que não responde é encerrado
                raise McpTransportError(f"Timeout de {timeout:.0f}s em '{method}'; servidor encerrado")
            except BaseException:
                self._pending.pop(rid, None)
                raise

    async def notify(self, method: str, params: Optional[dict] = None) -> None:
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        await self._write(msg)

    async def _terminate(self) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        try:
            await asyncio.to_thread(_kill_tree_sync, proc.pid)
            if proc.returncode is None:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.stdin.close()
                await asyncio.wait_for(proc.wait(), timeout=2)     # encerramento limpo (stdin fechado)
            except (asyncio.TimeoutError, OSError, BrokenPipeError):
                await self._terminate()
        for task in (self._reader, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self._fail_all(McpTransportError("Sessão MCP encerrada"))


class HttpTransport:
    """MCP "streamable HTTP": cada mensagem é um POST; a resposta é JSON ou um fluxo SSE com a resposta."""

    def __init__(self, url: str, client: Optional[httpx.AsyncClient] = None):
        from app.gateway.safe_transport import make_safe_async_client
        self.url = url
        self._client = client or make_safe_async_client(timeout=30.0)     # anti-SSRF na conexão
        self._owns_client = client is None
        self._session_id: Optional[str] = None
        self._protocol_version: Optional[str] = None
        self._next_id = 0
        self._lock = asyncio.Lock()
        self.alive = True

    def set_protocol_version(self, version: str) -> None:
        self._protocol_version = version

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        if self._protocol_version:
            h["MCP-Protocol-Version"] = self._protocol_version
        return h

    async def request(self, method: str, params: Optional[dict], timeout: float) -> Any:
        async with self._lock:
            self._next_id += 1
            rid = self._next_id
            msg: Dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
            if params is not None:
                msg["params"] = params
            try:
                resp = await self._client.send(
                    self._client.build_request("POST", self.url, json=msg, headers=self._headers(), timeout=timeout),
                    stream=True)
            except httpx.TimeoutException as exc:
                raise McpTransportError(f"Timeout em '{method}'") from exc
            except httpx.HTTPError as exc:
                raise McpTransportError(f"Falha de rede com o servidor MCP: {type(exc).__name__}") from exc
            try:
                if resp.status_code >= 400:
                    raise McpTransportError(f"Servidor MCP respondeu HTTP {resp.status_code}")
                sid = resp.headers.get("mcp-session-id")
                if sid and method == "initialize":
                    self._session_id = sid
                ctype = resp.headers.get("content-type", "")
                if "text/event-stream" in ctype:
                    result = await self._read_sse(resp, rid, timeout)
                else:
                    body = await asyncio.wait_for(resp.aread(), timeout=timeout)
                    if len(body) > MAX_LINE * 4:
                        raise McpTransportError("Resposta do servidor MCP grande demais")
                    result = self._extract(json.loads(body), rid)
                return result
            except (ValueError, asyncio.TimeoutError) as exc:
                raise McpTransportError(f"Resposta inválida/lenta do servidor MCP: {type(exc).__name__}") from exc
            finally:
                await resp.aclose()

    @staticmethod
    def _extract(msg: Any, rid: int) -> Any:
        if isinstance(msg, list):                          # lote: procura a resposta do nosso id
            msg = next((m for m in msg if isinstance(m, dict) and m.get("id") == rid), None)
        if not isinstance(msg, dict):
            raise McpTransportError("Resposta JSON-RPC ausente")
        if "error" in msg and isinstance(msg["error"], dict):
            raise McpRemoteError(int(msg["error"].get("code", -32000)), str(msg["error"].get("message", "erro")))
        return msg.get("result")

    async def _read_sse(self, resp: httpx.Response, rid: int, timeout: float) -> Any:
        data_lines: List[str] = []
        total = 0

        async def consume():
            nonlocal total
            async for line in resp.aiter_lines():
                total += len(line)
                if total > MAX_LINE * 4:
                    raise McpTransportError("Fluxo SSE do servidor MCP grande demais")
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
                elif line == "" and data_lines:
                    payload = "\n".join(data_lines)
                    data_lines.clear()
                    try:
                        msg = json.loads(payload)
                    except ValueError:
                        continue
                    if isinstance(msg, dict) and msg.get("id") == rid and ("result" in msg or "error" in msg):
                        return self._extract(msg, rid)
            raise McpTransportError("Fluxo SSE terminou sem a resposta esperada")
        return await asyncio.wait_for(consume(), timeout=timeout)

    async def notify(self, method: str, params: Optional[dict] = None) -> None:
        msg: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            resp = await self._client.post(self.url, json=msg, headers=self._headers(), timeout=15.0)
            if resp.status_code >= 400:
                raise McpTransportError(f"Servidor MCP respondeu HTTP {resp.status_code}")
        except httpx.HTTPError as exc:
            raise McpTransportError(f"Falha de rede com o servidor MCP: {type(exc).__name__}") from exc

    async def close(self) -> None:
        self.alive = False
        if self._owns_client:
            await self._client.aclose()
