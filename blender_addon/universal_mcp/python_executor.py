# SPDX-License-Identifier: MIT
"""Execução controlada de Python dentro do Blender (Modo 2 do §6.2).

Convenções:
- `bpy`, `bmesh`, `mathutils`, `bpy_extras` já disponíveis no namespace;
- atribua a variável ``result`` para devolver dados JSON-serializáveis;
- stdout/stderr são capturados e devolvidos no envelope;
- modo estrito (padrão) bloqueia padrões perigosos ANTES do exec (§14).

Sem isolamento real de sandbox existe: a checagem estática + autorização
ADVANCED + auditoria são o controle disponível nesta versão (ver §14 do
plano: execução arbitrária nunca é "segura", é *controlada*).
"""
from __future__ import annotations

import bmesh
import io
import time
import threading
import traceback
from contextlib import redirect_stdout
from typing import Any, Dict

import bpy
import mathutils
import bpy_extras
from bpy_extras import object_utils

from .scene_serializer import to_jsonable
from .security import AddonSecurity


class _ThreadCapture:
    """Route only the executing thread into its command's output buffer."""
    def __init__(self, buffer, original):
        self.buffer = buffer
        self.original = original
        self.owner = threading.get_ident()

    def _target(self):
        return self.buffer if threading.get_ident() == self.owner else self.original

    def write(self, value):
        return self._target().write(value)

    def flush(self):
        return self._target().flush()

    def __getattr__(self, name):
        return getattr(self._target(), name)


def execute_code(code: str, security: AddonSecurity = None,
                 timeout_note: bool = True) -> Dict[str, Any]:
    security = security or AddonSecurity()
    started = time.time()

    if security.strict_python:
        findings = security.scan_python_source(code)
        if findings:
            return {
                "success": False,
                "error": {"type": "PolicyBlocked",
                          "message": "código bloqueado pelas políticas do addon "
                                     "(padrões perigosos)",
                          "findings": findings},
                "stdout": "",
                "duration_s": round(time.time() - started, 3),
            }

    namespace: Dict[str, Any] = {
        "bpy": bpy, "bmesh": bmesh, "mathutils": mathutils,
        "bpy_extras": bpy_extras,
        "result": None, "__name__": "__mcp_exec__",
    }
    stdout, stderr = io.StringIO(), io.StringIO()
    try:
        compiled = compile(code, "<mcp_execute>", "exec")
        import sys
        with redirect_stdout(_ThreadCapture(stdout, sys.stdout)):
            import contextlib

            with contextlib.redirect_stderr(_ThreadCapture(stderr, sys.stderr)):
                exec(compiled, namespace)  # noqa: S102 — controlado por política
        return {
            "success": True,
            "result": to_jsonable(namespace.get("result")),
            "stdout": stdout.getvalue()[-20000:],
            "stderr": stderr.getvalue()[-20000:],
            "duration_s": round(time.time() - started, 3),
            "timeout_enforced": False,
            "timeout_note": ("in-process execution cannot be interrupted; Bridge "
                             "timeout limits waiting only") if timeout_note else None,
        }
    except BaseException as exc:  # SystemExit/KeyboardInterrupt must not exit Blender
        tb = traceback.format_exc()
        return {
            "success": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc)[:500] or _last_exception_message(tb),
                "traceback": tb[-4000:],
            },
            "result": to_jsonable(namespace.get("result")),
            "stdout": stdout.getvalue()[-20000:],
            "stderr": stderr.getvalue()[-20000:],
            "duration_s": round(time.time() - started, 3),
        }


def _last_exception_message(tb: str) -> str:
    lines = [line for line in tb.strip().splitlines() if line.strip()]
    return lines[-1][:500] if lines else "erro desconhecido em python_execute"
