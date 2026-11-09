"""Executor de processos externos com limites (§8.1 do plano).

- Somente runtimes registrados e autorizados (RuntimeRegistry);
- cwd confinado ao workspace (workspace_only);
- timeout, truncamento de stdout/stderr e limite de processos concorrentes;
- resultado estruturado (nunca exceção crua para o agente).
"""
from __future__ import annotations

import os
import subprocess
import threading
import time
import weakref
from collections.abc import Iterable, Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict, List, Optional

from .process_control import (attach_process_tree, popen_group_options,
                              release_process_tree, terminate_tree)
from .runtime_registry import RuntimeRegistry, registry_from_settings
from .workspace_manager import WorkspaceManager, workspace_from_settings

DEFAULT_LIMITS = {
    "timeout_s": 300,
    "max_output_bytes": 1_048_576,
    "max_processes": 4,
}

DEFAULT_ENV_ALLOWLIST = frozenset({
    "COMSPEC", "HOME", "LANG", "LC_ALL", "LOCALAPPDATA", "PATH",
    "PATHEXT", "SYSTEMDRIVE", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR",
    "USERPROFILE", "WINDIR",
})
_SECRET_ENV_MARKERS = (
    "API_KEY", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN",
)


class _BoundedCapture:
    def __init__(self, limit: int):
        if limit < 0:
            raise ValueError("max_output_bytes não pode ser negativo")
        self._remaining = limit
        self._lock = threading.Lock()
        self._data = {"stdout": bytearray(), "stderr": bytearray()}

    def add(self, stream_name: str, chunk: bytes) -> None:
        with self._lock:
            keep = min(self._remaining, len(chunk))
            if keep:
                self._data[stream_name].extend(chunk[:keep])
                self._remaining -= keep

    def text(self, stream_name: str) -> str:
        return bytes(self._data[stream_name]).decode("utf-8", errors="ignore")


def _is_safe_env_name(name: str) -> bool:
    upper = name.upper()
    return not any(marker in upper for marker in _SECRET_ENV_MARKERS)


def _minimal_environment(
    env_allowlist: Optional[Mapping[str, str] | Iterable[str]],
) -> Dict[str, str]:
    names = set(DEFAULT_ENV_ALLOWLIST)
    overrides: Dict[str, str] = {}
    if isinstance(env_allowlist, Mapping):
        overrides = {str(key): str(value)
                     for key, value in env_allowlist.items()
                     if _is_safe_env_name(str(key))}
    elif env_allowlist is not None:
        names.update(str(name) for name in env_allowlist)
    environment = {
        name: value for name, value in os.environ.items()
        if name.upper() in names and _is_safe_env_name(name)
    }
    environment.update(overrides)
    environment.setdefault("PYTHONIOENCODING", "utf-8")
    environment.setdefault("PYTHONUTF8", "1")
    return environment


def _drain_stream(pipe, capture: _BoundedCapture, name: str) -> None:
    try:
        while True:
            chunk = pipe.read(65_536)
            if not chunk:
                break
            capture.add(name, chunk)
    finally:
        pipe.close()


def _write_stdin(pipe, text: str, stop_event: threading.Event) -> None:
    data = memoryview(text.encode("utf-8"))
    offset = 0
    try:
        while offset < len(data) and not stop_event.is_set():
            written = pipe.write(data[offset:offset + 65_536])
            if written is None:
                break
            offset += written
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            pipe.close()
        except (OSError, ValueError):
            pass


class ExecutionController:
    """Cota de processos compartilhada por raiz canônica de projeto."""

    def __init__(self, max_processes: int):
        if max_processes < 1:
            raise ValueError("max_processes deve ser positivo")
        self.max_processes = max_processes
        self._semaphore = threading.BoundedSemaphore(max_processes)

    def acquire(self, timeout: float) -> bool:
        return self._semaphore.acquire(timeout=timeout)

    def release(self) -> None:
        self._semaphore.release()


_CONTROLLERS_LOCK = threading.Lock()
_CONTROLLERS: weakref.WeakValueDictionary[str, ExecutionController] = \
    weakref.WeakValueDictionary()


def _controller_for(root: Path, max_processes: int) -> ExecutionController:
    key = str(root.resolve()).casefold()
    with _CONTROLLERS_LOCK:
        controller = _CONTROLLERS.get(key)
        if controller is None:
            controller = ExecutionController(max_processes)
            _CONTROLLERS[key] = controller
        elif controller.max_processes != max_processes:
            raise ValueError(
                "max_processes divergente para o mesmo workspace: "
                f"{controller.max_processes} != {max_processes}")
        return controller


def _canonical_executable(path: Path | str) -> Path:
    return Path(path).resolve()


