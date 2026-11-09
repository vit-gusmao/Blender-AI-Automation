"""Runner de Python externo isolado (§8).

Escreve o código em script dentro do workspace e executa em subprocesso
com o interpretador configurado (nunca no processo do servidor). Suporta
o padrão "gerar → executar → produzir asset" do §8.1.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Optional

from .process_runner import ProcessRunner
from .workspace_manager import WorkspaceManager


class PythonRunner:
    def __init__(self, runner: ProcessRunner,
                 workspace: Optional[WorkspaceManager] = None):
        self.runner = runner
        self.workspace = workspace or runner.workspace

    @classmethod
    def from_settings(cls, settings) -> "PythonRunner":
        return cls(ProcessRunner.from_settings(settings))

    def _write_script(self, code: str, task_dir: Path) -> Path:
        script = task_dir / "script.py"
        script.write_text(code, encoding="utf-8")
        return script

    def run_code(self, code: str, args: Optional[list] = None,
                 timeout_s: Optional[int] = None) -> Dict[str, Any]:
        task_dir = self.workspace.create_task_dir("py")
        with self.workspace.task_lease(task_dir):
            script = self._write_script(code, task_dir)
            result = self.runner.run(
                "python", [str(script)], extra_args=list(args or []),
                cwd=task_dir, timeout_s=timeout_s)
        result["script"] = str(script)
        result["task_dir"] = str(task_dir)
        return result

    def run_script(self, script_path: Path | str,
                   args: Optional[list] = None) -> Dict[str, Any]:
        try:
            path = self.workspace.resolve_existing_file(script_path)
        except (PermissionError, FileNotFoundError, IsADirectoryError) as exc:
            return {"success": False, "error": {
                "type": type(exc).__name__, "message": str(exc)}}
        with self.workspace.task_lease(path.parent):
            result = self.runner.run(
                "python", [str(path)], extra_args=list(args or []),
                cwd=path.parent)
        result["script"] = str(path)
        return result

    def run_module(self, module: str,
                   args: Optional[list] = None) -> Dict[str, Any]:
        argv = ["-m", module, *(args or [])]
        if module == "universal_blender_mcp":
            argv = [*argv]  # servidor é módulo do próprio repo
        return self.runner.run("python", argv)


def default_interpreter() -> str:
    return sys.executable
