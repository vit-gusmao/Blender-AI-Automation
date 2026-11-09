"""Cliente MCP sobre stdio (JSON-RPC newline-delimited) — §13.5.

O gateway é um cliente MCP qualquer: lança ``python -m
universal_blender_mcp`` (serve) como subprocesso e fala initialize →
tools/list → tools/call. Compatível com qualquer servidor MCP stdio.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_PROTOCOL_VERSION = "2024-11-05"


class MCPStdioClient:
    def __init__(self, command: Optional[List[str]] = None,
                 env: Optional[Dict[str, str]] = None,
                 server_name: str = "universal-blender-mcp"):
        self.command = command or [sys.executable, "-m", "universal_blender_mcp"]
        self.env = env or {}
        self.server_name = server_name
        self._process: Optional[asyncio.subprocess.Process] = None
        self._next_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._write_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._reader_error: Optional[Exception] = None
        self.tools: List[Dict[str, Any]] = []
        self.server_info: Dict[str, Any] = {}

    # -- ciclo de vida ------------------------------------------------------------

    async def start(self) -> None:
        if self._process is not None or self._closed:
            raise RuntimeError('MCP client already started or closed')
        env = {**os.environ, **self.env}
        pythonpath_parts = [str(Path(__file__).resolve().parents[1]
                                / "mcp_server" / "src")]
        if env.get("PYTHONPATH"):
            pythonpath_parts.append(env["PYTHONPATH"])
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)

        self._process = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=env)
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._drain_stderr()
        try:
            await self.initialize()
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        async with self._close_lock:
            self._closed = True
            self._fail_pending(RuntimeError('MCP client closed'))
            if self._process:
                if self._process.stdin:
                    self._process.stdin.close()
                if self._process.returncode is None:
                    try:
                        self._process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(self._process.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        try:
                            self._process.kill()
                        except ProcessLookupError:
                            pass
                        await self._process.wait()
                else:
                    await self._process.wait()
            tasks = [t for t in (self._reader_task, self._stderr_task)
                     if t is not None and t is not asyncio.current_task()]
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

    def _fail_pending(self, exc: Exception) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def _drain_stderr(self) -> None:
        if self._process and self._process.stderr:

            async def drain() -> None:
                while True:
                    line = await self._process.stderr.readline()
                    if not line:
                        return

            self._stderr_task = asyncio.create_task(drain())

    # -- protocolo --------------------------------------------------------------------

    async def _read_loop(self) -> None:
        assert self._process and self._process.stdout
        error = RuntimeError('MCP stdout EOF')
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                if not line.strip():
                    continue
                message = json.loads(line.decode("utf-8"))
                if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
                    raise ValueError('invalid JSON-RPC response')
                msg_id = message.get('id')
                if msg_id is not None:
                    if not isinstance(msg_id, (str, int)):
                        raise ValueError('invalid response id')
                    future = self._pending.get(msg_id)
                    if future is not None and not future.done():
                        if ('result' in message) == ('error' in message):
                            raise ValueError('response must contain result or error')
                        future.set_result(message)
        except asyncio.CancelledError:
            error = RuntimeError('MCP reader closed')
            raise
        except Exception as exc:
            error = RuntimeError(f'MCP protocol/read failure: {type(exc).__name__}')
        finally:
            self._reader_error = error
            self._fail_pending(error)

    async def _write(self, payload: Dict[str, Any]) -> None:
        async with self._write_lock:
            if self._closed or self._reader_error:
                raise RuntimeError('MCP client closed or reader failed')
            if self._process is None or self._process.stdin is None:
                raise RuntimeError('cliente MCP não iniciado')
            self._process.stdin.write((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))
            await self._process.stdin.drain()

    async def _request(self, method: str,
                       params: Optional[Dict[str, Any]] = None,
                       timeout_s: float = 180.0) -> Dict[str, Any]:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("cliente MCP não iniciado")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method,
                   "params": params or {}}
        try:
            await self._write(payload)
            response = await asyncio.wait_for(future, timeout=timeout_s)
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
        if "error" in response:
            raise RuntimeError(f"{method}: {response['error']}")
        return response.get("result", {})

    async def _notify(self, method: str,
                      params: Optional[Dict[str, Any]] = None) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("cliente MCP não iniciado")
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        await self._write(payload)

    async def initialize(self) -> Dict[str, Any]:
        result = await self._request("initialize", {
            "protocolVersion": _PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "universal-agent-gateway",
                           "version": "0.1.0"},
        })
        await self._notify("notifications/initialized")
        self.server_info = result.get("serverInfo", {})
        return result

    # -- ferramentas ---------------------------------------------------------------------

    async def list_tools(self) -> List[Dict[str, Any]]:
        result = await self._request("tools/list", {})
        self.tools = result.get("tools", [])
        return self.tools

    async def call_tool(self, name: str,
                        arguments: Optional[Dict[str, Any]] = None,
                        timeout_s: float = 300.0) -> Dict[str, Any]:
        """Chama uma ferramenta e devolve {"content": str, "parsed": dict}."""
        command_timeout = (arguments or {}).get('timeout_s', 0)
        if isinstance(command_timeout, (int, float)) and command_timeout > 0:
            timeout_s = max(timeout_s, command_timeout + 10.0)
        result = await self._request(
            "tools/call", {"name": name, "arguments": arguments or {}},
            timeout_s=timeout_s)
        text_parts = [
            c.get("text", "") for c in result.get("content", [])
            if c.get("type") == "text"
        ]
        text = "\n".join(text_parts)
        parsed: Dict[str, Any] = {}
        if text.strip().startswith("{"):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = {}
        return {"content": text, "parsed": parsed,
                "is_error": bool(result.get("isError", False))}
