"""Registro de runtimes externos autorizados (§8).

Lê ``[external_runtimes.*]`` de config/app.toml e descobre executáveis no
PATH. Um runtime precisa estar ``allowed = true`` E ter executável
encontrável para rodar.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class Runtime:
    name: str
    executable: str
    args: List[str] = field(default_factory=list)
    allowed: bool = False
    resolved_path: Optional[str] = None
    available: bool = False

    def resolve(self) -> "Runtime":
        found = shutil.which(self.executable)
        self.resolved_path = str(Path(found).resolve()) if found else None
        self.available = self.resolved_path is not None
        return self


class RuntimeRegistry:
    def __init__(self, runtimes: Dict[str, Any] | None = None):
        self._runtimes: Dict[str, Runtime] = {}
        for name, cfg in (runtimes or {}).items():
            cfg = cfg or {}
            runtime = Runtime(
                name=name,
                executable=str(cfg.get("executable", name)),
                args=[str(a) for a in (cfg.get("args") or [])],
                allowed=bool(cfg.get("allowed", False)),
            ).resolve()
            self._runtimes[name] = runtime

    # -- API ---------------------------------------------------------------------

    def get(self, name: str) -> Runtime:
        runtime = self._runtimes.get(name)
        if runtime is None:
            raise KeyError(
                f"runtime não registrado: {name} "
                f"(disponíveis: {sorted(self._runtimes)})")
        return runtime

    def usable(self, name: str) -> Runtime:
        runtime = self.get(name)
        if not runtime.allowed:
            raise PermissionError(
                f"runtime '{name}' não está autorizado em config/app.toml")
        if not runtime.available:
            raise RuntimeError(
                f"executável não encontrado no PATH: {runtime.executable}")
        return runtime

    def list(self, only_allowed: bool = False) -> List[Runtime]:
        return [r for r in self._runtimes.values()
                if (r.allowed or not only_allowed)]

    def describe(self) -> List[Dict[str, Any]]:
        return [
            {"name": r.name, "executable": r.executable,
             "allowed": r.allowed, "available": r.available,
             "path": r.resolved_path}
            for r in self._runtimes.values()
        ]


def registry_from_settings(settings) -> RuntimeRegistry:
    return RuntimeRegistry(settings.external_runtimes)