class ProcessRunner:
    def __init__(self, registry: RuntimeRegistry, workspace: WorkspaceManager,
                 timeout_s: int = 300, max_output_bytes: int = 1_048_576,
                 max_processes: int = 4, workspace_only: bool = True,
                 executables_allowlist: Optional[List[Path | str]] = None):
        self.registry = registry
        self.workspace = workspace
        self.timeout_s = timeout_s
        self.max_output_bytes = max_output_bytes
        self._controller = _controller_for(workspace.root, max_processes)
        self.max_processes = self._controller.max_processes
        self.workspace_only = workspace_only
        self.executables_allowlist = frozenset(
            _canonical_executable(path) for path in
            (executables_allowlist or []))
        self._lock = threading.Lock()
        self.active: List[str] = []

    @classmethod
    def from_settings(cls, settings) -> "ProcessRunner":
        limits = settings.external_limits or DEFAULT_LIMITS
        external_policy = (settings.policies_cfg or {}).get(
            "external_execution", {}) or {}
        return cls(
            registry_from_settings(settings),
            workspace_from_settings(settings),
            timeout_s=int(limits.get("timeout_s", 300)),
            max_output_bytes=int(limits.get("max_output_bytes", 1_048_576)),
            max_processes=int(limits.get("max_processes", 4)),
            workspace_only=bool(external_policy.get("workspace_only", True)),
            executables_allowlist=list(
                external_policy.get("executables_allowlist", []) or []),
        )

    # -- API -----------------------------------------------------------------------

    def resolve_runtime(self, runtime_name: str):
        """Retorna runtime utilizável e seu executável canônico autorizado."""
        runtime = self.registry.usable(runtime_name)
        executable = _canonical_executable(runtime.resolved_path or "")
        if (self.executables_allowlist
                and executable not in self.executables_allowlist):
            raise PermissionError(
                f"executável fora da allowlist: {executable}")
        return runtime, executable

    def run(self, runtime_name: str, argv: List[str],
            extra_args: Optional[List[str]] = None,
            cwd: Optional[Path | str] = None,
            stdin_text: Optional[str] = None,
            timeout_s: Optional[float] = None,
            env_allowlist: Optional[
                Mapping[str, str] | Iterable[str]] = None) -> Dict[str, Any]:
        """Executa ``runtime argv`` com limites e devolve resultado estruturado."""
        started = time.time()
        try:
            runtime, executable = self.resolve_runtime(runtime_name)
        except (PermissionError, KeyError, RuntimeError) as exc:
            return {"success": False, "error": {"type": type(exc).__name__,
                                                "message": str(exc)}}
        try:
            cwd_path = self.workspace.confine(cwd) if cwd is not None else \
                self.workspace.root
        except PermissionError as exc:
            return {"success": False,
                    "error": {"type": "PermissionError", "message": str(exc)}}
        if self.workspace_only and not cwd_path.is_relative_to(self.workspace.root):
            return {"success": False, "error": {
                "type": "PermissionError",
                "message": "cwd fora do workspace"}}

        cmd = [str(executable), *runtime.args, *argv, *(extra_args or [])]
        call_timeout = self.timeout_s if timeout_s is None else timeout_s
        if call_timeout <= 0:
            return {"success": False, "error": {
                "type": "ValueError", "message": "timeout_s deve ser positivo"}}
        if not self._controller.acquire(timeout=5):
            return {"success": False, "error": {
                "type": "Busy",
                "message": f"limite de {self.max_processes} processos ativos"}}

        with self._lock:
            self.active.append(runtime_name)
        lease_stack = ExitStack()
        try:
            lease_stack.enter_context(self.workspace.task_lease(cwd_path))
            capture = _BoundedCapture(self.max_output_bytes)
            deadline = time.monotonic() + call_timeout
            proc = subprocess.Popen(  # noqa: S603 — executável canônico autorizado
                cmd, cwd=str(cwd_path),
                stdin=subprocess.PIPE if stdin_text is not None else None,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=_minimal_environment(env_allowlist),
                **popen_group_options(),
            )
            attach_process_tree(proc)
            readers = [
                threading.Thread(target=_drain_stream,
                                 args=(proc.stdout, capture, "stdout"),
                                 daemon=True),
                threading.Thread(target=_drain_stream,
                                 args=(proc.stderr, capture, "stderr"),
                                 daemon=True),
            ]
            for reader in readers:
                reader.start()
            writer = None
            writer_stop = threading.Event()
            if proc.stdin is not None:
                writer = threading.Thread(
                    target=_write_stdin,
                    args=(proc.stdin, stdin_text, writer_stop),
                    name=f"external-runtime-stdin-{proc.pid}",
                    daemon=True,
                )
                writer.start()
            timed_out = False
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(cmd, call_timeout)
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                timed_out = True
                writer_stop.set()
                terminate_tree(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            finally:
                release_process_tree(proc)
                writer_stop.set()
            for reader in readers:
                reader.join(timeout=5)
            if writer is not None:
                writer.join(timeout=5)
                if writer.is_alive():
                    try:
                        proc.stdin.close()
                    except (OSError, ValueError):
                        pass
                    writer.join(timeout=1)
            stdout = capture.text("stdout")
            stderr = capture.text("stderr")
            if timed_out:
                return {
                    "success": False, "runtime": runtime_name,
                    "command": cmd, "stdout": stdout, "stderr": stderr,
                    "error": {"type": "Timeout",
                              "message": f"excedeu {call_timeout}s"},
                    "duration_s": round(time.time() - started, 2),
                }
            return {
                "success": proc.returncode == 0,
                "runtime": runtime_name,
                "command": cmd,
                "returncode": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "cwd": str(cwd_path),
                "duration_s": round(time.time() - started, 2),
            }
        except OSError as exc:
            return {
                "success": False, "runtime": runtime_name, "command": cmd,
                "error": {"type": type(exc).__name__, "message": str(exc)},
                "duration_s": round(time.time() - started, 2),
            }
        finally:
            lease_stack.close()
            with self._lock:
                self.active.remove(runtime_name)
            self._controller.release()
