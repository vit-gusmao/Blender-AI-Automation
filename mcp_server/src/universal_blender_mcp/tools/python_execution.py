"""Execução de Python: dentro do Blender (via Bridge) e runtime externo."""
from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any, Dict

from ..authorization import AuthLevel
from ..tool_registry import ServerContext, ToolRegistry, ToolSpec, bridge_tool


def register(ctx: ServerContext, registry: ToolRegistry) -> None:
    bridge_tool(
        ctx, registry, "python_execute_blender",
        "Executa código Python dentro do Blender (bpy) na thread principal. "
        "Nível ADVANCED; padrões perigosos são bloqueados pelas políticas. "
        "Convenção: atribua a variável `result` para retornar dados; stdout "
        "é capturado. O timeout limita a espera da Bridge; não interrompe código "
        "já em execução dentro do Blender.",
        "python",
        {"properties": {
            "code": {"type": "string"},
            "timeout_s": {"type": "integer"},
        }, "required": ["code"]},
        AuthLevel.ADVANCED, "high", timeout_s=300,
    )

    async def python_execute_external(args: Dict[str, Any]) -> Dict[str, Any]:
        import sys
        root_path = str(Path(ctx.settings.root))
        if root_path not in sys.path:  # idempotente (review F5)
            sys.path.insert(0, root_path)
        from external_runtime.process_runner import ProcessRunner  # noqa: PLC0415
        from ..schemas import new_command_id  # noqa: PLC0415

        def envelope(data, success):
            return {"command_id": new_command_id(),
                    "status": "completed" if success else "failed",
                    "success": success, "data": data, "scene_revision": None,
                    "error": None if success else {
                        "type": (data.get("error") or {}).get("type", "ExternalError"),
                        "message": (data.get("error") or {}).get(
                            "message", "execução externa falhou")}}

        runtime = args.get("runtime", "python")
        policy = ctx.policy
        backend_name = getattr(policy, "external_backend", None)
        backend = None
        if backend_name:
            backend = getattr(ctx, "external_execution_backends", {}).get(
                backend_name)
            if not callable(backend):
                return envelope({"success": False, "error": {
                    "type": "SandboxUnavailable",
                    "message": (f"backend de sandbox '{backend_name}' "
                                "não está disponível")}}, False)
        else:
            if not getattr(policy, "allow_unsafe_host_execution", False):
                return envelope({"success": False, "error": {
                    "type": "SandboxUnavailable",
                    "message": ("nenhum backend de sandbox configurado; "
                                "execução host insegura está desabilitada")}},
                                False)
            expected = getattr(policy, "unsafe_approval_token", "")
            if (not expected
                    or args.get("unsafe_approval_token") != expected):
                return envelope({"success": False, "error": {
                    "type": "UnsafeApprovalRequired",
                    "message": ("execução host requer aprovação explícita "
                                "válida nesta requisição")}}, False)

        runner = ProcessRunner.from_settings(ctx.settings)
        try:
            runtime_spec, executable = runner.resolve_runtime(runtime)
        except (PermissionError, KeyError, RuntimeError) as exc:
            return envelope({"success": False, "error": {
                "type": type(exc).__name__, "message": str(exc)}}, False)
        script = None
        task_dir = None
        if args.get("code") and runtime == "python":
            task_dir = runner.workspace.create_task_dir("mcp_inline")
            script = task_dir / "script.py"
            script.write_text(args["code"], encoding="utf-8")
        elif args.get("script_path"):
            try:
                script = runner.workspace.resolve_existing_file(
                    args["script_path"])
            except (PermissionError, FileNotFoundError,
                    IsADirectoryError) as exc:
                return envelope({"success": False, "error": {
                    "type": type(exc).__name__, "message": str(exc)}}, False)
            task_dir = script.parent
        else:
            return envelope({"success": False,
                             "error": {"type": "InvalidArguments",
                                       "message": "forneça 'code' (runtime python) "
                                                  "ou 'script_path'"}}, False)

        request = {
            "runtime": str(executable),
            "runtime_name": runtime,
            "runtime_args": list(runtime_spec.args),
            "script_path": str(script),
            "args": list(args.get("args", [])),
            "cwd": str(task_dir),
            "timeout_s": args.get("timeout_s"),
        }
        with runner.workspace.task_lease(task_dir):
            if backend is not None:
                out = backend(request)
                if inspect.isawaitable(out):
                    out = await out
                if not isinstance(out, dict):
                    return envelope({"success": False, "error": {
                        "type": "SandboxBackendError",
                        "message": "backend retornou resultado inválido"}}, False)
                out = dict(out)
                out.setdefault("execution_mode", f"sandbox:{backend_name}")
            else:
                out = await asyncio.to_thread(
                    runner.run, runtime, [str(script)],
                    request["args"], task_dir, None,
                    request["timeout_s"])
                out["execution_mode"] = "unsafe_host"
        return envelope(out, bool(out.get("success")))

    registry.register(ToolSpec(
        "python_execute_external",
        "Executa script em runtime externo autorizado por um backend de "
        "sandbox configurado. Sem backend, falha por padrão; subprocesso host "
        "exige opt-in de política e aprovação explícita na requisição. O "
        "workspace, timeout, ambiente e saída são limitados. Nível ADVANCED.",
        "python", AuthLevel.ADVANCED,
        {"properties": {
            "runtime": {"type": "string", "description": "Nome do runtime registrado"},
            "code": {"type": "string", "description": "Código (apenas runtime python)"},
            "script_path": {"type": "string"},
            "args": {"type": "array", "items": {"type": "string"}},
            "timeout_s": {"type": "number", "exclusiveMinimum": 0},
            "unsafe_approval_token": {"type": "string"},
        }},
        handler=python_execute_external, bridge=False, risk="high",
        timeout_s=300,
    ))
