"""Deadline-aware JSON-RPC client for MCP stdio diagnostics."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from collections.abc import Iterable
from typing import Any


class MCPDiagnosticTimeout(TimeoutError):
    """Raised when a diagnostic request exceeds its deadline."""


class MCPDiagnosticError(RuntimeError):
    """Raised when the child exits or returns an invalid response."""


_EOF = object()


class MCPDiagnosticClient:
    """Minimal line-delimited MCP client with deterministic process cleanup."""

    def __init__(
        self,
        *,
        command: str | None = None,
        args: Iterable[str] | None = None,
        env: dict[str, str] | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.command = command or sys.executable
        self.args = list(args or ["-m", "universal_blender_mcp", "serve"])
        self.env = dict(os.environ if env is None else env)
        self.timeout = float(timeout)
        self.process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[str | object] = queue.Queue()
        self._stderr: list[str] = []
        self._threads: list[threading.Thread] = []
        self._next_id = 0

    @staticmethod
    def _drain(stream, destination) -> None:
        try:
            for line in iter(stream.readline, ""):
                destination(line)
        finally:
            destination(_EOF)

    def start(self) -> "MCPDiagnosticClient":
        if self.process is not None:
            return self
        self.process = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=self.env,
        )
        stdout_thread = threading.Thread(
            target=self._drain,
            args=(self.process.stdout, self._responses.put),
            daemon=True,
            name="mcp-diag-stdout",
        )

        def append_stderr(item: str | object) -> None:
            if item is not _EOF:
                self._stderr.append(str(item))

        stderr_thread = threading.Thread(
            target=self._drain,
            args=(self.process.stderr, append_stderr),
            daemon=True,
            name="mcp-diag-stderr",
        )
        self._threads = [stdout_thread, stderr_thread]
        for thread in self._threads:
            thread.start()
        return self

    def _write(self, payload: dict[str, Any]) -> None:
        self.start()
        assert self.process is not None and self.process.stdin is not None
        if self.process.poll() is not None:
            raise MCPDiagnosticError(
                f"MCP child exited with {self.process.returncode}: {self.stderr_tail()}"
            )
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._write({"jsonrpc": "2.0", "method": method,
                     "params": params or {}})

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        self._next_id += 1
        request_id = self._next_id
        self._write({"jsonrpc": "2.0", "id": request_id,
                     "method": method, "params": params or {}})
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPDiagnosticTimeout(
                    f"deadline exceeded waiting for {method!r}"
                )
            try:
                item = self._responses.get(timeout=remaining)
            except queue.Empty as exc:
                raise MCPDiagnosticTimeout(
                    f"deadline exceeded waiting for {method!r}"
                ) from exc
            if item is _EOF:
                code = None if self.process is None else self.process.poll()
                raise MCPDiagnosticError(
                    f"MCP child closed stdout (exit={code}): {self.stderr_tail()}"
                )
            try:
                message = json.loads(str(item))
            except json.JSONDecodeError as exc:
                raise MCPDiagnosticError(f"invalid JSON-RPC line: {item!r}") from exc
            if message.get("id") == request_id:
                return message

    def initialize(self) -> dict[str, Any]:
        response = self.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-diagnostic", "version": "1"},
        })
        self.notify("notifications/initialized")
        return response

    def stderr_tail(self, limit: int = 3000) -> str:
        return "".join(self._stderr)[-limit:]

    def close(self) -> None:
        process = self.process
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=min(max(self.timeout, 0.1), 2.0))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        for thread in self._threads:
            thread.join(timeout=1.0)

    def __enter__(self) -> "MCPDiagnosticClient":
        return self.start()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

