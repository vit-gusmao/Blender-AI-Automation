"""Isolamento de diretórios para execuções externas (§8.1).

Todo trabalho externo acontece dentro de ``storage/external_workspace``;
cada tarefa ganha um subdiretório próprio. Caminhos fora do workspace são
recusados (confinamento contra escapes relativos e absolutos).
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List


_SAFE_PREFIX = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_LOCK_DIR_NAME = ".external_runtime_locks"  # legado: nunca tratar como task
_LOCK_FILE_PREFIX = ".external_runtime_lock_"
_FILESYSTEM_LOCK_GUARD = threading.RLock()
_LEASE_COUNTS: dict[Path, int] = {}
_LEASE_HANDLES: dict[Path, "_FileLock"] = {}
_CLEANUP_ROOTS: set[Path] = set()


class _FileLock:
    """Lock advisory stdlib compartilhado entre managers e processos."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        self._handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(
                    self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            self._handle.close()
            raise

    def close(self) -> None:
        if self._handle.closed:
            return
        self._handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()


class WorkspaceManager:
    def __init__(self, root: Path | str):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._task_lock = threading.RLock()
        self._active_task_dirs: dict[Path, int] = {}

    def _task_root(self, path: Path) -> Path | None:
        relative = path.relative_to(self.root)
        return self.root / relative.parts[0] if relative.parts else None

    def _lock_path(self, task_root: Path) -> Path:
        key = hashlib.sha256(
            str(task_root).casefold().encode("utf-8")).hexdigest()
        return self.root / f"{_LOCK_FILE_PREFIX}{key}.lock"

    def _acquire_task_root(self, task_root: Path) -> None:
        with _FILESYSTEM_LOCK_GUARD:
            if task_root in _CLEANUP_ROOTS:
                raise PermissionError(
                    f"diretório de tarefa em limpeza: {task_root}")
            count = _LEASE_COUNTS.get(task_root, 0)
            if count == 0:
                try:
                    _LEASE_HANDLES[task_root] = _FileLock(
                        self._lock_path(task_root))
                except (OSError, BlockingIOError) as exc:
                    raise PermissionError(
                        f"diretório de tarefa bloqueado: {task_root}") from exc
            _LEASE_COUNTS[task_root] = count + 1

    def _release_task_root(self, task_root: Path) -> None:
        with _FILESYSTEM_LOCK_GUARD:
            remaining = _LEASE_COUNTS[task_root] - 1
            if remaining:
                _LEASE_COUNTS[task_root] = remaining
                return
            del _LEASE_COUNTS[task_root]
            _LEASE_HANDLES.pop(task_root).close()

    # -- criação de áreas de trabalho ------------------------------------------

    def create_task_dir(self, prefix: str = "task") -> Path:
        import uuid

        if _SAFE_PREFIX.fullmatch(prefix) is None:
            raise ValueError(f"prefixo de tarefa inválido: {prefix!r}")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        task_dir = self.root / f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}"
        task_dir.mkdir(parents=True, exist_ok=False)
        return task_dir

    def task_dir_for(self, name: str) -> Path:
        task_dir = (self.root / name).resolve()
        if not task_dir.is_relative_to(self.root):
            raise PermissionError(
                f"caminho fora do workspace: {name}")
        task_dir.mkdir(parents=True, exist_ok=True)
        return task_dir

    # -- confinamento -------------------------------------------------------------

    def confine(self, path: Path | str) -> Path:
        """Resolve e valida que o caminho está dentro do workspace."""
        p = Path(path)
        resolved = (p if p.is_absolute() else (self.root / p)).resolve()
        if not resolved.is_relative_to(self.root):
            raise PermissionError(
                f"caminho fora do workspace externo: {resolved}")
        return resolved

    def resolve_existing_file(self, path: Path | str) -> Path:
        """Resolve um arquivo existente sem permitir escapes do workspace."""
        resolved = self.confine(path)
        if not resolved.exists():
            raise FileNotFoundError(str(resolved))
        if not resolved.is_file():
            raise IsADirectoryError(str(resolved))
        return resolved

    @property
    def active_task_dirs(self) -> frozenset[Path]:
        with self._task_lock:
            return frozenset(self._active_task_dirs)

    @contextmanager
    def task_lease(self, task_dir: Path | str) -> Iterator[Path]:
        """Impede que ``cleanup`` remova um diretório enquanto ele está em uso."""
        resolved = self.confine(task_dir)
        task_root = self._task_root(resolved)
        with self._task_lock:
            if not resolved.is_dir():
                raise NotADirectoryError(str(resolved))
            if task_root is not None:
                self._acquire_task_root(task_root)
            self._active_task_dirs[resolved] = \
                self._active_task_dirs.get(resolved, 0) + 1
        try:
            yield resolved
        finally:
            with self._task_lock:
                remaining = self._active_task_dirs[resolved] - 1
                if remaining:
                    self._active_task_dirs[resolved] = remaining
                else:
                    del self._active_task_dirs[resolved]
                if task_root is not None:
                    self._release_task_root(task_root)

    def list_artifacts(self, subdir: str = "") -> List[Path]:
        base = self.confine(subdir) if subdir else self.root
        lock_dir = self.root / _LOCK_DIR_NAME
        return sorted(
            p for p in base.rglob("*")
            if (p.is_file() and lock_dir not in p.parents
                and not p.name.startswith(_LOCK_FILE_PREFIX))
        )

    # -- limpeza -------------------------------------------------------------------

    def cleanup(self, keep_last: int = 20) -> int:
        if keep_last < 0:
            raise ValueError("keep_last não pode ser negativo")
        task_dirs = sorted(
            (d for d in self.root.iterdir()
             if d.is_dir() and d.name != _LOCK_DIR_NAME),
            key=lambda d: d.stat().st_mtime, reverse=True,
        )
        removed = 0
        with self._task_lock:
            for old in task_dirs[keep_last:]:
                resolved = old.resolve()
                if any(active == resolved or active.is_relative_to(resolved)
                       for active in self._active_task_dirs):
                    continue
                with _FILESYSTEM_LOCK_GUARD:
                    if _LEASE_COUNTS.get(resolved, 0):
                        continue
                    try:
                        cleanup_lock = _FileLock(self._lock_path(resolved))
                    except (OSError, BlockingIOError):
                        continue
                    _CLEANUP_ROOTS.add(resolved)
                try:
                    shutil.rmtree(old)
                    removed += 1
                finally:
                    with _FILESYSTEM_LOCK_GUARD:
                        _CLEANUP_ROOTS.remove(resolved)
                        cleanup_lock.close()
        return removed


def workspace_from_settings(settings) -> WorkspaceManager:
    return WorkspaceManager(settings.external_workspace)
