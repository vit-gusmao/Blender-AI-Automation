"""Runtime externo multilíngue (§8 do plano).

Executa utilitários fora do Blender, em workspace isolado, com runtimes
autorizados, timeout e limites de saída. O LLM não recebe acesso livre a
``cmd.exe``/PowerShell: tudo passa por esta camada.
"""
from .workspace_manager import WorkspaceManager
from .process_runner import ProcessRunner
from .python_runner import PythonRunner
from .runtime_registry import RuntimeRegistry

__all__ = [
    "WorkspaceManager", "ProcessRunner", "PythonRunner", "RuntimeRegistry",
]
